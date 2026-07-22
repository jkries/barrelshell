@echo off

set TELEGRAM_BOT_TOKEN=8821221483:AAEOtvIzlo07zDFFHRM_uiRbUA_4azHYQmQ
set TELEGRAM_ALLOWED_IDS=8758733851

REM Optional: pulse/reminder delivery target (defaults to first allowed ID)
set PULSE_CHAT_ID=8758733851

REM Optional: Ollama running on ANOTHER machine — remove REM and edit.
set OLLAMA_HOST=http://jkai.tail462055.ts.net:11434

REM %~dp0 = the folder this bat lives in, so the path never
REM needs editing — keep the bat in the project folder.
cd /d %~dp0
if not exist ".venv\Scripts\python.exe" (
  echo [%date% %time%] ERROR: no venv at %cd%\.venv — run: py -3 -m venv .venv >> service.log
)

:loop
.venv\Scripts\python.exe sharp_v6.py >> service.log 2>&1
echo [%date% %time%] Sharp exited (code %errorlevel%), restarting in 10s >> service.log
timeout /t 10 /nobreak >nul
goto loop