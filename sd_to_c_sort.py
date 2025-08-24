"""
PhotoSorterPro (sd_to_c_sort) — Clean photo organization tool

Scans an SD card or a specified folder and organizes image files into destination
folders based on capture date, camera/lens model, and file type (JPG/RAW/other).
Provides a preview, duplicate-handling policies, copy/move operations, and
settings suitable for large-scale photo organization.

Key features:
    * Group by camera or lens
    * Choose folder hierarchy: device-first or date-first
    * Optionally separate RAW and JPG into distinct subfolders
    * Select copy or move behavior
    * Skip identical files by comparing content hashes
    * Multi-threaded metadata extraction and processing (uses single-threaded mode for interactive duplicate prompts)
    * EXIF extraction via multiple strategies (Pillow, exifread, external exiftool) for robustness
    * Preview, summary, progress reporting, and logging before and during sorting
"""

import os
import sys
import json
import shutil
import time
import traceback
import subprocess
import hashlib
from datetime import datetime
from pathlib import Path
import threading
import queue
import webbrowser

# ---- High DPI (Windows)
try:
    import ctypes  # type: ignore
    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore
except Exception:
    pass

# ---- GUI
import customtkinter as ctk  # type: ignore
from tkinter import filedialog, messagebox, ttk  # type: ignore

# ---- EXIF (optional)
try:
    from PIL import Image  # type: ignore
    from PIL.ExifTags import TAGS  # type: ignore
    PIL_OK = True
except Exception:
    PIL_OK = False

try:
    import exifread  # type: ignore
    EXIFREAD_OK = True
except Exception:
    EXIFREAD_OK = False

APP_NAME = "sd_to_c_sort"
DEFAULT_DEST = r"D:\\pics\\2025-1"

CONFIG_DIR = Path(os.getenv("APPDATA") or Path.home() / ".config") / APP_NAME
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = CONFIG_DIR / "run.log"
STATE_FILE = CONFIG_DIR / "state.json"

IMAGE_EXT = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff",
    ".arw", ".cr2", ".cr3", ".nef", ".orf", ".rw2", ".raf", ".dng", ".srw", ".pef"
}
PROC_EXT = {".jpg", ".jpeg", ".heic", ".heif", ".png"}
RAW_EXT = {
    ".arw", ".cr2", ".cr3", ".nef", ".orf", ".rw2", ".raf", ".dng", ".srw", ".pef", ".tif", ".tiff"
}


def read_state() -> dict:
    """Read the state file and return default settings."""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    # Return default configuration with new options included
    return {
        "dest_root": DEFAULT_DEST,
        "policy": "ask",  # rename, skip, ask
        "appearance": "light",
        "scale": 1.0,
        "group_by": "camera",  # camera | lens
        "hierarchy": "device-first",  # device-first | date-first
        "split_raw_jpg": True,  # separate RAW/JPG files
        "action": "copy",  # copy | move
        "skip_hash_dup": False,  # skip if content hash duplicates
        "max_workers": 1,  # number of concurrent processing threads (1=sequential)
    }


def write_state(data: dict) -> None:
    """Save configuration state to the file."""
    try:
        STATE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def log_write(msg: str) -> None:
    """Append a string to the log file."""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def walk_images(root: Path):
    """Traverse all image files under the root folder."""
    for dp, _, fns in os.walk(root):
        for fn in fns:
            p = Path(dp) / fn
            if p.suffix.lower() in IMAGE_EXT:
                yield p


def human(n: int) -> str:
    """Return a human‑readable representation of an integer."""
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else (f"{n / 1_000:.1f}k" if n >= 1_000 else str(n))


def which_exiftool() -> str | None:
    """Return the path to the exiftool executable if available, otherwise None."""
    return shutil.which("exiftool")


def sanitize(name: str) -> str:
    """Sanitize a string so it is safe for use as a folder or file name."""
    if not name:
        return "Unknown"
    safe_chars = []
    for ch in name.strip():
        if ch.isalnum() or ch in " ._-()+[]#/":
            safe_chars.append(ch)
        else:
            safe_chars.append(" ")
    s = " ".join("".join(safe_chars).split())
    trimmed = s[:120] if len(s) > 120 else s
    return trimmed or "Unknown"


def parse_dt_str(s: str) -> datetime | None:
    """Convert an EXIF date string into a :class:`datetime` object."""
    s = s.strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z"):
        try:
            dt = datetime.strptime(s, fmt)
            # Remove timezone information if present
            return dt.replace(tzinfo=None)
        except Exception:
            continue
    return None


def exif_from_pillow(path: Path):
    """Extract date, camera and lens metadata using Pillow."""
    if not PIL_OK:
        return None, None, None
    try:
        with Image.open(path) as im:  # type: ignore
            exif = im.getexif()
            if not exif:
                return None, None, None
            # Find date/time string
            dto = None
            for key in (36867, 306):  # DateTimeOriginal, DateTime
                if key in exif:
                    dto = parse_dt_str(str(exif.get(key)))
                    if dto:
                        break
            model = str(exif.get(0x0110) or "")  # Model
            lens = str(exif.get(0xA434) or "")  # LensModel
            return dto, (model or None), (lens or None)
    except Exception:
        return None, None, None


def exif_from_exifread(path: Path):
    """Extract EXIF metadata using the :mod:`exifread` module."""
    if not EXIFREAD_OK:
        return None, None, None
    try:
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False, stop_tag="UNDEF", strict=True)  # type: ignore
        dto = None
        for key in ("EXIF DateTimeOriginal", "Image DateTime"):
            if key in tags:
                dto = parse_dt_str(str(tags[key]))
                if dto:
                    break
        model = None
        for key in ("Image Model", "EXIF Model"):
            if key in tags:
                model = str(tags[key]).strip()
                break
        lens = None
        for key in ("EXIF LensModel", "EXIF LensSpecification", "EXIF LensMake", "MakerNote LensType"):
            if key in tags:
                lens = str(tags[key]).strip()
                break
        return dto, model or None, lens or None
    except Exception:
        return None, None, None


def exif_from_exiftool(path: Path):
    """Extract EXIF metadata using the external ``exiftool`` executable."""
    exe = which_exiftool()
    if not exe:
        return None, None, None
    try:
        # Call exiftool to extract only essential fields (model, make, lens, date)
        cmd = [exe, "-json", "-S", "-Model", "-Make", "-LensModel", "-Lens", "-DateTimeOriginal", str(path)]
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
        data = json.loads(out.decode("utf-8", errors="ignore"))[0] if out else {}
        dto = None
        if "DateTimeOriginal" in data:
            dto = parse_dt_str(str(data.get("DateTimeOriginal")))
        model = data.get("Model") or ""
        make = data.get("Make") or ""
        if make and model and make not in model:
            model = f"{make} {model}"
        lens = data.get("LensModel") or data.get("Lens") or ""
        return dto, (model or None), (lens or None)
    except Exception:
        return None, None, None


def extract_meta(path: Path) -> dict:
    """
    Extract date, camera, lens and file type information from the given file.

    Multiple strategies are used in sequence (Pillow, exifread, exiftool) to fill
    in as much metadata as possible. Falls back to the file modification time
    if no EXIF date is found.
    """
    dto = cam = lens = None
    # pillow
    d1, c1, l1 = exif_from_pillow(path)
    if d1:
        dto = d1
    if c1:
        cam = c1
    if l1:
        lens = l1
    # exifread
    d2, c2, l2 = exif_from_exifread(path)
    if not dto and d2:
        dto = d2
    if not cam and c2:
        cam = c2
    if not lens and l2:
        lens = l2
    # exiftool
    d3, c3, l3 = exif_from_exiftool(path)
    if not dto and d3:
        dto = d3
    if not cam and c3:
        cam = c3
    if not lens and l3:
        lens = l3
    # fallback
    if dto is None:
        try:
            dto = datetime.fromtimestamp(path.stat().st_mtime)
        except Exception:
            dto = datetime.now()
    year = f"{dto:%Y}"
    month = f"{dto:%Y-%m}"
    date = f"{dto:%Y-%m-%d}"
    cam = sanitize(cam or "Unknown Camera")
    lens = sanitize(lens or "Unknown Lens")
    ext = path.suffix.lower()
    if ext in RAW_EXT:
        kind = "raw"
    elif ext in PROC_EXT:
        kind = "jpg"
    else:
        kind = "other"
    return {
        "path": path,
        "dt": dto,
        "year": year,
        "month": month,
        "date": date,
        "camera": cam,
        "lens": lens,
        "kind": kind,
    }


def unique_dest(dest_dir: Path, name: str) -> Path:
    """Generate a destination file path that will not collide with existing files."""
    base, ext = os.path.splitext(name)
    cand = dest_dir / name
    i = 1
    while cand.exists():
        cand = dest_dir / f"{base}_{i}{ext}"
        i += 1
    return cand


def file_hash(path: Path, chunk_size: int = 1 << 20) -> str:
    """Compute the SHA‑1 hash of a file (reads in chunks for efficiency)."""
    sha1 = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            while True:
                data = f.read(chunk_size)
                if not data:
                    break
                sha1.update(data)
        return sha1.hexdigest()
    except Exception:
        return ""


# ----- Theme definitions -----
# Modern color palette definitions (light/dark)
# Modern color palette inspired by YouTube style
PALETTE = {
    "light": {
        # Background color (light gray)
        "bg": "#F9F9F9",
        # Card and top bar background (white)
        "elev": "#FFFFFF",
        # Divider and sub-card background (light gray)
        "subtle": "#EDEDED",
        # Primary text (almost black)
        "text": "#0F0F0F",
        # Secondary text (medium gray)
        "text_sub": "#606060",
        # Primary accent color (YouTube blue)
        "primary": "#065FD4",
        # Danger/cancel color (YouTube red)
        "danger": "#C4302B",
        # Secondary accent color (bright blue)
        "accent": "#3EA6FF",
    },
    "dark": {
        # Background color (almost black)
        "bg": "#0F0F0F",
        # Card and top bar background (dark gray)
        "elev": "#1F1F1F",
        # Divider and sub-card background (dark gray)
        "subtle": "#353535",
        # Primary text (light gray)
        "text": "#F1F1F1",
        # Secondary text (gray)
        "text_sub": "#AAAAAA",
        # Primary accent color (bright blue)
        "primary": "#3EA6FF",
        # Danger/cancel color (YouTube red)
        "danger": "#C4302B",
        # Secondary accent color (light sky blue)
        "accent": "#65B5FF",
    },
}

# Translation dictionary
# Defines messages in English (en) and Korean (ko) for use in program summaries and dialogs.
TR_MSG: dict[str, dict[str, str]] = {
    # Summary after completing scan preview
    "scan_complete": {
        "en": "Preview complete. Current settings:",
        "ko": "미리보기 완료. 현재 설정:",
    },
    "group": {"en": "Group", "ko": "그룹"},
    "hierarchy": {"en": "Hierarchy", "ko": "계층"},
    "split_raw": {"en": "RAW-JPG Separation", "ko": "RAW-JPG 분리"},
    "action": {"en": "Action", "ko": "동작"},
    "skip_hash": {"en": "Skip same hash", "ko": "해시 중복 무시"},
    "thread_count": {"en": "Thread count", "ko": "스레드 수"},
    "total_files": {"en": "Total files", "ko": "총 파일"},
    "planned_folders": {"en": "Planned target folders", "ko": "예정 대상 폴더 수"},
    "pre_duplicates": {"en": "Pre-existing duplicates", "ko": "사전 중복"},
    "examples": {"en": "Examples:", "ko": "생성 예시(일부):"},
    "press_start": {"en": "Press Start Sorting to begin.", "ko": "정리 시작 버튼을 누르면 정리가 진행됩니다."},
    # Summary after sorting is completed
    "sort_completed": {"en": "Sorting completed.", "ko": "정리 작업이 완료되었습니다."},
    "success_count": {"en": "Success", "ko": "성공"},
    "skipped_count": {"en": "Skipped", "ko": "건너뜀"},
    "errors_count": {"en": "Errors", "ko": "오류"},
    "error_list": {"en": "Error list:", "ko": "오류 목록:"},
    "skip_list": {"en": "Skipped list:", "ko": "건너뜀 목록:"},
    "only_partial": {"en": "… only part of the list is shown.", "ko": "… 일부만 표시되었습니다."},
    # Initial settings and language selection
    "initial_settings": {"en": "Initial settings", "ko": "초기 설정"},
    "select_language": {"en": "Select Language", "ko": "언어 선택"},
    "english": {"en": "English", "ko": "English"},
    "korean": {"en": "Korean", "ko": "한국어"},
    "settings_overview": {"en": "Current settings", "ko": "현재 설정"},
    "dest_folder": {"en": "Destination folder", "ko": "대상 폴더"},
    "duplicates_policy": {"en": "Duplicate policy", "ko": "중복 정책"},

    # UI label/text translations
    # Top bar and actions section
    "app_subtitle": {"en": "SD → Sort by Camera/Lens/Date · Preview · Duplicate Policy · Progress", "ko": "SD → 카메라/렌즈/날짜 기반 정리 · 미리보기 · 중복 정책 · 진행률"},
    "task_title": {"en": "Actions", "ko": "작업"},
    "btn_pick": {"en": "1) Select Folder", "ko": "1) 폴더 선택"},
    "btn_scan": {"en": "2) Scan & Preview", "ko": "2) 스캔 · 미리보기"},
    "btn_start": {"en": "3) Start Sorting", "ko": "3) 정리 시작"},
    "btn_stop": {"en": "Stop", "ko": "중단"},
    "dest_card": {"en": "Destination Folder", "ko": "대상 폴더"},
    "change": {"en": "Change…", "ko": "변경…"},
    "dup_policy_card": {"en": "Duplicate Handling Policy", "ko": "중복 처리 정책"},
    "policy_rename": {"en": "Always New Name (_1)", "ko": "항상 새 이름(_1)"},
    "policy_skip": {"en": "Always Skip", "ko": "항상 건너뛰기"},
    "policy_ask": {"en": "Ask Every File", "ko": "매 파일마다 묻기"},
    # Options section
    "group_title": {"en": "Grouping", "ko": "정렬 기준 (그룹)"},
    "hier_title": {"en": "Hierarchy", "ko": "폴더 계층 (위치)"},
    "raw_split": {"en": "Separate RAW/JPG", "ko": "RAW/JPG 분리"},
    "action_title": {"en": "Operation (copy/move)", "ko": "동작 (복사/이동)"},
    "action_copy": {"en": "copy", "ko": "copy"},
    "action_move": {"en": "move", "ko": "move"},
    "hash_skip": {"en": "Skip if identical content (hash)", "ko": "내용이 같으면 건너뛰기 (해시)"},
    # Log
    "log": {"en": "Log", "ko": "로그"},
    # Summary cards
    "total_images": {"en": "Total Images", "ko": "총 이미지"},
    "date_folders": {"en": "Date Folders", "ko": "날짜 폴더"},
    "pre_conflicts": {"en": "Pre-existing Duplicates", "ko": "사전 중복"},
    "camera_count": {"en": "Cameras", "ko": "카메라 수"},
    "lens_count": {"en": "Lenses", "ko": "렌즈 수"},
    # Preview section
    "selected_folder": {"en": "Selected folder", "ko": "선택한 폴더"},
    "not_selected": {"en": "(none)", "ko": "(미선택)"},
    "preview_tree": {"en": "Planned Top Folders (Preview)", "ko": "생성될 상위 폴더(미리보기)"},
    "file_count": {"en": "File Count", "ko": "파일 수"},
    # Distribution summary
    "dist_summary": {"en": "Distribution Summary", "ko": "분포 요약"},
    "camera": {"en": "Camera", "ko": "카메라"},
    "count": {"en": "Count", "ko": "개수"},
    "lens": {"en": "Lens", "ko": "렌즈"},
    # Duplicate list
    "conflict_list": {"en": "Pre-existing Duplicates (same filename) List", "ko": "사전 중복(동일 파일명) 목록"},
    "existing_path": {"en": "Existing Destination Path", "ko": "이미 존재하는 대상 경로"},
    # Progress information
    "idle": {"en": "Idle", "ko": "대기 중"},
    # Settings window
    "settings_title": {"en": "Settings", "ko": "설정"},
    "dest_change_title": {"en": "Destination folder for copy/move", "ko": "복사/이동 대상 폴더"},
    "dup_default_policy": {"en": "Default Duplicate Policy", "ko": "중복 처리 기본 정책"},
    "theme": {"en": "Theme", "ko": "테마"},
    "interface_scale": {"en": "Interface Scale", "ko": "인터페이스 스케일"},
    "group_hier_options": {"en": "Grouping/Hierarchy/RAW Separation/Action/Hash Dup", "ko": "정렬 기준/폴더 계층/RAW분리/동작/중복 해시"},
    "threads_count": {"en": "Concurrent threads", "ko": "동시 처리 스레드 수"},
    "save": {"en": "Save", "ko": "저장"},
    "close": {"en": "Close", "ko": "닫기"},
    "folder_tree_example": {"en": "Folder Tree Example", "ko": "폴더 트리 예시"},

    # Donation/support messages
    "donate_link": {"en": "Buy me a coffee", "ko": "후원하기"},
    "donation_prompt": {
        "en": "If you find this tool helpful, please consider supporting development:\nhttps://buymeacoffee.com/modang",
        "ko": "이 도구가 유용하다면 후원을 부탁드립니다:\nhttps://buymeacoffee.com/modang",
    },

    # Error and dialog messages
    "error_invalid_folder": {"en": "Selected folder is invalid.", "ko": "선택한 폴더가 올바르지 않습니다."},
    "error_no_images": {"en": "No target image files were found.", "ko": "대상 이미지 파일이 없습니다."},
    "duplicate_processing": {"en": "Duplicate File Handling", "ko": "중복 파일 처리"},
    "duplicate_exists": {"en": "A file with the same name already exists.", "ko": "동일 파일명이 이미 존재합니다."},
    "rename_new": {"en": "Save with New Name", "ko": "새 이름으로 저장"},
    "skip_btn": {"en": "Skip", "ko": "건너뛰기"},
    "cancel_all_btn": {"en": "Cancel All", "ko": "전체 중단"},
    # Status and log prefixes
    "skip_same_content": {"en": "Skipped (identical content)", "ko": "건너뜀(같은 내용)"},
    "skip_duplicate_name": {"en": "Skipped (duplicate name)", "ko": "건너뜀(중복 이름)"},
    "skip_user": {"en": "Skipped (user)", "ko": "건너뜀(사용자)"},
    "processed_file": {"en": "Processed", "ko": "처리됨"},
    "failure_prefix": {"en": "Failure", "ko": "실패"},
    "speed": {"en": "Speed", "ko": "속도"},
    "ready": {"en": "Ready", "ko": "준비 완료."},
    "preview_done": {"en": "Preview complete. Press Start Sorting when ready.", "ko": "미리보기 완료. 정리 시작 버튼을 눌러 주세요."},
    "preview_prompt": {"en": "Preview completed. Review settings and press Start Sorting.", "ko": "미리보기 완료. 설정 확인 후 정리 시작 버튼을 누르세요."},
    "cancel_requested": {"en": "Cancel requested…", "ko": "중단 요청됨…"},
    "sort_done": {"en": "Sorting done.", "ko": "정리 완료."},
    "program_start": {"en": "=== program start ===", "ko": "=== 프로그램 시작 ==="},
    "program_end": {"en": "=== program end ===", "ko": "=== 프로그램 종료 ==="},
}


def font_stack(size: int = 12, weight: str = "normal") -> ctk.CTkFont:
    """Select a font from the available system font stack."""
    candidates = ["SF Pro Text", "SF Pro Display", "Segoe UI", "Helvetica", "Arial"]
    for name in candidates:
        try:
            f = ctk.CTkFont(family=name, size=size, weight=weight)  # type: ignore
            return f
        except Exception:
            continue
    return ctk.CTkFont(size=size, weight=weight)  # type: ignore


class PhotoSorterApp(ctk.CTk):
    """Main application class."""

    def __init__(self) -> None:
        super().__init__()
        self.state_data = read_state()
        # Appearance settings
        ctk.set_appearance_mode(self.state_data.get("appearance", "light"))  # type: ignore
        ctk.set_default_color_theme("blue")  # type: ignore
        ctk.set_widget_scaling(self.state_data.get("scale", 1.0))  # type: ignore
        self.title(APP_NAME)
        self.geometry("1220x800")
        self.minsize(1080, 700)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        # Runtime state
        self.src_dir: Path | None = None
        self.dest_root: Path = Path(self.state_data.get("dest_root") or DEFAULT_DEST)
        self.files: list[Path] = []
        self.metas: list[dict] = []
        self.plan: dict[Path, list[Path]] = {}
        self.conflicts: list[tuple[Path, Path]] = []
        self.stop_flag = threading.Event()
        self.ui_queue: queue.Queue = queue.Queue()
        # UI binding variables
        self.policy_var = ctk.StringVar(value=self.state_data.get("policy", "ask"))
        self.group_by_var = ctk.StringVar(value=self.state_data.get("group_by", "camera"))
        self.hier_var = ctk.StringVar(value=self.state_data.get("hierarchy", "device-first"))
        self.split_var = ctk.BooleanVar(value=self.state_data.get("split_raw_jpg", True))
        self.action_var = ctk.StringVar(value=self.state_data.get("action", "copy"))
        self.skip_hash_var = ctk.BooleanVar(value=self.state_data.get("skip_hash_dup", False))
        # Number of concurrent processing threads (>=1)
        self.workers_var = ctk.IntVar(value=max(1, int(self.state_data.get("max_workers", 1))))
        # Initialize the list of widgets targeted for translation (before building the UI)
        self._lang_widgets: list[tuple[object, str]] = []

        # Build the UI
        self._build_appbar()
        self._build_body()
        self._style_treeviews()
        self._apply_palette()
        # UI update schedule
        self.after(80, self._drain_ui_queue)
        # Adjust the layout based on screen size (prevent content clipping when window is too wide)
        self.after(200, lambda: self._adjust_layout(force=True))
        # Adjust column widths when the window size changes
        self.bind("<Configure>", self._on_resize)

        # Language configuration
        # If 'language' is missing from state_data, open the language selection dialog.
        self.language: str = self.state_data.get("language", "ko")  # default: Korean
        if "language" not in self.state_data:
            # Request language selection after the UI is fully built
            self.after(300, self._select_language)
        else:
            # If language is already set, immediately show the initial settings summary
            self.after(350, self._show_initial_settings)
        # Update UI text to match the current language
        self.after(360, self._update_ui_language)
        # Note: donation prompt will be shown after the initial settings summary
        # to avoid overlapping with language selection dialogs.
        # See _show_initial_settings where it is called.

    # 번역 메시지를 가져오는 도우미
    def _t_msg(self, key: str) -> str:
        try:
            return TR_MSG.get(key, {}).get(self.language, TR_MSG[key]["ko"])
        except Exception:
            return key

    def _register_lang(self, widget: object, key: str) -> None:
        """Register a widget whose text should be updated when the language changes."""
        self._lang_widgets.append((widget, key))

    def _update_ui_language(self) -> None:
        """Update the text of registered widgets according to the current language."""
        for widget, key in self._lang_widgets:
            try:
                # Some widgets can have their text set via configure
                widget.configure(text=self._t_msg(key))
            except Exception:
                try:
                # Treeview headings are updated separately
                    pass
                except Exception:
                    pass
        # Update tree headings
        try:
            # Preview tree headings
            self.tree_preview.heading("#0", text=self._t_msg("preview_tree"))
            self.tree_preview.heading("count", text=self._t_msg("file_count"))
            # Distribution summary tree
            self.tree_cam.heading("#0", text=self._t_msg("camera"))
            self.tree_cam.heading("cnt", text=self._t_msg("count"))
            self.tree_len.heading("#0", text=self._t_msg("lens"))
            self.tree_len.heading("cnt", text=self._t_msg("count"))
            # Duplicate list tree
            self.tree_conf.heading("dst", text=self._t_msg("existing_path"))
        except Exception:
            pass
        # Update card titles
        try:
            self.card_total.title_label.configure(text=self._t_msg("total_images"))
            self.card_dates.title_label.configure(text=self._t_msg("date_folders"))
            self.card_conf.title_label.configure(text=self._t_msg("pre_conflicts"))
            self.card_cam.title_label.configure(text=self._t_msg("camera_count"))
            self.card_len.title_label.configure(text=self._t_msg("lens_count"))
        except Exception:
            pass
        # Update various labels
        try:
            self._app_subtitle.configure(text=self._t_msg("app_subtitle"))
            self.lbl_work.configure(text=self._t_msg("task_title"))
            # Action buttons
            self.btn_pick.configure(text=self._t_msg("btn_pick"))
            self.btn_scan.configure(text=self._t_msg("btn_scan"))
            self.btn_start.configure(text=self._t_msg("btn_start"))
            self.btn_stop.configure(text=self._t_msg("btn_stop"))
            # Card titles
            self.lbl_dest_title.configure(text=self._t_msg("dest_card"))
            self.btn_change_dest.configure(text=self._t_msg("change"))
            self.lbl_dup_title.configure(text=self._t_msg("dup_policy_card"))
            # Radio buttons
            self.rdo_policy_rename.configure(text=self._t_msg("policy_rename"))
            self.rdo_policy_skip.configure(text=self._t_msg("policy_skip"))
            self.rdo_policy_ask.configure(text=self._t_msg("policy_ask"))
            # Options section
            self.lbl_group_title.configure(text=self._t_msg("group_title"))
            self.lbl_hier_title.configure(text=self._t_msg("hier_title"))
            self.sw_split.configure(text=self._t_msg("raw_split"))
            self.lbl_action_title.configure(text=self._t_msg("action_title"))
            self.sw_hash.configure(text=self._t_msg("hash_skip"))
            # Log section
            self.lbl_log_title.configure(text=self._t_msg("log"))
            # Update the selected folder label
            if hasattr(self, 'lbl_sd_path'):
                if self.src_dir:
                    self.lbl_sd_path.configure(text=f"{self._t_msg('selected_folder')}: {self.src_dir}")
                else:
                    self.lbl_sd_path.configure(text=f"{self._t_msg('selected_folder')}: {self._t_msg('not_selected')}")
        except Exception:
            pass

    def _show_donation_prompt(self) -> None:
        """Display a donation/support message at program start."""
        try:
            messagebox.showinfo(APP_NAME, self._t_msg("donation_prompt"), parent=self)
        except Exception:
            pass

    def _select_language(self) -> None:
        """Display the language selection dialog. Called on first run."""
        dlg = ctk.CTkToplevel(self)
        dlg.title(self._t_msg("select_language"))
        dlg.geometry("360x200")
        dlg.grab_set()
        # Instruction message
        ctk.CTkLabel(dlg, text=self._t_msg("select_language"), font=font_stack(16, "bold")).pack(pady=(20, 12))
        row = ctk.CTkFrame(dlg, fg_color="transparent")
        row.pack(pady=(0, 20))
        def _choose(lang: str) -> None:
        # Save the selected language and immediately apply it to the UI
            self.language = lang
            self.state_data["language"] = lang
            write_state(self.state_data)
            dlg.destroy()
            # UI 텍스트 갱신
            self._update_ui_language()
            # 초기 설정 요약을 바로 표시
            self._show_initial_settings()
        # Buttons for the two languages
        btn_en = ctk.CTkButton(row, text=self._t_msg("english"), width=120, command=lambda: _choose("en"))
        btn_ko = ctk.CTkButton(row, text=self._t_msg("korean"), width=120, command=lambda: _choose("ko"))
        btn_en.pack(side="left", padx=10)
        btn_ko.pack(side="left", padx=10)
        dlg.wait_window()

    def _show_initial_settings(self) -> None:
        """Display a summary of the current settings to the user."""
        try:
        # Build the settings summary string
            gb = self.group_by_var.get()
            hb = self.hier_var.get()
            split_raw = bool(self.split_var.get())
            action = self.action_var.get()
            skip_hash = bool(self.skip_hash_var.get())
            workers = max(1, int(self.workers_var.get()))
            # Duplicate policy
            pol = self.policy_var.get()
            summary_lines = []
            summary_lines.append(self._t_msg("settings_overview") + ":")
            summary_lines.append(f"• {self._t_msg('dest_folder') if 'dest_folder' in TR_MSG else 'Destination'}: {self.dest_root}")
            summary_lines.append(f"• {self._t_msg('group')}: {gb}")
            summary_lines.append(f"• {self._t_msg('hierarchy')}: {hb}")
            summary_lines.append(f"• {self._t_msg('split_raw')}: {split_raw}")
            summary_lines.append(f"• {self._t_msg('action')}: {action}")
            summary_lines.append(f"• {self._t_msg('skip_hash')}: {skip_hash}")
            summary_lines.append(f"• {self._t_msg('thread_count')}: {workers}")
            summary_lines.append(f"• {self._t_msg('duplicates_policy') if 'duplicates_policy' in TR_MSG else 'Duplicate policy'}: {pol}")
            messagebox.showinfo(APP_NAME, "\n".join(summary_lines), parent=self)
            # After displaying the settings summary, show the donation prompt.
            # We wrap in try/except to avoid interrupting the flow if the prompt fails.
            try:
                self._show_donation_prompt()
            except Exception:
                pass
        except Exception:
            pass

    def _adjust_layout(self, force: bool = True) -> None:
        """
        Adjust the initial window size based on screen resolution and scale settings,
        and resize treeview columns according to the current window width.

        When force=True, also adjust geometry (size and position) of the window.
        When force=False, only column widths are adjusted.
        """
        try:
            # Calculate screen dimensions and scaling
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            base_w, base_h = 1220, 800
            scale = float(self.state_data.get("scale", 1.0))
            target_w = int(base_w * scale)
            target_h = int(base_h * scale)
            # Restrict window size to at most 90% of the screen dimensions
            w = min(target_w, int(sw * 0.9))
            h = min(target_h, int(sh * 0.9))
            if force:
                # Center the window on the screen
                x = max(0, (sw - w) // 2)
                y = max(0, (sh - h) // 2)
                self.geometry(f"{w}x{h}+{x}+{y}")
            # Calculate available width for the right pane (left panel is fixed at 360)
            current_width = self.winfo_width() or w
            right_available = current_width - 360 - 18 - 12 - 16 - 16  # subtract padding, margins etc.
            if right_available > 0:
                conf_width = max(400, int(right_available * 0.7))
                try:
                    self.tree_conf.column("dst", width=conf_width)
                except Exception:
                    pass
                preview_width = max(300, int(right_available * 0.6))
                try:
                    self.tree_preview.column("#0", width=preview_width)
                except Exception:
                    pass
        except Exception:
            pass

    def _adjust_columns(self) -> None:
        """Adjust treeview column widths based on the current window width."""
        try:
            current_width = self.winfo_width()
            if not current_width:
                return
            right_available = current_width - 360 - 18 - 12 - 16 - 16
            if right_available > 0:
                conf_width = max(400, int(right_available * 0.7))
                try:
                    self.tree_conf.column("dst", width=conf_width)
                except Exception:
                    pass
                preview_width = max(300, int(right_available * 0.6))
                try:
                    self.tree_preview.column("#0", width=preview_width)
                except Exception:
                    pass
        except Exception:
            pass

    def _on_resize(self, event=None) -> None:
        """Adjust column widths when the window size changes."""
        self._adjust_columns()

    # ----- Styling -----
    def _apply_palette(self) -> None:
        mode = ctk.get_appearance_mode().lower()  # type: ignore
        pal = PALETTE.get(mode, PALETTE["light"])
        self.configure(fg_color=pal["bg"])
        for w in getattr(self, "_themed_frames", []):
            try:
                w.configure(fg_color=pal["elev"])
            except Exception:
                pass
        for w in getattr(self, "_subtle_frames", []):
            try:
                w.configure(fg_color=pal["subtle"])
            except Exception:
                pass
        try:
            self._appbar.configure(fg_color=pal["elev"])
            self._footer.configure(fg_color=pal["elev"])
        except Exception:
            pass
        for lbl in getattr(self, "_title_labels", []):
            try:
                lbl.configure(text_color=pal["text"])
            except Exception:
                pass
        for lbl in getattr(self, "_sub_labels", []):
            try:
                lbl.configure(text_color=pal["text_sub"])
            except Exception:
                pass

    def _style_treeviews(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Treeview.Heading",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            background="#E6E6EA",
            foreground="#222",
        )
        style.map("Treeview.Heading", relief=[("active", "flat")])
        style.configure(
            "Apple.Treeview",
            rowheight=28,
            font=("Segoe UI", 10),
            background="#FAFAFC",
            fieldbackground="#FAFAFC",
            bordercolor="#DDDEE3",
            lightcolor="#DDDEE3",
            darkcolor="#DDDEE3",
        )
        style.map(
            "Apple.Treeview",
            background=[("selected", "#D3E7FF")],
            foreground=[("selected", "#000000")],
            highlightthickness=[("!focus", 0)],
        )
        for tv in (self.tree_preview, self.tree_conf, self.tree_cam, self.tree_len):
            tv.configure(style="Apple.Treeview")

    # ----- UI Build -----
    def _build_appbar(self) -> None:
        pal = PALETTE[self.state_data.get("appearance", "light")]
        bar = ctk.CTkFrame(self, height=64, corner_radius=0, fg_color=pal["elev"])
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        self._appbar = bar
        title = ctk.CTkLabel(bar, text=APP_NAME, font=font_stack(20, "bold"))
        # Register subtitle so it can be updated when the language changes
        subtitle = ctk.CTkLabel(
            bar,
            text=self._t_msg("app_subtitle"),
            font=font_stack(12),
        )
        title.grid(row=0, column=0, sticky="w", padx=20, pady=(10, 0))
        subtitle.grid(row=1, column=0, sticky="w", padx=20, pady=(0, 10))
        # Register
        self._app_subtitle = subtitle
        self._register_lang(subtitle, "app_subtitle")
        right_wrap = ctk.CTkFrame(bar, fg_color="transparent")
        right_wrap.grid(row=0, column=1, rowspan=2, sticky="e", padx=12)
        self.appearance_opt = ctk.CTkSegmentedButton(
            right_wrap,
            values=["light", "dark"],
            command=self._toggle_appearance,
            width=132,
        )
        self.appearance_opt.set(self.state_data.get("appearance", "light"))
        self.appearance_opt.grid(row=0, column=0, padx=(0, 8), pady=10)
        # The settings button text will change according to the selected language
        self.btn_settings = ctk.CTkButton(right_wrap, text=self._t_msg("settings_title"), width=84, command=self._open_settings)
        self.btn_settings.grid(row=0, column=1, pady=10)
        self._register_lang(self.btn_settings, "settings_title")
        # Donate/support button that opens the Buy Me a Coffee link
        self.btn_donate = ctk.CTkButton(
            right_wrap,
            text=self._t_msg("donate_link"),
            width=120,
            command=lambda: webbrowser.open("https://buymeacoffee.com/modang"),
        )
        self.btn_donate.grid(row=0, column=2, pady=10)
        self._register_lang(self.btn_donate, "donate_link")
        sep = ctk.CTkFrame(self, height=1, corner_radius=0, fg_color=("#E8E8EA", "#2A2A2A"))
        sep.grid(row=0, column=0, sticky="ew", pady=(63, 0))

    def _build_body(self) -> None:
        mode = self.state_data.get("appearance", "light")
        pal = PALETTE[mode]
        # Create the main interface as a scrollable frame so that it can scroll vertically
        body = ctk.CTkScrollableFrame(self, corner_radius=16, fg_color=pal["bg"], orientation="vertical")
        # Configure the grid layout inside the scroll frame to place widgets
        body.grid(row=1, column=0, sticky="nsew", padx=18, pady=18)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(1, weight=1)
        # Left rail
        left = ctk.CTkFrame(body, width=360, corner_radius=16, fg_color=pal["elev"])
        left.grid(row=0, column=0, rowspan=2, sticky="nsw", padx=(16, 12), pady=16)
        left.grid_propagate(False)
        self._themed_frames = [left]
        self._subtle_frames = []
        self._title_labels = []
        self._sub_labels = []
        # Actions section
        # Task title
        self.lbl_work = ctk.CTkLabel(left, text=self._t_msg("task_title"), font=font_stack(16, "bold"))
        self.lbl_work.pack(anchor="w", padx=16, pady=(16, 6))
        self._title_labels.append(self.lbl_work)
        self._register_lang(self.lbl_work, "task_title")
        # Action buttons
        self.btn_pick = ctk.CTkButton(left, text=self._t_msg("btn_pick"), command=self.pick_sd, height=42)
        self.btn_scan = ctk.CTkButton(left, text=self._t_msg("btn_scan"), command=self.scan_preview, height=42, state="disabled")
        self.btn_start = ctk.CTkButton(left, text=self._t_msg("btn_start"), command=self.start_sort, height=46, state="disabled")
        # The stop button uses the danger color from the palette
        self.btn_stop = ctk.CTkButton(left, text=self._t_msg("btn_stop"), command=self.request_stop, fg_color=PALETTE[self.state_data.get("appearance", "light")]["danger"], hover_color=None, height=36, state="disabled")
        # Register each button for language updates
        for key, btn in [("btn_pick", self.btn_pick), ("btn_scan", self.btn_scan), ("btn_start", self.btn_start), ("btn_stop", self.btn_stop)]:
            self._register_lang(btn, key)
            btn.pack(fill="x", padx=16, pady=8)
        self.btn_pick.pack_configure(pady=(4, 8))
        self.btn_stop.pack_configure(pady=(8, 16))
        # Destination folder card
        card = ctk.CTkFrame(left, corner_radius=12, fg_color=pal["elev"])
        card.pack(fill="x", padx=16, pady=(0, 12))
        self._themed_frames.append(card)
        # Card title
        self.lbl_dest_title = ctk.CTkLabel(card, text=self._t_msg("dest_card"), font=font_stack(13, "bold"))
        self.lbl_dest_title.pack(anchor="w", padx=12, pady=(10, 4))
        self._title_labels.append(self.lbl_dest_title)
        self._register_lang(self.lbl_dest_title, "dest_card")
        # Display destination folder path
        self.lbl_dest = ctk.CTkLabel(card, text=str(self.dest_root), justify="left", wraplength=280, font=font_stack(12))
        self.lbl_dest.pack(anchor="w", padx=12, pady=(0, 8))
        # Change button
        self.btn_change_dest = ctk.CTkButton(card, text=self._t_msg("change"), width=88, command=self._change_dest)
        self.btn_change_dest.pack(anchor="e", padx=12, pady=(0, 10))
        self._register_lang(self.btn_change_dest, "change")
        # Duplicate handling policy card
        pol = ctk.CTkFrame(left, corner_radius=12, fg_color=pal["elev"])
        pol.pack(fill="x", padx=16, pady=(0, 12))
        self._themed_frames.append(pol)
        self.lbl_dup_title = ctk.CTkLabel(pol, text=self._t_msg("dup_policy_card"), font=font_stack(13, "bold"))
        self.lbl_dup_title.pack(anchor="w", padx=12, pady=(10, 4))
        self._title_labels.append(self.lbl_dup_title)
        self._register_lang(self.lbl_dup_title, "dup_policy_card")
        self.rdo_policy_rename = ctk.CTkRadioButton(pol, text=self._t_msg("policy_rename"), variable=self.policy_var, value="rename")
        self.rdo_policy_rename.pack(anchor="w", padx=14, pady=3)
        self._register_lang(self.rdo_policy_rename, "policy_rename")
        self.rdo_policy_skip = ctk.CTkRadioButton(pol, text=self._t_msg("policy_skip"), variable=self.policy_var, value="skip")
        self.rdo_policy_skip.pack(anchor="w", padx=14, pady=3)
        self._register_lang(self.rdo_policy_skip, "policy_skip")
        self.rdo_policy_ask = ctk.CTkRadioButton(pol, text=self._t_msg("policy_ask"), variable=self.policy_var, value="ask")
        self.rdo_policy_ask.pack(anchor="w", padx=14, pady=(3, 12))
        self._register_lang(self.rdo_policy_ask, "policy_ask")
        # Sorting and options card
        opt = ctk.CTkFrame(left, corner_radius=12, fg_color=pal["elev"])
        opt.pack(fill="x", padx=16, pady=(0, 12))
        self._themed_frames.append(opt)
        # Grouping criterion
        self.lbl_group_title = ctk.CTkLabel(opt, text=self._t_msg("group_title"), font=font_stack(13, "bold"))
        self.lbl_group_title.pack(anchor="w", padx=12, pady=(10, 4))
        self._register_lang(self.lbl_group_title, "group_title")
        gb = ctk.CTkSegmentedButton(opt, values=["camera", "lens"])
        gb.set(self.group_by_var.get())
        gb.pack(fill="x", padx=12)
        def _sync_gb(_=None):
            self.group_by_var.set(gb.get())
        gb._command = _sync_gb  # type: ignore
        # Folder hierarchy
        self.lbl_hier_title = ctk.CTkLabel(opt, text=self._t_msg("hier_title"), font=font_stack(13, "bold"))
        self.lbl_hier_title.pack(anchor="w", padx=12, pady=(12, 4))
        self._register_lang(self.lbl_hier_title, "hier_title")
        hb = ctk.CTkSegmentedButton(opt, values=["device-first", "date-first"])
        hb.set(self.hier_var.get())
        hb.pack(fill="x", padx=12)
        def _sync_hb(_=None):
            self.hier_var.set(hb.get())
        hb._command = _sync_hb  # type: ignore
        # RAW separation switch
        row_opt = ctk.CTkFrame(opt, fg_color="transparent")
        row_opt.pack(fill="x", padx=12, pady=(10, 10))
        self.sw_split = ctk.CTkSwitch(row_opt, text=self._t_msg("raw_split"), variable=self.split_var, onvalue=True, offvalue=False)
        self.sw_split.pack(anchor="w")
        self._register_lang(self.sw_split, "raw_split")
        # Action selection
        self.lbl_action_title = ctk.CTkLabel(opt, text=self._t_msg("action_title"), font=font_stack(13, "bold"))
        self.lbl_action_title.pack(anchor="w", padx=12, pady=(6, 4))
        self._register_lang(self.lbl_action_title, "action_title")
        act = ctk.CTkSegmentedButton(opt, values=["copy", "move"])
        act.set(self.action_var.get())
        act.pack(fill="x", padx=12)
        def _sync_act(_=None):
            self.action_var.set(act.get())
        act._command = _sync_act  # type: ignore
        # Hash duplication switch
        self.sw_hash = ctk.CTkSwitch(opt, text=self._t_msg("hash_skip"), variable=self.skip_hash_var, onvalue=True, offvalue=False)
        self.sw_hash.pack(anchor="w", padx=12, pady=(8, 12))
        self._register_lang(self.sw_hash, "hash_skip")
        # Log card
        lg = ctk.CTkFrame(left, corner_radius=12, fg_color=pal["elev"])
        lg.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self._themed_frames.append(lg)
        self.lbl_log_title = ctk.CTkLabel(lg, text=self._t_msg("log"), font=font_stack(13, "bold"))
        self.lbl_log_title.pack(anchor="w", padx=12, pady=(10, 4))
        self._register_lang(self.lbl_log_title, "log")
        self.log_text = ctk.CTkTextbox(lg, width=280, height=160)
        self.log_text.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        # Write a ready message in the selected language
        self._append_log(self._t_msg("ready"))
        # Right-hand area
        right = ctk.CTkFrame(body, corner_radius=16, fg_color=pal["elev"])
        right.grid(row=0, column=1, sticky="nsew", padx=(12, 16), pady=(16, 12))
        right.grid_columnconfigure(0, weight=1)
        # Assign row weights so that each section expands proportionally to the window size
        right.grid_rowconfigure(1, weight=2)  # The preview occupies more space due to its higher weight
        right.grid_rowconfigure(2, weight=1)  # Distribution summary
        right.grid_rowconfigure(3, weight=1)  # Duplicate list
        self._themed_frames.append(right)
        # Summary cards
        cards = ctk.CTkFrame(right, fg_color=pal["elev"])
        cards.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 10))
        cards.grid_columnconfigure((0, 1, 2, 3, 4), weight=1)
        self.card_total = self._metric_card(cards, self._t_msg("total_images"), "0")
        self.card_total.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.card_dates = self._metric_card(cards, self._t_msg("date_folders"), "0")
        self.card_dates.grid(row=0, column=1, sticky="ew", padx=8)
        self.card_conf = self._metric_card(cards, self._t_msg("pre_conflicts"), "0")
        self.card_conf.grid(row=0, column=2, sticky="ew", padx=8)
        self.card_cam = self._metric_card(cards, self._t_msg("camera_count"), "0")
        self.card_cam.grid(row=0, column=3, sticky="ew", padx=8)
        self.card_len = self._metric_card(cards, self._t_msg("lens_count"), "0")
        self.card_len.grid(row=0, column=4, sticky="ew", padx=(8, 0))
        # Preview area
        preview = ctk.CTkFrame(right, corner_radius=12, fg_color=pal["elev"])
        preview.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 12))
        preview.grid_columnconfigure(0, weight=1)
        preview.grid_rowconfigure(1, weight=1)
        self._themed_frames.append(preview)
        # Display selected folder
        self.lbl_sd_path = ctk.CTkLabel(preview, text=f"{self._t_msg('selected_folder')}: {self._t_msg('not_selected')}", anchor="w", font=font_stack(12))
        self.lbl_sd_path.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 4))
        # Do not specify height so the row expands automatically based on its weight
        self.tree_preview = ttk.Treeview(preview, columns=("count",), show="tree headings")
        # Set initial headings; they will be updated when the language changes
        self.tree_preview.heading("#0", text=self._t_msg("preview_tree"))
        self.tree_preview.heading("count", text=self._t_msg("file_count"))
        self.tree_preview.column("#0", width=520)
        self.tree_preview.column("count", width=120, anchor="center")
        self.tree_preview.grid(row=1, column=0, sticky="nsew", padx=12, pady=6)
        # Distribution section
        dist = ctk.CTkFrame(right, corner_radius=12, fg_color=pal["elev"])
        dist.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 12))
        dist.grid_columnconfigure((0, 1), weight=1)
        dist.grid_rowconfigure(1, weight=1)
        self._themed_frames.append(dist)
        self.lbl_dist_title = ctk.CTkLabel(dist, text=self._t_msg("dist_summary"), font=font_stack(13, "bold"))
        self.lbl_dist_title.grid(row=0, column=0, sticky="w", padx=12, pady=(12, 0))
        self._register_lang(self.lbl_dist_title, "dist_summary")
        self.tree_cam = ttk.Treeview(dist, columns=("cnt",), show="tree headings")
        self.tree_cam.heading("#0", text=self._t_msg("camera"))
        self.tree_cam.heading("cnt", text=self._t_msg("count"))
        self.tree_cam.column("#0", width=300)
        self.tree_cam.column("cnt", width=60, anchor="center")
        self.tree_cam.grid(row=1, column=0, sticky="nsew", padx=(12, 6), pady=(6, 12))
        self.tree_len = ttk.Treeview(dist, columns=("cnt",), show="tree headings")
        self.tree_len.heading("#0", text=self._t_msg("lens"))
        self.tree_len.heading("cnt", text=self._t_msg("count"))
        self.tree_len.column("#0", width=300)
        self.tree_len.column("cnt", width=60, anchor="center")
        self.tree_len.grid(row=1, column=1, sticky="nsew", padx=(6, 12), pady=(6, 12))
        # Duplicates section
        conflicts = ctk.CTkFrame(right, corner_radius=12, fg_color=pal["elev"])
        conflicts.grid(row=3, column=0, sticky="nsew", padx=16, pady=(0, 16))
        conflicts.grid_columnconfigure(0, weight=1)
        conflicts.grid_rowconfigure(1, weight=1)
        self._themed_frames.append(conflicts)
        self.lbl_conflict_title = ctk.CTkLabel(conflicts, text=self._t_msg("conflict_list"), font=font_stack(13, "bold"))
        self.lbl_conflict_title.grid(row=0, column=0, sticky="w", padx=12, pady=(12, 0))
        self._register_lang(self.lbl_conflict_title, "conflict_list")
        self.tree_conf = ttk.Treeview(conflicts, columns=("dst",), show="headings")
        self.tree_conf.heading("dst", text=self._t_msg("existing_path"))
        # Set a generous column width to ensure long paths are not truncated
        self.tree_conf.column("dst", anchor="w", width=1000)
        self.tree_conf.grid(row=1, column=0, sticky="nsew", padx=12, pady=(6, 12))
        # Progress bar and information
        footer = ctk.CTkFrame(self, corner_radius=0, fg_color=pal["elev"])
        footer.grid(row=2, column=0, sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        self._footer = footer
        self.progress = ctk.CTkProgressBar(footer, height=16)
        self.progress.set(0)
        self.progress.grid(row=0, column=0, sticky="ew", padx=20, pady=(10, 4))
        self.progress_info = ctk.CTkLabel(footer, text=self._t_msg("idle"), font=font_stack(12))
        self._register_lang(self.progress_info, "idle")
        self.progress_info.grid(row=1, column=0, sticky="w", padx=20, pady=(0, 12))

    # Helper to create a metric card widget
    def _metric_card(self, parent, title: str, value: str) -> ctk.CTkFrame:
        mode = ctk.get_appearance_mode().lower()  # type: ignore
        pal = PALETTE.get(mode, PALETTE["light"])
        f = ctk.CTkFrame(parent, corner_radius=12, fg_color=pal["elev"], border_width=1, border_color=pal["subtle"])
        ctk.CTkLabel(f, text=title, font=font_stack(12)).pack(anchor="w", padx=12, pady=(10, 0))
        lbl = ctk.CTkLabel(f, text=value, font=font_stack(22, "bold"))
        lbl.pack(anchor="w", padx=12, pady=(2, 10))
        f.value_label = lbl  # type: ignore
        return f

    def _set_card(self, card: ctk.CTkFrame, val: str | int) -> None:
        card.value_label.configure(text=str(val))  # type: ignore

    # ----- UI 액션 -----
    def _toggle_appearance(self, mode: str) -> None:
        ctk.set_appearance_mode(mode)  # type: ignore
        self.state_data["appearance"] = mode
        write_state(self.state_data)
        self._apply_palette()
        self._style_treeviews()

    def _change_dest(self) -> None:
        d = filedialog.askdirectory(title="복사/이동 대상 폴더 선택", initialdir=str(self.dest_root), parent=self)
        if not d:
            return
        self.dest_root = Path(d)
        self.lbl_dest.configure(text=str(self.dest_root))
        self.state_data["dest_root"] = str(self.dest_root)
        write_state(self.state_data)
        # Log destination change with language-neutral text
        self._append_log(f"{self._t_msg('dest_folder')} changed: {self.dest_root}")

    def _open_settings(self) -> None:
        # Settings dialog
        dlg = ctk.CTkToplevel(self)
        dlg.title(self._t_msg("settings_title"))
        dlg.geometry("600x500")
        dlg.grab_set()
        # Use a scrollable frame so all options are visible even on smaller screens
        scroll = ctk.CTkScrollableFrame(dlg, fg_color="transparent")
        scroll.pack(fill="both", expand=True)
        # Destination folder for copy/move operations
        ctk.CTkLabel(scroll, text=self._t_msg("dest_change_title"), font=font_stack(14, "bold")).pack(anchor="w", padx=18, pady=(18, 6))
        dest_row = ctk.CTkFrame(scroll)
        dest_row.pack(fill="x", padx=18)
        dest_label = ctk.CTkLabel(dest_row, text=str(self.dest_root), wraplength=420, justify="left")
        dest_label.pack(side="left", padx=(6, 6), pady=6)
        ctk.CTkButton(dest_row, text=self._t_msg("change"), width=84, command=lambda: self._settings_change_dest(dest_label)).pack(side="right", padx=6)
        # Default duplicate handling policy
        ctk.CTkLabel(scroll, text=self._t_msg("dup_default_policy"), font=font_stack(14, "bold")).pack(anchor="w", padx=18, pady=(16, 6))
        pol = ctk.CTkSegmentedButton(scroll, values=["rename", "skip", "ask"])
        pol.set(self.policy_var.get())
        pol.pack(fill="x", padx=18)
        # Language selection (English/Korean)
        ctk.CTkLabel(scroll, text=self._t_msg("select_language"), font=font_stack(14, "bold")).pack(anchor="w", padx=18, pady=(16, 6))
        lang_seg = ctk.CTkSegmentedButton(scroll, values=["en", "ko"])
        lang_seg.set(self.language)
        def _on_lang_change(*args):
            new_lang = lang_seg.get()
            self.language = new_lang
            self.state_data["language"] = new_lang
            write_state(self.state_data)
        # Refresh the UI
            self._update_ui_language()
        lang_seg.configure(command=_on_lang_change)
        lang_seg.pack(fill="x", padx=18)
        # Theme selection
        ctk.CTkLabel(scroll, text=self._t_msg("theme"), font=font_stack(14, "bold")).pack(anchor="w", padx=18, pady=(16, 6))
        ap = ctk.CTkSegmentedButton(scroll, values=["light", "dark"], command=self._toggle_appearance)
        ap.set(self.state_data.get("appearance", "light"))
        ap.pack(fill="x", padx=18)
        # Interface scaling
        ctk.CTkLabel(scroll, text=self._t_msg("interface_scale"), font=font_stack(14, "bold")).pack(anchor="w", padx=18, pady=(16, 6))
        # Scale slider and current value display
        scale_row = ctk.CTkFrame(scroll, fg_color="transparent")
        scale_row.pack(fill="x", padx=18, pady=(0, 8))
        scale_val_label = ctk.CTkLabel(scale_row, text=f"{self.state_data.get('scale', 1.0):.2f}", font=font_stack(12))
        # Slider can adjust between 0.8 and 1.5
        scale = ctk.CTkSlider(scale_row, from_=0.8, to=1.5, number_of_steps=14)
        scale.set(self.state_data.get("scale", 1.0))
        # Update preview and UI immediately when scale changes
        def _on_scale_change(val: float) -> None:
            v = round(float(val), 2)
            # Update the displayed scale value
            scale_val_label.configure(text=f"{v:.2f}")
            # Change widget scaling in real-time
            ctk.set_widget_scaling(v)  # type: ignore
            # Adjust layout before saving as well
            self.state_data["scale"] = v
            # Immediately adjust the layout on the next event loop
            self.after(10, lambda: self._adjust_layout(force=True))
        scale.configure(command=_on_scale_change)
        scale.pack(side="left", expand=True, fill="x")
        scale_val_label.pack(side="right")
        # Title for group/hierarchy/RAW separation/action/hash duplication options
        ctk.CTkLabel(scroll, text=self._t_msg("group_hier_options"), font=font_stack(14, "bold")).pack(anchor="w", padx=18, pady=(16, 6))
        row1 = ctk.CTkFrame(scroll)
        row1.pack(fill="x", padx=18)
        gb = ctk.CTkSegmentedButton(row1, values=["camera", "lens"])
        gb.set(self.group_by_var.get())
        gb.pack(side="left", expand=True, fill="x", padx=(0, 6))
        hb = ctk.CTkSegmentedButton(row1, values=["device-first", "date-first"])
        hb.set(self.hier_var.get())
        hb.pack(side="left", expand=True, fill="x", padx=(6, 6))
        act = ctk.CTkSegmentedButton(row1, values=["copy", "move"])
        act.set(self.action_var.get())
        act.pack(side="left", expand=True, fill="x", padx=(6, 0))
        sw_split = ctk.CTkSwitch(scroll, text=self._t_msg("raw_split"), variable=self.split_var, onvalue=True, offvalue=False)
        sw_split.pack(anchor="w", padx=18, pady=(6, 2))
        sw_hash = ctk.CTkSwitch(scroll, text=self._t_msg("hash_skip"), variable=self.skip_hash_var, onvalue=True, offvalue=False)
        sw_hash.pack(anchor="w", padx=18, pady=(2, 8))
        # Set the number of concurrent threads
        ctk.CTkLabel(scroll, text=self._t_msg("threads_count"), font=font_stack(14, "bold")).pack(anchor="w", padx=18, pady=(10, 4))
        import os
        max_cpu = max(1, (os.cpu_count() or 1))
        workers_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        workers_frame.pack(fill="x", padx=18, pady=(0, 8))
        slider_workers = ctk.CTkSlider(workers_frame, from_=1, to=max_cpu, number_of_steps=max_cpu - 1)
        slider_workers.set(self.workers_var.get())
        lbl_workers_val = ctk.CTkLabel(workers_frame, text=f"{self.workers_var.get()}개", font=font_stack(12))
        def _on_workers_change(val):
            v = int(round(val))
            if v < 1:
                v = 1
            self.workers_var.set(v)
            slider_workers.set(v)
            lbl_workers_val.configure(text=f"{v}개")
        slider_workers.configure(command=_on_workers_change)
        slider_workers.pack(side="left", expand=True, fill="x", padx=(0, 6))
        lbl_workers_val.pack(side="right")
        # Donation/support link
        ctk.CTkLabel(scroll, text="", height=4).pack()  # small spacer
        btn_donate_set = ctk.CTkButton(
            scroll,
            text=self._t_msg("donate_link"),
            width=200,
            command=lambda: webbrowser.open("https://buymeacoffee.com/modang"),
        )
        btn_donate_set.pack(anchor="w", padx=18, pady=(4, 12))
        self._register_lang(btn_donate_set, "donate_link")

        # Save and close buttons
        row_btn = ctk.CTkFrame(scroll)
        row_btn.pack(fill="x", padx=18, pady=16)
        def _save() -> None:
            self.policy_var.set(pol.get())
            self.state_data["policy"] = pol.get()
            self.state_data["scale"] = round(scale.get(), 2)
            ctk.set_widget_scaling(self.state_data["scale"])  # type: ignore
            self.state_data["group_by"] = gb.get()
            self.group_by_var.set(gb.get())
            self.state_data["hierarchy"] = hb.get()
            self.hier_var.set(hb.get())
            self.state_data["action"] = act.get()
            self.action_var.set(act.get())
            self.state_data["split_raw_jpg"] = bool(self.split_var.get())
            self.state_data["skip_hash_dup"] = bool(self.skip_hash_var.get())
            self.state_data["max_workers"] = max(1, int(self.workers_var.get()))
            write_state(self.state_data)
            dlg.destroy()
            # 저장 후 레이아웃과 언어 갱신
            self.after(50, lambda: self._adjust_layout(force=True))
            self.after(60, self._update_ui_language)
            self._append_log(
                f"[설정] 정책:{self.state_data['policy']} / 테마:{self.state_data['appearance']} / 스케일:{self.state_data['scale']} / "
                f"그룹:{self.state_data['group_by']} / 계층:{self.state_data['hierarchy']} / 동작:{self.state_data['action']} / "
                f"분리:{self.state_data['split_raw_jpg']} / 해시:{self.state_data['skip_hash_dup']}"
            )
        ctk.CTkButton(row_btn, text=self._t_msg("save"), width=84, command=_save).pack(side="right", padx=(8, 0))
        ctk.CTkButton(row_btn, text=self._t_msg("close"), width=84, command=dlg.destroy).pack(side="right", padx=(0, 8))
        # Folder tree example
        ctk.CTkLabel(scroll, text=self._t_msg("folder_tree_example"), font=font_stack(14, "bold")).pack(anchor="w", padx=18, pady=(8, 4))
        example_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        example_frame.pack(fill="x", padx=18, pady=(0, 12))
        example_label = ctk.CTkLabel(
            example_frame,
            text="",
            justify="left",
            wraplength=520,
        )
        example_label.pack(anchor="w")
        def _update_example(*args):
            # Generate a folder tree example using sample values
            group_val = gb.get()
            hier_val = hb.get()
            split_raw = bool(self.split_var.get())
            dummy_group = "Camera" if group_val == "camera" else "Lens"
            year = datetime.now().strftime("%Y")
            month = datetime.now().strftime("%Y-%m")
            date = datetime.now().strftime("%Y-%m-%d")
            if hier_val == "device-first":
                path_parts = [str(self.dest_root), dummy_group, year, month, date]
            else:
                path_parts = [str(self.dest_root), year, month, date, dummy_group]
            if split_raw:
                path_parts.append("raw")
            example_path = os.path.join(*path_parts)
            example_label.configure(text=example_path)
        _update_example()
        # Update the example when options change via event bindings
        gb.configure(command=lambda *_: (_sync_gb(), _update_example()))  # type: ignore
        hb.configure(command=lambda *_: (_sync_hb(), _update_example()))  # type: ignore
        act.configure(command=lambda *_: (_sync_act()))  # 유지
        def _on_split_change():
            _update_example()
        sw_split.configure(command=lambda *_: (_on_split_change()))

    def _settings_change_dest(self, label_widget) -> None:
        d = filedialog.askdirectory(title="Select destination folder for copy/move", initialdir=str(self.dest_root), parent=self)
        if not d:
            return
        self.dest_root = Path(d)
        label_widget.configure(text=str(self.dest_root))
        self.lbl_dest.configure(text=str(self.dest_root))
        self.state_data["dest_root"] = str(self.dest_root)
        write_state(self.state_data)
        # Log destination change with language-neutral text
        self._append_log(f"[Settings] {self._t_msg('dest_folder')} changed: {self.dest_root}")

    def pick_sd(self) -> None:
        d = filedialog.askdirectory(title="Select folder to organize (SD card or regular folder)", parent=self)
        if not d:
            return
        self.src_dir = Path(d)
        self.lbl_sd_path.configure(text=f"{self._t_msg('selected_folder')}: {self.src_dir}")
        self._append_log(f"Folder selected: {self.src_dir}")
        self.btn_scan.configure(state="normal")
        self.btn_start.configure(state="disabled")
        self.progress.set(0)
        self.progress_info.configure(text=self._t_msg('idle'))
        for t in (self.tree_preview, self.tree_conf, self.tree_cam, self.tree_len):
            for i in t.get_children():
                t.delete(i)
        for card in (self.card_total, self.card_dates, self.card_conf, self.card_cam, self.card_len):
            self._set_card(card, 0)

    # 계획
    def _target_dir_for(self, meta: dict) -> Path:
        group = meta["camera"] if self.group_by_var.get() == "camera" else meta["lens"]
        group = sanitize(group)
        # Build a three-level folder structure: year/month/date
        if self.hier_var.get() == "device-first":
            base = self.dest_root / group / meta["year"] / meta["month"] / meta["date"]
        else:
            base = self.dest_root / meta["year"] / meta["month"] / meta["date"] / group
        if self.split_var.get():
            base = base / meta["kind"]
        return base

    def scan_preview(self) -> None:
        if not self.src_dir or not self.src_dir.exists():
            # Display an error if the source folder is invalid
            messagebox.showerror(APP_NAME, self._t_msg("error_invalid_folder"), parent=self)
            return
        try:
            self.dest_root.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            # Build a destination creation error message in the current language
            if self.language == "ko":
                msg = f"대상 폴더 생성 실패:\n{self.dest_root}\n{e}"
            else:
                msg = f"Failed to create destination folder:\n{self.dest_root}\n{e}"
            messagebox.showerror(APP_NAME, msg, parent=self)
            return
        self.files = list(walk_images(self.src_dir))
        if not self.files:
            # Inform the user if no image files were found
            messagebox.showinfo(APP_NAME, self._t_msg("error_no_images"), parent=self)
            return
        # Collect metadata
        # Use the specified number of worker threads to extract metadata in parallel
        self.metas = []
        cam_counts: dict[str, int] = {}
        len_counts: dict[str, int] = {}
        dateset: set[str] = set()
        workers = max(1, int(self.workers_var.get()))
        # Perform extract_meta in parallel; use sequential processing if there are few files or only one worker
        if workers > 1 and len(self.files) > 1:
            import concurrent.futures
                # Limit the maximum number of workers to avoid oversubscription
            max_workers = min(workers, os.cpu_count() or 1)
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    # Use enumerate to periodically update progress in the event loop
                    for idx, m in enumerate(executor.map(extract_meta, self.files)):
                        self.metas.append(m)
                        cam_counts[m["camera"]] = cam_counts.get(m["camera"], 0) + 1
                        len_counts[m["lens"]] = len_counts.get(m["lens"], 0) + 1
                        dateset.add(m["date"])
                        # Periodically update the UI
                        if idx % 50 == 0:
                            self.update_idletasks()
            except Exception:
                # Fall back to sequential processing if errors occur during parallel extraction
                self.metas.clear()
                cam_counts.clear(); len_counts.clear(); dateset.clear()
                for idx, p in enumerate(self.files):
                    m = extract_meta(p)
                    self.metas.append(m)
                    cam_counts[m["camera"]] = cam_counts.get(m["camera"], 0) + 1
                    len_counts[m["lens"]] = len_counts.get(m["lens"], 0) + 1
                    dateset.add(m["date"])
                    if idx % 50 == 0:
                        self.update_idletasks()
        else:
            # Sequential processing
            for idx, p in enumerate(self.files):
                m = extract_meta(p)
                self.metas.append(m)
                cam_counts[m["camera"]] = cam_counts.get(m["camera"], 0) + 1
                len_counts[m["lens"]] = len_counts.get(m["lens"], 0) + 1
                dateset.add(m["date"])
                if idx % 50 == 0:
                    self.update_idletasks()
        # Build the plan
        self.plan.clear()
        self.conflicts.clear()
        for m in self.metas:
            out_dir = self._target_dir_for(m)
            self.plan.setdefault(out_dir, []).append(m["path"])
        # Scan for pre-existing duplicates
        for out_dir, flist in self.plan.items():
            for src in flist:
                dst = out_dir / src.name
                if dst.exists():
                    self.conflicts.append((src, dst))
        # Fill the preview tree
        for i in self.tree_preview.get_children():
            self.tree_preview.delete(i)
        # For tree_preview, show only top-level group, using dest_root-relative paths instead of full paths
        by_top: dict[str, int] = {}
        for tdir, srcs in self.plan.items():
            try:
                # Group by the first path element under dest_root
                rel = tdir.relative_to(self.dest_root)
                top = rel.parts[0] if rel.parts else str(tdir)
            except Exception:
                top = str(tdir)
            by_top[top] = by_top.get(top, 0) + len(srcs)
        for top, cnt in sorted(by_top.items()):
            self.tree_preview.insert("", "end", text=top, values=(cnt,))
        # Fill distribution summary
        for tv in (self.tree_cam, self.tree_len):
            for i in tv.get_children():
                tv.delete(i)
        for k, v in sorted(cam_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            self.tree_cam.insert("", "end", text=k, values=(v,))
        for k, v in sorted(len_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            self.tree_len.insert("", "end", text=k, values=(v,))
        # Populate duplicate list
        for i in self.tree_conf.get_children():
            self.tree_conf.delete(i)
        for _, dst in self.conflicts:
            # Display duplicate paths relative to dest_root
            try:
                rel_dst = dst.relative_to(self.dest_root)
                self.tree_conf.insert("", "end", values=(str(rel_dst),))
            except Exception:
                self.tree_conf.insert("", "end", values=(str(dst),))
        # Update summary cards
        self._set_card(self.card_total, human(len(self.files)))
        self._set_card(self.card_dates, human(len(dateset)))
        self._set_card(self.card_conf, human(len(self.conflicts)))
        self._set_card(self.card_cam, human(len(cam_counts)))
        self._set_card(self.card_len, human(len(len_counts)))
        # Compute effective thread count (1 if 'ask' policy is selected)
        _policy = self.policy_var.get()
        eff_workers = max(1, int(self.workers_var.get()))
        if _policy == "ask":
            eff_workers = 1
        # Show up to 20 sample folder paths relative to dest_root
        sample_lines: list[str] = []
        preview_targets = sorted(list(self.plan.keys()))[:20]
        for p in preview_targets:
            try:
                rel = p.relative_to(self.dest_root)
                sample_lines.append(f"- {rel}")
            except Exception:
                sample_lines.append(f"- {p}")
        preview_text = "\n".join(sample_lines)
        # Compose a summary message in the selected language
        lines: list[str] = []
        lines.append(self._t_msg("scan_complete"))
        lines.append(
            f"• {self._t_msg('group')}: {self.group_by_var.get()} / {self._t_msg('hierarchy')}: {self.hier_var.get()} / "
            f"{self._t_msg('split_raw')}: {bool(self.split_var.get())}"
        )
        lines.append(
            f"• {self._t_msg('action')}: {self.action_var.get()} / {self._t_msg('skip_hash')}: {bool(self.skip_hash_var.get())}"
        )
        lines.append(f"• {self._t_msg('thread_count')}: {eff_workers}")
        lines.append(
            f"• {self._t_msg('total_files')}: {len(self.files)} / {self._t_msg('planned_folders')}: {len(self.plan)} / "
            f"{self._t_msg('pre_duplicates')}: {len(self.conflicts)}"
        )
        lines.append("")
        lines.append(self._t_msg("examples"))
        lines.append(preview_text + ("\n…" if len(self.plan) > 20 else ""))
        lines.append("")
        lines.append(self._t_msg("press_start"))
        summary = "\n".join(lines)
        # Display as an informational message (without Yes/No buttons)
        try:
            messagebox.showinfo(APP_NAME, summary, parent=self)
        except Exception:
            pass
        # Update progress info label reflecting the current language
        try:
            self.progress_info.configure(text=self._t_msg("preview_done"))
        except Exception:
            pass
        # Enable the Start button
        self.btn_start.configure(state="normal")
        # Write to log using translated prompt
        self._append_log(self._t_msg("preview_prompt"))

    def request_stop(self) -> None:
        self.stop_flag.set()
        # Log a cancel request
        self._append_log(self._t_msg("cancel_requested"))

    def start_sort(self) -> None:
        if not self.metas:
            return
        self.stop_flag.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.btn_scan.configure(state="disabled")
        self.btn_pick.configure(state="disabled")
        t = threading.Thread(target=self._worker_sort, daemon=True)
        t.start()

    def _ask_conflict(self, src: Path, dst: Path) -> str:
        result = "rename"
        dlg = ctk.CTkToplevel(self)
        # Title uses translated duplicate processing message
        dlg.title(self._t_msg("duplicate_processing"))
        dlg.geometry("600x260")
        dlg.grab_set()
        ctk.CTkLabel(dlg, text=self._t_msg("duplicate_exists"), font=font_stack(16, "bold")).pack(anchor="w", padx=20, pady=(20, 8))
        ctk.CTkLabel(dlg, text=f"{self._t_msg('selected_folder')}: {src}", wraplength=560, justify="left").pack(anchor="w", padx=20, pady=2)
        ctk.CTkLabel(dlg, text=f"{self._t_msg('existing_path')}: {dst}", wraplength=560, justify="left").pack(anchor="w", padx=20, pady=2)
        def _do(choice: str) -> None:
            nonlocal result
            result = choice
            dlg.destroy()
        btns = ctk.CTkFrame(dlg)
        btns.pack(fill="x", padx=20, pady=18)
        ctk.CTkButton(btns, text=self._t_msg("rename_new"), command=lambda: _do("rename")).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btns, text=self._t_msg("skip_btn"), command=lambda: _do("skip")).pack(side="left", padx=8)
        ctk.CTkButton(btns, text=self._t_msg("cancel_all_btn"), fg_color="#8a1c1c", hover_color="#6c1414", command=lambda: _do("cancel")).pack(side="right", padx=(8, 0))
        dlg.wait_window()
        return result

    def _worker_sort(self) -> None:
        total = len(self.metas)
        start_ts = time.time()
        # Shared counters
        done = 0
        success = 0
        skipped = 0
        failed = 0
        # Store messages when errors occur
        errors: list[str] = []
        # Store a list of skipped files
        skipped_list: list[str] = []
        policy = self.policy_var.get()
        action = self.action_var.get()
        skip_hash = bool(self.skip_hash_var.get())
        # Determine worker thread count (force 1 for 'ask' policy)
        workers = max(1, int(self.workers_var.get()))
        if policy == "ask" and workers > 1:
            workers = 1  # disable parallelism for interactive duplicate handling
        lock = threading.Lock()

        def process(m):
            nonlocal done, success, skipped, failed
            if self.stop_flag.is_set():
                return
            src = m["path"]
            try:
                out_dir = self._target_dir_for(m)
                out_dir.mkdir(parents=True, exist_ok=True)
                dst = out_dir / src.name
                # Duplicate handling
                if dst.exists():
                    # Compare content hashes; skip if file contents are identical
                    if skip_hash and file_hash(src) == file_hash(dst):
                        with lock:
                            skipped += 1
                            done += 1
                            skipped_list.append(str(src))
                        # Perform status updates at regular intervals only
                        if done <= 10 or done % max(1, total // 100) == 0 or done == total:
                            self._enqueue_status(done, total, start_ts, f"{self._t_msg('skip_same_content')}: {src.name}")
                        return
                    # Branch according to duplicate filename handling policy
                    if policy == "rename":
                        # Save using a new name
                        dst = unique_dest(out_dir, src.name)
                    elif policy == "skip":
                        # Skip the file and add to the skipped list
                        with lock:
                            skipped += 1
                            done += 1
                            skipped_list.append(str(src))
                        if done <= 10 or done % max(1, total // 100) == 0 or done == total:
                            self._enqueue_status(done, total, start_ts, f"{self._t_msg('skip_duplicate_name')}: {src.name}")
                        return
                    else:
                        # 'ask' policy: prompt the user (called only in single-threaded mode)
                        resp = self._ask_conflict(src, dst)
                        if resp == "cancel":
                            # Cancel all processing
                            self.stop_flag.set()
                            return
                        elif resp == "skip":
                            with lock:
                                skipped += 1
                                done += 1
                                skipped_list.append(str(src))
                            if done <= 10 or done % max(1, total // 100) == 0 or done == total:
                                self._enqueue_status(done, total, start_ts, f"{self._t_msg('skip_user')}: {src.name}")
                            return
                        else:
                            # Save using a new name based on user selection
                            dst = unique_dest(out_dir, src.name)
                # Copy or move the file
                if action == "move":
                    shutil.move(str(src), str(dst))  # type: ignore
                else:
                    shutil.copy2(str(src), str(dst))  # type: ignore
                with lock:
                    success += 1
                    done += 1
                self._enqueue_status(done, total, start_ts, f"{self._t_msg('processed_file')}: {dst.name}")
            except Exception as e:
                with lock:
                    failed += 1
                    done += 1
                err_msg = f"{src} : {e}"
                errors.append(err_msg)
                # Write failure information to the log using translated prefix
                log_write(f"{self._t_msg('failure_prefix')}: {src} / {e}\n{traceback.format_exc()}")
                # Progress updates are performed only at intervals (checked in the outer function)
                if done <= 10 or done % max(1, total // 100) == 0 or done == total:
                    self._enqueue_status(done, total, start_ts, f"{self._t_msg('failure_prefix')}: {src.name}")

        # Parallel execution
        if workers > 1:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(process, m) for m in self.metas]
                # wait for completion
                for f in futures:
                    # Terminate if stop_flag is set in the main thread
                    if self.stop_flag.is_set():
                        break
                    try:
                        f.result()
                    except Exception:
                        pass
        else:
            # Sequential execution
            for m in self.metas:
                if self.stop_flag.is_set():
                    break
                process(m)
        elapsed = time.time() - start_ts
        # Include error and skipped lists in the finish event payload
        self.ui_queue.put(("finish", {
            "total": total,
            "success": success,
            "skipped": skipped,
            "failed": failed,
            "elapsed": elapsed,
            "errors": errors,
            "skipped_list": skipped_list,
        }))

    def _enqueue_status(self, done: int, total: int, start_ts: float, line: str) -> None:
        elapsed = time.time() - start_ts
        speed = done / elapsed if elapsed > 0 else 0
        eta = (total - done) / speed if speed > 0 else 0
        self.ui_queue.put(("status", {"done": done, "total": total, "elapsed": elapsed, "eta": eta, "line": line}))

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "status":
                    done, total = payload["done"], payload["total"]
                    frac = done / total if total else 0
                    self.progress.set(frac)
                    self.progress_info.configure(
                        text=(
                            f"{done}/{total}  ({frac*100:0.1f}%)   "
                            f"속도: {done / max(payload['elapsed'], 1e-6):0.2f} 파일/초   "
                            f"ETA: {payload['eta']:0.1f}s   |  {payload['line']}"
                        )
                    )
                    self._append_log(payload["line"])
                elif kind == "finish":
                    # Handle completion of sorting task
                    total = payload.get("total", 0)
                    success = payload.get("success", 0)
                    skipped_cnt = payload.get("skipped", 0)
                    failed_cnt = payload.get("failed", 0)
                    elapsed = payload.get("elapsed", 0.0)
                    errors_list = payload.get("errors", [])
                    skipped_list = payload.get("skipped_list", [])
                    # Update the progress bar and summary text
                    self.progress.set(1 if total else 0)
                    # Summary text changes based on the selected language
                    if self.language == "en":
                        self.progress_info.configure(
                            text=f"Finished: {self._t_msg('success_count')} {success} / {self._t_msg('skipped_count')} {skipped_cnt} / {self._t_msg('errors_count')} {failed_cnt}  (elapsed {elapsed:0.1f}s)"
                        )
                    else:
                        self.progress_info.configure(
                            text=f"완료: 성공 {success} / 스킵 {skipped_cnt} / 실패 {failed_cnt}  (경과 {elapsed:0.1f}s)"
                        )
                    # Restore button states
                    self.btn_stop.configure(state="disabled")
                    self.btn_scan.configure(state="normal")
                    self.btn_pick.configure(state="normal")
                    # Write entry to the log
                    if self.language == "en":
                        self._append_log("Sorting finished.")
                    else:
                        self._append_log("정리 완료.")
                    # Display a summary of skipped files and errors to the user
                    try:
                        lines: list[str] = []
                        # Basic summary
                        lines.append(f"{self._t_msg('sort_completed')}")
                        lines.append(
                            f"{self._t_msg('success_count')}: {success}, {self._t_msg('skipped_count')}: {skipped_cnt}, {self._t_msg('errors_count')}: {failed_cnt}"
                        )
                        # List of skipped files
                        if skipped_cnt:
                            lines.append("")
                            lines.append(self._t_msg("skip_list"))
                            max_show = 20
                            for idx, p in enumerate(skipped_list[:max_show]):
                                lines.append(f"{idx+1}. {p}")
                            if skipped_cnt > max_show:
                                lines.append(self._t_msg("only_partial"))
                        # List of errors
                        if failed_cnt:
                            lines.append("")
                            lines.append(self._t_msg("error_list"))
                            max_show = 20
                            for idx, e in enumerate(errors_list[:max_show]):
                                lines.append(f"{idx+1}. {e}")
                            if failed_cnt > max_show:
                                lines.append(self._t_msg("only_partial"))
                        messagebox.showinfo(APP_NAME, "\n".join(lines), parent=self)
                    except Exception:
                        pass
        except queue.Empty:
            pass
        self.after(80, self._drain_ui_queue)

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def main() -> None:
    try:
        dest = Path(read_state().get("dest_root") or DEFAULT_DEST)
        dest.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        messagebox.showerror(APP_NAME, f"대상 폴더 생성 실패:\n{dest}\n{e}")  # type: ignore
        sys.exit(1)
    log_write("=== 프로그램 시작 ===")
    app = PhotoSorterApp()
    app.mainloop()
    log_write("=== 프로그램 종료 ===")


if __name__ == "__main__":
    main()