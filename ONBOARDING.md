# First-run onboarding

## Fresh-install acceptance checklist

1. Use a **new directory/clone**, not your active bot's working directory. Install
   Python 3.10+, Ollama, and `requirements.txt`. No new production dependencies
   are needed for onboarding.
2. Do not copy `identity.md`, `config.json`, or `run_barrel.bat` for this test.
   Remove `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_IDS` from this shell's
   environment (including empty values). Do not stop or alter another deployment
   unless it is using the same Telegram bot; use a separate test bot if possible.
3. From the project directory, run `python barrel_v1.py` using your virtualenv.
4. Open **http://127.0.0.1:8787/**. The welcome banner should link to **/setup**.
5. Enter a name and optional owner name. Choose a personality, inspect/edit the
   generated markdown, then continue. Check that `identity.md` contains it.
6. Check Ollama. Installed models should appear as suggestions. If none exist,
   run the shown `ollama pull qwen3:8b` command on the Ollama machine (or choose a
   smaller model). Test actual generation. A failed test must not allow saving
   that backend. Continue after a successful test.
7. Send a preview message. Check that the personality feels right; go back and
   adjust if needed. Preview calls do not run tools or write history/transcripts.
8. Try **Save & finish later**, reopen Setup / Settings, and verify your draft
   and current step are restored. A process restart preserves the draft too;
   model tests and unconfirmed Telegram challenges are intentionally ephemeral.
9. Choose local-only by continuing past Telegram. Finish, then send a real message
   in dashboard chat. No Telegram credentials are required.
10. Reopen Setup / Settings to test Telegram. Create a bot with @BotFather,
    paste the token, and validate it. Open the generated link and press Start.
    Confirm the displayed account/name/numeric ID in the dashboard. A direct
    message without the exact challenge must not authorize an account.
11. Verify that Telegram receives the deterministic test reply. This tests
    transport, not the LLM. Finish and **restart the BarrelShell process**
    (or type `/restart` in normal dashboard chat). Send the Telegram bot a
    normal message and verify a model reply.
12. Restart again and verify Telegram still works from saved local credentials.
    A different Telegram account must remain unauthorized.

## Existing setup checks

- Opening setup loads existing identity markdown without regenerating it.
- Editing preferences alone does not replace custom markdown; use **Generate
  identity from these choices** when you explicitly want to regenerate it.
- Replacing an existing identity requires the confirmation checkbox. The first
  replaced identity is retained as `identity.md.setup-backup.local.md`.
- Saving a backend preserves unrelated `config.json` keys. Model, address, and
  context apply live. `OLLAMA_HOST` overrides the configured address.
- Active Telegram deployments are kept as-is. The wizard will not start a
  competing poller for an active runtime. To change the bot, stop the process,
  remove old credentials from its launcher/environment and `secrets.local.json`,
  restart local-only, and pair the replacement bot.

## Local files and privacy

| File | Purpose | Commit? |
| --- | --- | --- |
| `config.json` | Merged tunables, including model and `ollama_host` | No |
| `identity.md` | User-editable personality | No |
| `secrets.local.json` | Bot token and numeric owner allowlist | **Never** |
| `onboarding.local.json` | Draft, current step, completion/dismissal flags | No |
| `identity.md.setup-backup.local.md` | Original identity backup | No |

These paths are ignored by `.gitignore`. No token is put in the wizard draft,
browser URL, browser local storage, or setup error logs. The pairing URL contains only
a random challenge, not a token. Challenges expire in five minutes and require
confirmation in the authenticated/same-origin dashboard. Before authorization,
ordinary messages cannot invoke model tools or mutate runtime memory.

Secrets are **not encrypted**. Atomic writes use private permissions on POSIX;
on Windows, filesystem ACLs are inherited, so keep the folder private to your
account. Anyone with dashboard access can change the assistant's setup. Keep
the default loopback bind, or configure dashboard authentication and use a
trusted encrypted tunnel. Frame embedding of the dashboard is disallowed.

## Troubleshooting

- **Ollama reachable, generation fails:** install the selected model on that
  server, check available RAM, lower context if necessary, and retry. Cold model
  loading can take up to two minutes. Saving requires a successful test of the
  exact address/model during this process lifetime.
- **Environment override:** update/remove `OLLAMA_HOST` in the actual launcher
  if you want to change the address. For wizard Telegram pairing, unset both
  Telegram variables; even blank variables override local secrets.
- **Pairing link expired:** cancel and create a new one. A page refresh can
  recover a pending account confirmation, but the secret pairing link is not
  persisted. A process restart requires a new challenge.
- **Pairing cancelled, immediate retry fails:** wait a few seconds for the old
  long-poll request to finish, then retry.
- **Polling conflict (409):** stop another process using this token. If the bot
  has a webhook, remove it deliberately yourself or create a different bot. The
  wizard never removes a webhook or takes over another deployment automatically.
- **Credentials saved, delivery failed:** authorization is saved separately
  from transport success. Check connectivity or whether the bot was blocked,
  restart, then test with a real message.
- **Saved credentials but Telegram is still off:** restart the process; updating
  the wizard page alone does not reload startup credentials.
- **Malformed local JSON:** repair the named file before retrying; setup does
  not silently replace unreadable configuration with defaults.
- **Want to retest the first-run welcome:** in the test directory only, remove
  `onboarding.local.json`. Remove `identity.md`, `config.json`, and
  `secrets.local.json` as well only if you deliberately want a completely fresh
  setup. Never delete production state just to test onboarding.

## Automated verification

Offline suite (no Ollama/Telegram service needed):

```bash
python -m unittest discover -s tests -v
```

Optional real Chromium UI tests with mocked backends:

```bash
pip install -r requirements-browser.txt
python -m playwright install chromium
# Linux / macOS / Git Bash:
RUN_ONBOARDING_BROWSER_TESTS=1 python -m unittest discover -s tests -p test_onboarding_browser.py -v
```

PowerShell: set `$env:RUN_ONBOARDING_BROWSER_TESTS = '1'`, then run the same
`python -m unittest ...` command without the prefix. Playwright is optional and
is not loaded by the wizard at runtime. Live Telegram delivery and actual local
model quality/performance still need the fresh-install checks above.
