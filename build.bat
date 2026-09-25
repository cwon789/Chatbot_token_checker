@echo off
REM Windows 로컬 빌드 (uv 가상환경). 결과: dist\TokenWidget.exe (콘솔 없음, 이 파일 하나만 배포하면 됨)
REM 사전: uv 설치 (https://docs.astral.sh/uv/). 그 외 파이썬/패키지 사전설치 불필요.
REM 사내망(SSL 검사)에서도 되도록 uv 가 Windows 인증서 저장소를 쓰게 한다. 프록시는 HTTPS_PROXY 를 따른다.

cd /d "%~dp0"
set UV_NATIVE_TLS=1

echo [1/3] uv 가상환경 생성...
uv venv .venv --clear --python 3.10 || goto :err

echo [2/3] PyInstaller · Pillow · pystray 설치...
uv pip install --python .venv pyinstaller pillow pystray truststore || goto :err

echo [3/3] 빌드...
uv run --python .venv pyinstaller --onefile --noconsole --name TokenWidget --clean --noconfirm --icon icons/tokenwidget.png --add-data "icons/tokenwidget.png;icons" ccusage_widget.py || goto :err

echo.
echo 완료: dist\TokenWidget.exe
goto :eof

:err
echo.
echo 빌드 실패. 위 오류를 확인하세요.
exit /b 1
