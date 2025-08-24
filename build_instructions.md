# PhotoSorterPro 실행 파일 빌드 가이드

이 문서는 `sd_to_c_sort.py` 스크립트를 Windows 실행 파일로 패키징하기 위해 필요한 환경 설정과 빌드 과정을 설명합니다.

## 1. 의존성 설치

먼저 파이썬이 설치된 상태에서 필요한 라이브러리를 설치해야 합니다. 프로젝트 루트에 있는 `requirements.txt` 파일을 사용하여 다음과 같이 설치할 수 있습니다.

```sh
pip install -r requirements.txt
```

`customtkinter`는 표준 Tkinter에 기반한 모던 GUI 프레임워크이며, Pillow와 exifread는 사진의 메타데이터를 읽어오기 위해 사용됩니다.

또한 EXIF 정보를 정확하게 읽어오려면 [ExifTool](https://exiftool.org) 프로그램이 시스템 PATH에 있어야 합니다. 설치하지 않아도 프로그램은 기본적인 기능을 수행하지만, 카메라·렌즈 정보가 일부 누락될 수 있습니다.

## 2. 빌드 도구 설치

실행 파일 생성에는 [PyInstaller](https://www.pyinstaller.org/)를 사용하는 것을 권장합니다. 다음 명령으로 PyInstaller를 설치합니다.

```sh
pip install pyinstaller
```

## 3. 실행 파일 만들기

프로젝트 디렉터리에서 다음 명령을 실행하여 콘솔을 표시하지 않는 단일 실행 파일을 생성할 수 있습니다. 출력 파일은 `dist/sd_to_c_sort.exe`로 생성됩니다.

```sh
pyinstaller --noconsole --onefile --name sd_to_c_sort sd_to_c_sort.py
```

빌드 과정 중 추가 데이터나 리소스 파일이 필요하지 않으므로 `--add-data` 옵션은 사용하지 않았습니다. 만약 실행 시 아이콘을 지정하려면 `--icon <ico파일>` 옵션을 추가할 수 있습니다.

## 4. 실행 및 배포

빌드가 완료되면 `dist` 디렉터리 안에 있는 `sd_to_c_sort.exe` 파일을 실행하여 프로그램을 사용할 수 있습니다. 실행 파일은 외부 라이브러리와 함께 패키징되지만, 사진의 메타데이터를 정확히 분석하려면 시스템에 ExifTool이 설치되어 있어야 함을 유의하세요.

## 5. 참고사항

- 이 스크립트는 사용자 설정을 `%APPDATA%\sd_to_c_sort` 디렉터리에 저장합니다. 실행 파일로 패키징하더라도 동일한 위치에 설정과 로그가 기록됩니다.
- 대용량 이미지 폴더를 처리할 때는 동시 처리 스레드 수를 조절하여 시스템 자원을 효율적으로 사용할 수 있습니다.