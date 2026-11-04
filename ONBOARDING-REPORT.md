# Onboarding implementation & GitHub file report

Date: 2026-11-04

## Delivered

- Dashboard welcome banner and persistent **Setup / Settings** link.
- Seven-screen wizard: welcome, name, personality, Ollama backend, isolated
  personality preview, optional Telegram, completion summary.
- Editable personality presets that produce an ordinary `identity.md`.
- Actual generation test, installed-model suggestions, explicit download
  instructions, configurable Ollama address, and context setting.
- The configured address is used consistently by normal, vision, and sidebar
  inference, not just the setup test. Backend settings apply live.
- Resumable local drafts; explicit replacement confirmation and rolling-original
  identity backup; unrelated config keys preserved.
- Telegram token validation, webhook/conflict diagnostics, five-minute random
  pairing challenge, private-chat-only candidate registration, and explicit
  dashboard confirmation before persisting the owner's numeric allowlist ID.
- Deterministic delivery test separate from LLM performance. Restart required
  after pairing; existing environment-based deployments continue working.
- Atomic local writes, ignored credentials/drafts/backups, redacted errors and
  runtime logs, origin/token checks, request-size limits, and anti-framing headers.
- Updated recommended quick start and detailed fresh-install acceptance checklist.

No new production dependencies were introduced by onboarding itself.

## Validation performed

- Python compilation: `python -m py_compile barrel_v1.py onboarding.py`.
- JavaScript parsing: `node --check onboarding.js`.
- Full suite, including both optional Chromium suites:

  ```bash
  RUN_ONBOARDING_BROWSER_TESTS=1 BARREL_BROWSER_TESTS=1 .venv/Scripts/python.exe -m unittest discover -s tests -v
  ```

  Result: **97 tests passed, no skips** on this Windows/Python 3.14 checkout.
- Real Chromium exercised fresh local-only completion, save/resume with an
  existing identity, and Telegram confirmation UI. Ollama/Telegram calls in
  onboarding tests were mocked; no live bot was paired or live model benchmarked.
- Startup subprocess tests verified persisted secrets, environment precedence,
  and saved backend loading. Token-redaction tests cover logs and errors.
- `git diff --check` passed; `git check-ignore` confirmed generated private files
  and temporary secret writes are ignored.

Your remaining acceptance test is a fresh install with your real Ollama model
and a Telegram test bot. Follow **ONBOARDING.md**.

## Files to add/update for onboarding

| Path | Change |
| --- | --- |
| `onboarding.py` | New setup service, persistence, validation, pairing worker |
| `onboarding.html` | New wizard page |
| `onboarding.js` | New wizard flow and pairing UI |
| `onboarding.css` | New responsive wizard styles |
| `barrel_v1.py` | Setup routes, credential loading, explicit backend inference, security integration |
| `dashboard.html` | Welcome banner and Setup / Settings link |
| `config.example.json` | `ollama_host` and wizard/credential guidance |
| `.gitignore` | Private files and interrupted atomic-write exclusions |
| `README.md` | Recommended wizard-first quick start, manual path retained |
| `barrelshell-setup.md` | Wizard instructions and environment precedence |
| `ONBOARDING.md` | New acceptance checklist, privacy, troubleshooting, test instructions |
| `tests/test_onboarding.py` | New service, HTTP/security, persistence, startup integration tests |
| `tests/test_onboarding_browser.py` | New opt-in real Chromium wizard tests |
| `tests/test_agent_security.py` | Existing tests updated to mock the explicit-host inference wrapper |
| `.github/workflows/tests.yml` | Enable onboarding UI tests in the browser CI job |
| `memento.md` | New project handoff/live-state notes |
| `ONBOARDING-REPORT.md` | This report |

Explicit staging commands (review the resulting diff before committing):

```bash
git add onboarding.py onboarding.html onboarding.js onboarding.css
git add barrel_v1.py dashboard.html config.example.json .gitignore
git add README.md barrelshell-setup.md ONBOARDING.md ONBOARDING-REPORT.md memento.md
git add tests/test_onboarding.py tests/test_onboarding_browser.py tests/test_agent_security.py
git add .github/workflows/tests.yml
```

## Important: this checkout already had unpublished work

At the start of implementation, the working tree already contained substantial
fetching, scheduling, security, documentation, dependency, and Git cleanup
changes. They were preserved. Some onboarding-touched files (especially
`barrel_v1.py`) therefore contain both older unpublished work and this feature.
A file-level commit is **not** an isolated onboarding-only diff against HEAD.

The current core already depends on these **previously untracked** files. A
fresh GitHub clone of this exact working tree will be incomplete if you upload
only the onboarding files and omit these dependencies:

- `pulse_tasks.py`
- `barrel_web/__init__.py`
- `barrel_web/__main__.py`
- `barrel_web/browser.py`
- `barrel_web/cache.py`
- `barrel_web/extract.py`
- `barrel_web/service.py`
- `barrel_web/transport.py`
- `requirements.txt`
- `requirements-browser.txt` (optional browser features and browser CI)

Also review/publish the existing related work if it has not been committed
elsewhere:

- `FETCHING.md`, `pulse-guide.md`
- `tests/test_browser.py`, `tests/test_documents.py`, `tests/test_pulse.py`,
  `tests/test_transport.py`
- Existing modifications in `bundled/fetch.py`, `bundled/file.py`,
  `bundled/system.py`, `pulse.example.md`, `barrelshell-skill-guide.md`
- The existing deletion of the incorrectly named `gitignore` file, replaced by
  the actual `.gitignore`.
- Already-staged removal of tracked `bundled/__pycache__/*.pyc` files. Retain
  these removals; do not re-add bytecode.

For publishing the complete tested checkout, review and stage that baseline too:

```bash
git add pulse_tasks.py barrel_web requirements.txt requirements-browser.txt
git add FETCHING.md pulse-guide.md tests
git add bundled/fetch.py bundled/file.py bundled/system.py pulse.example.md barrelshell-skill-guide.md
git add -u -- gitignore bundled/__pycache__
git diff --cached --stat
git diff --cached --check
```

No staging, commit, or push was performed during this implementation.

## Never upload

`secrets.local.json`, `onboarding.local.json`, `identity.md`, `config.json`,
`identity.md.setup-backup.local.md`, `.env`, `run_barrel.bat`, chat/history files,
logs, workspace contents, `.venv/`, cache directories, or Python bytecode.

The wizard stores credentials **unencrypted**, with private permissions on
POSIX and inherited filesystem ACLs on Windows. Protect the project directory.
