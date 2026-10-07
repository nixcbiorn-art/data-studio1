@echo off
chcp 65001 >nul
cd /d %~dp0
title RedCat Studio

echo.
echo   Запускаю RedCat Studio...
echo   Приложение откроется в браузере. Это окно не закрывайте — пока оно
echo   открыто, работает сервер. Для остановки нажмите Ctrl+C или закройте окно.
echo.

python studio.py %*

if errorlevel 1 (
    echo.
    echo ----------------------------------------------------------------
    echo Не удалось запустить приложение.
    echo.
    echo Что проверить:
    echo   1^) установлен ли Python — команда:  python --version
    echo   2^) лежат ли рядом файлы studio.py и папка redcat
    echo.
    echo Приложению не нужны дополнительные библиотеки — только сам Python.
    echo ----------------------------------------------------------------
    pause
)
