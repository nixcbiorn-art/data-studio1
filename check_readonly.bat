@echo off
chcp 65001 >nul
cd /d %~dp0
title Проверка режима «только чтение»

python selftest_readonly.py

echo.
echo Нажмите любую клавишу, чтобы закрыть окно.
pause >nul
