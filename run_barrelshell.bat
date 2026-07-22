@echo off

set TELEGRAM_BOT_TOKEN=**********
set TELEGRAM_ALLOWED_IDS=**********************

REM Optional: pulse/reminder delivery target (defaults to first allowed ID)
set PULSE_CHAT_ID=**********************

REM Optional: Ollama running on ANOTHER machine — remove REM and edit.
REM set OLLAMA_HOST=

REM %~dp0 = the folder this bat lives in, so the path never
REM needs editing — keep the bat in the project folder.
cd /d %~dp0
if not exist ".venv\Scripts\python.exe" (
  echo [%date% %time%] ERROR: no venv at %cd%\.venv — run: py -3 -m venv .venv >> service.log
)

:loop
.venv\Scripts\python.exe barrel_v.py >> service.log 2>&1
echo [%date% %time%] Barrel exited (code %errorlevel%), restarting in 10s >> service.log
timeout /t 10 /nobreak >nul
goto loop
