@echo off
chcp 65001 >nul
rem 带控制台窗口的版本，用来排查问题
setlocal
set "TARGET=%~dp0stoneshard_accel.py"

set "PY="
if exist "D:\Python\Python313\python.exe" set "PY=D:\Python\Python313\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY (
    echo 没有找到 Python。
    pause
    exit /b 1
)

"%PY%" "%TARGET%"
if errorlevel 1 pause
exit /b 0
