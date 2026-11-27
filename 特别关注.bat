@echo off
cd /d "%~dp0"
set "APP=%LOCALAPPDATA%\Programs\WxPopup\WxPopup.exe"
if exist "%APP%" (
    start "" "%APP%" --star
    exit /b
)
where pythonw >nul 2>nul && (
    start "" pythonw "main.py" --star
    exit /b
)
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%D\pythonw.exe" (
        start "" "%%D\pythonw.exe" "main.py" --star
        exit /b
    )
)
echo 没找到客户端，也没找到 Python。请先安装，或双击 打包.bat 重新生成客户端。
pause
