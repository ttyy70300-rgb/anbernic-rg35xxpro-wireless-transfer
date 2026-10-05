@echo off
rem ==========================================================================
rem PocketTransfer PC 端启动器（备用方案）
rem
rem 优先用 PocketTransfer.exe —— 那个是独立程序，不依赖 Python 环境。
rem 这个 bat 只在你想从源码直接跑、或需要看控制台输出时用。
rem ==========================================================================
setlocal
cd /d "%~dp0"

if exist PocketTransfer.exe (
  start "" PocketTransfer.exe
  exit /b 0
)

where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo.
    echo   [!] 没找到 Python，也没有 PocketTransfer.exe
    echo       请直接使用 PocketTransfer.exe，或安装 Python 3.9+
    echo.
    pause
    exit /b 1
  )
  set PY=py
) else (
  set PY=python
)

%PY% main.py %*
if errorlevel 1 pause
