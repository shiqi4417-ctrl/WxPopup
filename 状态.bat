@echo off
rem 有 Python 就直接输出文字；没有就用客户端（会弹出记事本显示结果）
cd /d "%~dp0"
set "APP=%LOCALAPPDATA%\Programs\WxPopup\WxPopup.exe"
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
        if exist "%%D\python.exe" set "PY=%%D\python.exe"
    )
)
if defined PY (
    "%PY%" main.py --status
) else if exist "%APP%" (
    start "" "%APP%" --status
) else (
    echo 没找到客户端，也没找到 Python。
)
echo.
pause
