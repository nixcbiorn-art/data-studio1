@echo off
REM Удаляет ранее созданную задачу автозапуска.
schtasks /delete /tn "RedCat Estate Scraper" /f
pause
