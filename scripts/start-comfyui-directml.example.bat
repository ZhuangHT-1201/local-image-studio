@echo off
chcp 65001 >nul
setlocal EnableExtensions

set "COMFY_DIR=C:\ComfyUI"
set "PYTHON=%COMFY_DIR%\venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo Python environment not found: %PYTHON%
    pause
    exit /b 1
)

cd /d "%COMFY_DIR%"
set HF_HUB_OFFLINE=1
set TRANSFORMERS_OFFLINE=1
set HF_HUB_DISABLE_TELEMETRY=1
set DO_NOT_TRACK=1
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1

"%PYTHON%" main.py --directml --disable-api-nodes --preview-method auto --use-split-cross-attention --lowvram %*
