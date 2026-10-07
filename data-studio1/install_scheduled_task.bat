@echo off
REM Запустите этот файл ОДИН РАЗ от имени администратора
REM (правой кнопкой -> "Запуск от имени администратора"),
REM чтобы создать задачу в Планировщике заданий Windows:
REM запуск по будням (Пн-Пт) в 10:00 по времени этого компьютера.

setlocal
set "SCRIPT_DIR=%~dp0"
set "TASK_NAME=RedCat Estate Scraper"

schtasks /create ^
  /tn "%TASK_NAME%" ^
  /tr "\"%SCRIPT_DIR%run_redcat_scraper.bat\"" ^
  /sc weekly ^
  /d MON,TUE,WED,THU,FRI ^
  /st 10:00 ^
  /rl highest ^
  /f

echo.
if %ERRORLEVEL%==0 (
    echo Готово! Задача "%TASK_NAME%" создана: запуск по будням в 10:00.
    echo Проверить/изменить её можно в "Планировщике заданий" ^(Task Scheduler^).
) else (
    echo Не удалось создать задачу. Убедитесь, что вы запустили файл от имени администратора.
)
pause
