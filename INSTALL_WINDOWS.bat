@echo off
setlocal
chcp 65001 >nul

cd /d "%~dp0"

echo ===== Install clevai-form-bot =====

python --version >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python chua duoc cai hoac chua co trong PATH.
  pause
  exit /b 1
)

if not exist ".venv" (
  echo [1/4] Tao virtual environment...
  python -m venv .venv
)

echo [2/4] Nang cap pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip

echo [3/4] Cai dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt

echo [4/4] Cai Playwright Chrome...
".venv\Scripts\python.exe" -m playwright install chrome

echo.
echo [DONE] Cai dat xong.
echo Chay bot: CLICK_TO_RUN_SUBMIT.bat
pause
