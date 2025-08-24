# sd to c sort

## English

`sd to c sort` is a cross-platform Python application that helps you sort and organise photographs from memory cards or folders.
Images are grouped by camera or lens, arranged in a year/month/day folder hierarchy, optionally separated by RAW/JPG types, and copied or moved to a destination of your choice.

### Features
- Group by camera or lens
- Year / Month / Day folder structure (for example: `2025/2025-08/2025-08-24`)
- RAW/JPG separation option
- Duplicate handling (skip, rename, or ask every time)
- Modern UI with light and dark themes inspired by YouTube
- Language support: English and Korean
- Detailed summary after scan
- Error and skip reports with full paths

### Requirements

```bash
pip install -r requirements.txt
```

Dependencies:
- customtkinter
- Pillow
- exifread

### Usage

```bash
python sd_to_c_sort.py
```

Steps:
1. Choose the source folder (for example, your SD card).
2. Preview how the files will be sorted.
3. Confirm and start the sorting process.

### Build Executable (Windows)

Using PyInstaller:

```bash
pyinstaller --noconfirm --onefile --windowed sd_to_c_sort.py
```

The executable will be created under the `dist/` directory.

More detailed build instructions are available in `build_instructions.md`.

---

## 한국어

`sd to c sort`는 SD 카드나 일반 폴더에 있는 사진들을 체계적으로 정리해 주는 크로스 플랫폼 파이썬 프로그램입니다.  
사진을 카메라 모델이나 렌즈 모델별로 분류할 수 있으며, 연/월/일 단위 폴더 구조로 정리됩니다. RAW와 JPG 파일을 분리 저장하는 옵션도 지원하며, 중복 파일 처리(건너뛰기/이름 변경/매번 물어보기) 기능을 제공합니다.

### 주요 기능
- 카메라 또는 렌즈 기준 분류
- 연/월/일 3단계 폴더 구조 (예: `2025/2025-08/2025-08-24`)
- RAW/JPG 파일 분리 옵션
- 중복 파일 처리 방식 선택
- YouTube 스타일의 현대적인 UI (라이트/다크 테마 지원)
- 영어 및 한국어 지원
- 정리 시작 전에 상세 미리보기 제공
- 실패/건너뛴 파일 목록 표시

### 요구 사항

```bash
pip install -r requirements.txt
```

필요한 라이브러리:
- customtkinter
- Pillow
- exifread

### 사용 방법

```bash
python sd_to_c_sort.py
```

단계:
1. 소스 폴더 선택 (예: SD 카드).
2. 정리될 파일 구조 미리보기.
3. 확인 후 정리 시작.

### 실행 파일 빌드 (Windows)

PyInstaller 사용:

```bash
pyinstaller --noconfirm --onefile --windowed sd_to_c_sort.py
```

생성된 실행 파일은 `dist/` 폴더에 저장됩니다.

자세한 빌드 방법은 `build_instructions.md`를 참고하세요.

---

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.

## Support Development

If you find this tool helpful, please consider supporting development:  
https://buymeacoffee.com/modang
