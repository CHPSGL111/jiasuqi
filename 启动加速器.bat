@echo off
chcp 65001 >nul
rem 紫色晶石加速器 —— 双击运行（无控制台窗口）
setlocal
set "TARGET=%~dp0stoneshard_accel.py"

set "PYW="
if exist "D:\Python\Python313\pythonw.exe" set "PYW=D:\Python\Python313\pythonw.exe"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
if not defined PYW for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW for /f "delims=" %%i in ('where python 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW (
    echo 没有找到 Python，请先安装 Python 3.10 以上版本。
    pause
    exit /b 1
)

start "" "%PYW%" "%TARGET%"
exit /b 0
