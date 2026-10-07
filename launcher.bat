@echo off
cd /d %~dp0
python -m redcat.tools.launcher
if errorlevel 1 (
    echo.
    echo Не удалось запустить панель управления. Убедитесь, что Python установлен
    echo и доступен в PATH ^(команда: python --version^).
    pause
)
