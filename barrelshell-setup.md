# BarrelShell — Setup & Deployment Guide

**BarrelShell** (barrelshell.com): the **Shell** is the platform —
self-hosted Ollama, the Python core, and the settings and skills
around it. A running bot instance is a **Barrel**.

Deployment of the agentic Telegram bot (**v5 script**: identity + history +
tools + pulse scheduler + logging) with a Python virtual environment
and automatic start/crash recovery.

Covers **Ubuntu/Linux** (systemd) and **Windows 10/11** (Task
Scheduler). The script itself is plain cross-platform Python — only
the service wrapper differs.

**Ollama location:** the guide assumes Ollama runs on the same
machine. To use an Ollama instance on another device, uncomment the
`OLLAMA_HOST` lines in step 5 and see the "Remote Ollama" note there.

---

## 1. Project directory and virtual environment

**Linux:**

```bash
mkdir -p ~/barrelshell && cd ~/barrelshell
sudo apt update && sudo apt install -y python3-venv

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install ollama ddgs requests croniter tzdata

readlink -f .venv/bin/python   # note this absolute path for systemd
```

**Windows (PowerShell):** install Python from python.org (check "Add
to PATH"), then:

```powershell
mkdir C:\barrelshell; cd C:\barrelshell

py -3 -m venv .venv
.venv\Scripts\Activate.ps1
# If activation is blocked:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

python -m pip install --upgrade pip
pip install ollama ddgs requests croniter tzdata
```

The venv's interpreter is `C:\barrelshell\.venv\Scripts\python.exe` —
services run it directly, no activation needed.

## 2. Project files

Place in the project directory:

```
barrel_v1.py   the bot script
identity.md               persona file
pulse.md              scheduled tasks (cron syntax; optional — no file, no pulses)
history.md             created automatically on first save (optional to pre-seed)
pulse_state.json      created automatically (pulse last-run tracking)
token_usage.json      created automatically (lifetime + per-day token counts)
web_chat.json         created automatically (dashboard chat transcript)
workspace/            created automatically — the ONLY folder the file skill can touch
config.json           optional — your tunables, copied from config.example.json
skills/               optional — drop-in skill files (see skills/example_roll.py)
reminders.json        created automatically (pending one-shot reminders)
agent_log.jsonl       created automatically (full turn/tool log)
```

## 2b. Configuration and drop-in skills

Two things survive every update to the main script because they live
outside it:

**config.json** — copy `config.example.json` to `config.json` and
edit. It's merged over built-in defaults, so any key you omit (or any
new setting a future version adds) just uses the default; an old
config never breaks a new script. Change the model here instead of
editing the script: `"model": "gemma3:12b"`. Secrets never go in this
file — they stay in `.env` / `run_barrel.bat`.

**Photo understanding (optional)** — set `"vision_model"` in
config.json to a vision-capable model you've pulled (e.g.
`gemma3:4b`, `qwen2.5vl:3b`, `minicpm-v`) and restart. Incoming
Telegram photos are then described by that model and the description
feeds the normal conversation — your main model stays whatever you
chose. Leave it empty and photos get the polite "text only" reply.
Note the honest limit of this design: the bot works from a
*description* of your photo, not the pixels, so fine detail
questions can exceed what the description captured.

**skills/** — every `.py` file in this folder defining a `SKILL`
dict (`name`, `desc`, `handler`) is merged into the tool registry at
startup; the console lists what loaded. `skills/example_roll.py`
shows the contract and is safe to delete. A skill file may override a
built-in tool by using its name — the console says so loudly.

> **Skills are code.** They execute with the bot's full permissions
> the moment the bot starts. Read any skill you didn't write before
> putting it in this folder — this is the same trust decision as
> running any downloaded script.

## 3. Choosing a model for your hardware

What matters for this project is **instruction-following and protocol
discipline** (the model must use the tool tags and `PULSE_OK`
correctly), not encyclopedic knowledge. Bigger models follow the
protocol more reliably; below ~4B expect regular tag mistakes and
persona drift.

All sizes assume Ollama's default Q4_K_M quantization. Rough guide:

| Host hardware | Suggested models | Notes |
|---|---|---|
| CPU-only or ≤6 GB VRAM, 8–16 GB RAM | `qwen3:4b`, `llama3.2:3b`, `gemma3:4b` | Works, but slow on CPU and flaky on protocol — fine for development, frustrating as a daily bot |
| 8 GB VRAM (RTX 3060/4060 class) | `qwen3:8b`, `llama3.1:8b` | The sweet spot for this project; script default |
| 12–16 GB VRAM (RTX 4070 Ti/4080), or Mac 16–32 GB unified | `qwen3:14b`, `gemma3:12b`, `phi4:14b` | Noticeably better protocol adherence and search-result synthesis |
| 24 GB VRAM (RTX 3090/4090), or Mac 32–48 GB unified | `qwen3:32b`, `gemma3:27b` | Very reliable tag discipline; comfortable headroom for larger `NUM_CTX` |
| 64 GB+ unified (Mac Studio, DGX Spark / GX10 class) | `qwen3:32b` at high context, `llama3.3:70b` | 70B-class models are dramatically better at knowing *when* to use a tool vs. answer |

Two model-specific notes:

- **Context costs memory.** `NUM_CTX` grows the KV cache, which
  competes with model weights for VRAM. If a model that fits suddenly
  crawls or spills to CPU after raising `NUM_CTX`, that's why.
- **Thinking models.** `qwen3` (and other reasoning models) can emit
  `<think>…</think>` blocks. Recent Ollama versions separate this out,
  but if reasoning text shows up in Telegram replies, pass
  `think=False` in `ollama.chat()` or strip `<think>` blocks the way
  the script strips `<remember>`.

Switch models with `ollama pull <model>`, edit the `MODEL` constant,
restart the service.

## 3b. Local-only mode (skipping Telegram)

Telegram is optional. If you provide no `TELEGRAM_BOT_TOKEN` and no
`TELEGRAM_ALLOWED_IDS`, your Barrel starts in **local-only mode**: the
dashboard becomes its only interface, and scheduled tasks, reminders,
and task proposals are delivered to the dashboard's chat panel instead
of Telegram. The startup banner says which mode it chose.

In local-only mode the web conversation is saved to `web_chat.json`,
so the transcript survives a restart or a page reload — with no
Telegram, that file is the only record of your chats.

To use it, skip step 4 entirely and start your Barrel (step 6); open
http://127.0.0.1:8787 and talk to it there. Telegram remains the
recommended interface — it's the easier phone experience, and it can
push messages to you rather than waiting for a browser tab to poll.

> **Viewing the dashboard from another device.** By default it binds
> to `127.0.0.1` and has no login. If you change `dashboard_host`,
> your Barrel will **refuse to start the dashboard** unless you also
> set `"dashboard_token"` in config.json — then open
> `http://host:8787/?token=YOUR_TOKEN` once and a cookie keeps you
> signed in. SSH-tunnelling the port stays the safer option.

## 4. Telegram setup

**Create the bot.** Message **@BotFather** on Telegram → `/newbot` →
choose a display name and a unique username ending in `bot`. BotFather
returns the API token (format `123456789:AAF...`). Treat it like a
password.

While in BotFather, also send `/setprivacy` → your bot → **Enable**.

**Get your numeric user ID** (for the whitelist). Message
**@userinfobot** — or start your bot, send it a message, and read the
ID from the raw API:

```bash
# Linux
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
```
```powershell
# Windows
Invoke-RestMethod "https://api.telegram.org/bot<TOKEN>/getUpdates" | ConvertTo-Json -Depth 8
```

Look for `"from": { "id": 123456789, ... }`.

## 5. Secrets and configuration

Never put the token in the script.

**Linux** — create `~/barrelshell/.env`:

```ini
TELEGRAM_BOT_TOKEN=123456789:AAF...your-token...
TELEGRAM_ALLOWED_IDS=123456789

# Optional: where pulse messages/reminders default to.
# Defaults to the first allowed ID.
#PULSE_CHAT_ID=123456789

# Optional: Ollama running on ANOTHER machine — uncomment and edit.
# Leave commented when Ollama is local.
#OLLAMA_HOST=http://192.168.1.50:11434
```

```bash
chmod 600 ~/barrelshell/.env
```

**Windows** — create `C:\barrelshell\run_barrel.bat`. This wrapper holds the
configuration AND the crash recovery — the `:loop` at the bottom
restarts the bot 5 seconds after any crash, so no service manager is
needed:

```bat
@echo off
set TELEGRAM_BOT_TOKEN=123456789:AAF...your-token...
set TELEGRAM_ALLOWED_IDS=123456789

REM Optional: pulse/reminder delivery target (defaults to first allowed ID)
REM set PULSE_CHAT_ID=123456789

REM Optional: Ollama running on ANOTHER machine — remove REM and edit.
REM set OLLAMA_HOST=http://192.168.1.50:11434

REM %~dp0 = the folder this bat lives in, so the path never
REM needs editing — keep the bat in the project folder.
cd /d %~dp0
if not exist ".venv\Scripts\python.exe" (
  echo [%date% %time%] ERROR: no venv at %cd%\.venv — run: py -3 -m venv .venv >> service.log
)
:loop
.venv\Scripts\python.exe barrel_v1.py >> service.log 2>&1
echo [%date% %time%] Barrel exited (code %errorlevel%), restarting in 5s >> service.log
timeout /t 5 /nobreak >nul
goto loop
```

This file contains your token — keep it out of synced/shared folders,
and restrict it to your account (file Properties → Security).

> **Remote Ollama note.** `OLLAMA_HOST` on the bot side is half the
> job. On the machine *hosting* Ollama, the server must listen beyond
> localhost: set the environment variable `OLLAMA_HOST=0.0.0.0` there
> (on Linux, a systemd override for ollama.service; on Windows, a
> system environment variable) and restart Ollama. **Ollama's API has
> no authentication** — firewall that port (11434) so only the bot
> machine can reach it, and never expose it toward the internet.

## 6. First run (manual test)

**Linux:**

```bash
cd ~/barrelshell
set -a; source .env; set +a
.venv/bin/python barrel_v1.py
```

**Windows:** run `run_barrel.bat` from a terminal. Output goes to
`service.log` — watch it live from a second PowerShell window:

```powershell
Get-Content C:\barrelshell\service.log -Tail 20 -Wait
```

To stop it, press Ctrl+C and answer `Y` to "Terminate batch job?" —
just closing python would trigger the restart loop.

Test from Telegram:

1. Any message → reply (first one is slow while the model loads).
2. A current-events question → console/log shows a search round.
3. Tell it a fact about yourself → `/history` shows it saved.
4. "Remind me in 5 minutes to stretch" → `/reminders` lists it; the
   ⏰ message arrives within ~30s of the due minute.
5. `/pulse` → lists tasks parsed from pulse.md. Then set one task's
   schedule 2–3 minutes out (hot-reloads, no restart), watch it fire
   or log `PULSE_OK`, and revert.
6. Message the bot from a non-whitelisted account → rejection.
7. (If `vision_model` is set) send the bot a photo with a caption
   asking something about it — the reply should reflect actual image
   content, and the log gains a `vision_reply` entry.
8. On the host machine, open **http://127.0.0.1:8787** — the
   dashboard should show status, uptime, the model actually loaded in
   Ollama, and your last message. `/status.json` serves the same data
   as JSON.

Stop it (Ctrl+C) before moving on. If it's broken here, the service
wrapper will just restart a broken script forever.

## 7. Run automatically with crash recovery

### Linux — systemd

Create `/etc/systemd/system/barrelshell.service`:

```ini
[Unit]
Description=BarrelShell Barrel (Telegram agent)
After=network-online.target ollama.service
Wants=network-online.target
# Remote Ollama? Remove "ollama.service" from After= above —
# this machine doesn't run it.

[Service]
Type=simple
User=jay
WorkingDirectory=/home/jay/barrelshell
EnvironmentFile=/home/jay/barrelshell/.env
ExecStart=/home/jay/barrelshell/.venv/bin/python barrel_v1.py

# Crash recovery: always restart, wait 5s between attempts,
# give up after 5 crashes in 2 minutes (a real bug — read the
# logs instead of loop-restarting).
Restart=always
RestartSec=5
StartLimitIntervalSec=120
StartLimitBurst=5

NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Adjust `User=` and paths, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now barrelshell
sudo systemctl status barrelshell
```

### Windows — Task Scheduler (built-in, nothing to install)

Crash recovery already lives in `run_barrel.bat`'s restart loop, so
Task Scheduler has exactly one job: launch the bat at boot. None of
its finicky restart settings are needed.

Task Scheduler → Create Task:

- **General:** name it `Barrel`; check "Run whether user is logged on
  or not" (this also hides the console window).
- **Triggers:** At startup, with a 30-second delay so networking is up.
- **Actions:** Start a program → `C:\barrelshell\run_barrel.bat`.
- **Settings:** uncheck "Stop the task if it runs longer than."

Test it now rather than at next reboot: right-click the task → Run,
then message the bot.

(If you ever want a true Windows service instead — proper Services
panel entry, cleaner process management — the third-party tool NSSM
does that, but it's not needed for this setup.)

## 8. Day-to-day operations

**Linux:**

```bash
journalctl -u barrelshell -f            # live logs (incl. pulse fires)
sudo systemctl restart barrelshell      # after editing the script
sudo systemctl reset-failed barrelshell # if it hit the crash limit
```

**Windows:**

```powershell
Get-Content C:\barrelshell\service.log -Tail 50 -Wait   # live logs
schtasks /end /tn Barrel                            # full stop
schtasks /run /tn Barrel                            # start again
```

A handy side effect of the restart loop: after editing the script,
you don't need to touch Task Scheduler — just end the python process
in Task Manager, and the loop relaunches it with your changes five
seconds later.

**Dashboard.** `http://127.0.0.1:8787` on the host machine shows live
runtime info: status/uptime, configured model and context size, what
Ollama actually has loaded (and how much VRAM it's using — a quick
check for CPU spillover), turn/tool/error counters, the last message
and reply, pulse tasks with last-run times, and pending reminders.
Token usage appears at three scopes: this session, the last turn
(including its context size vs `NUM_CTX` and generation speed), and
lifetime/today totals persisted in `token_usage.json` — one compact
entry per day, so it tracks the whole life of the install without
growing meaningfully.
The agent can also report all of this about itself in chat — ask it
"what model are you?" or "how many tokens have you used?" and its
status skill answers from live data instead of guessing.
It auto-refreshes every 5 seconds; `/status.json` returns the same
data for scripting (`curl` it from a health-check if you like). The
port is the `DASHBOARD_PORT` constant.

It binds to **localhost only, on purpose** — the page displays your
private conversation content. To view it from another device, tunnel
it over SSH rather than binding wider:

```bash
ssh -L 8787:127.0.0.1:8787 user@barrel-host   # then browse localhost:8787
```

On both platforms, `agent_log.jsonl` in the project directory is the
detailed record — every raw model reply, tool call, history save, and
reminder. That's where to look when the bot *behaves* oddly;
service logs are for when it *crashes*.

Restart is only needed for changes to the **script**. `identity.md`,
`history.md`, and `pulse.md` are re-read continuously — persona and
schedule edits take effect within seconds, no restart.

If a pulse task misbehaves, check `pulse_state.json` for its last-run
timestamp; a brand-new task arms from when it's first seen rather
than back-firing missed runs.

Crash-test recovery once — kill the process (`sudo kill -9 <PID>` /
end task in Task Manager) and confirm it's back within ~10 seconds.

## 9. Known first-iteration limits

The active conversation lives in RAM, so every restart wipes it —
only `history.md` (the long-term fact store), reminders, and pulse
state survive. Persisting the conversation is a natural next-version
item. `agent_log.jsonl`
grows without bound — fine for a beta; add logrotate (or dated files)
later. The DuckDuckGo search backend rate-limits heavy use; the
upgrade path is self-hosted SearXNG. And `getUpdates` polling means
only one running instance — Telegram rejects a second poller with a
409, a handy safeguard against accidentally running it twice.
