@echo off
cd /d %~dp0
python launcher.py
if errorlevel 1 (
    echo.
    echo Не удалось запустить панель управления. Убедитесь, что Python установлен
    echo и доступен в PATH ^(команда: python --version^).
    pause
)
