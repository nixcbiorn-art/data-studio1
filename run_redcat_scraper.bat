@echo off
REM Запускает скрапер из его собственной папки и пишет вывод в run_log.txt
REM
REM Для разового запуска со свежим токеном (не трогая .env) можно вызвать:
REM     run_redcat_scraper.bat "eyJ...новый_токен"
REM Для автозапуска по расписанию токен НЕ передаётся сюда — он берётся
REM из .env, поэтому .env нужно обновлять вручную, пока он не истёк
REM (скрипт сам предупредит в run_log.txt, если срок истекает менее чем
REM через сутки).
cd /d %~dp0

REM Если используете виртуальное окружение venv, раскомментируйте строку ниже:
REM call venv\Scripts\activate.bat

if "%~1"=="" (
    python redcat_scraper.py >> run_log.txt 2>&1
) else (
    python redcat_scraper.py --token "%~1" >> run_log.txt 2>&1
)

echo %date% %time% - завершено с кодом %ERRORLEVEL% >> run_log.txt
