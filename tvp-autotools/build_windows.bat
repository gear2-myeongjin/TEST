@echo off
REM GitHub Actions 를 쓰지 않고 Windows PC 에서 직접 빌드할 때만 사용 (빌드하는 사람만 Python 필요)
cd /d %~dp0
python -m pip install -r requirements.txt || goto :err
if not exist plugin mkdir plugin
powershell -Command "Invoke-WebRequest -Uri 'https://github.com/brunchstudio/tvpaint-rpc/releases/download/1.1.0/tvpaint-rpc-1.1.0-tvp-11.dll' -OutFile 'plugin\tvpaint-rpc.dll'" || goto :err
pyinstaller --noconfirm --onefile --windowed --name TVPaintAutoTools --icon assets\icon.ico --add-data "plugin\tvpaint-rpc.dll;plugin" --add-data "assets\icon.ico;assets" --collect-submodules pytvpaint main.py || goto :err
echo.
echo 완료: dist\TVPaintAutoTools.exe
pause
exit /b 0
:err
echo 빌드 실패
pause
exit /b 1
