# sd to c sort

`sd to c sort` is a cross-platform Python application that helps you sort and organise photographs from memory cards or folders.
Images are grouped by camera or lens, arranged in a year/month/day folder hierarchy, optionally separated by RAW/JPG types, and copied or moved to a destination of your choice.

## Features

- Group by camera or lens
- Year / Month / Day folder structure (for example: `2025/2025-08/2025-08-24`)
- RAW/JPG separation option
- Duplicate handling (skip, rename, or ask every time)
- Modern UI with light and dark themes inspired by YouTube
- Language support: English and Korean
- Detailed summary after scan (before sorting starts)
- Error and skip reports (shows which files failed or were skipped, with full paths)

## Requirements

Install dependencies:

```bash
pip install -r requirements.txt
```

Dependencies:
- customtkinter
- Pillow
- exifread

## Usage

Run the application:

```bash
python sd_to_c_sort.py
```

Steps:
1. Choose the source folder (for example, your SD card).
2. Preview how the files will be sorted.
3. Confirm and start the sorting process.

## Build Executable (Windows)

Using PyInstaller:

```bash
pyinstaller --noconfirm --onefile --windowed sd_to_c_sort.py
```

The executable will be created under the `dist/` directory.

More detailed build instructions are available in `build_instructions.md`.

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.

## Support Development

If you find this tool helpful, please consider supporting development:
https://buymeacoffee.com/modang
