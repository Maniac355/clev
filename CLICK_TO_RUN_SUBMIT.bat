@echo off
setlocal
chcp 65001 >nul

cd /d "%~dp0"

echo ===== Clevai Form Bot =====
echo.
echo Chay nhanh:
echo   - Mac dinh vao mode run (fetch + submit)
echo   - Nhap SO / WHO / Token / teacher_status tren CLI
echo   - Chon profile bang so tren CLI
echo.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" clevai_form_bot.py run
) else (
  python clevai_form_bot.py run
)

echo.
echo Done.
pause
