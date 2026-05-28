@echo off
chcp 65001 > nul
title GHN AI Assistant – Debug Mode

echo ============================================
echo   GHN AI Assistant Backend – Debug Mode
echo ============================================
echo.

:: --- Kiem tra Python ---
echo [1] Kiem tra Python...
python --version
if %errorlevel% neq 0 (
    echo.
    echo [LOI] Khong tim thay Python!
    echo Cai Python tai: https://www.python.org/downloads/
    echo Khi cai nho tick "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

:: --- Kiem tra pip ---
echo [2] Kiem tra pip...
pip --version
if %errorlevel% neq 0 (
    echo [LOI] pip khong hoat dong!
    pause
    exit /b 1
)

:: --- Cai dependencies ---
echo.
echo [3] Cai dependencies...
pip install fastapi uvicorn[standard] httpx pydantic python-multipart
if %errorlevel% neq 0 (
    echo [LOI] Cai dependencies that bai!
    pause
    exit /b 1
)

:: --- Chay server ---
echo.
echo [4] Khoi dong server tai http://localhost:8000
echo     API Docs: http://localhost:8000/docs
echo     Nhan Ctrl+C de dung
echo.

cd /d "%~dp0"
python -m uvicorn main:app --reload --port 8000 --host 0.0.0.0

echo.
echo [!] Server da dung.
pause
