@echo off
rem Rebuild the portable client into %LOCALAPPDATA%\Programs\WxPopup
cd /d "%~dp0"
set "PY=python"
where python >nul 2>nul || set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
"%PY%" -m PyInstaller --noconfirm --onedir --windowed --name "微信消息弹窗" --icon "app.ico" --exclude-module numpy --exclude-module pandas --exclude-module matplotlib main.py
if not exist "dist\微信消息弹窗\WxPopup.exe" (
    echo 打包失败，看看上面的报错。
    pause
    exit /b 1
)
if not exist "%LOCALAPPDATA%\Programs\WxPopup" mkdir "%LOCALAPPDATA%\Programs\WxPopup"
robocopy "dist\微信消息弹窗" "%LOCALAPPDATA%\Programs\WxPopup" /MIR /NFL /NDL /NJH /NJS >nul
echo.
echo 已更新客户端: %LOCALAPPDATA%\Programs\WxPopup
pause
