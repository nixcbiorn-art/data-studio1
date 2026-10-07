@echo off
chcp 65001 >nul
cd /d %~dp0
title Проверка режима «только чтение»

python -m redcat.tools.selftest_readonly

echo.
echo Нажмите любую клавишу, чтобы закрыть окно.
pause >nul
