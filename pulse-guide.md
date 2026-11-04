# Pulse tasks: remove, expire, and run once

Pulse runs scheduled **agent instructions**, not just literal reminder messages.
For a simple one-time notification, the existing `remind` tool is still suitable.
All schedule times below use the bot host's local timezone unless an ISO offset
is supplied. The scheduler checks every `pulse_check_seconds` (30 by default).

## List or remove a particular task

Type these directly in Telegram or dashboard chat:

```text
/pulse
/pulse list
/pulse remove morning-brief
/pulse cleanup
```

- `/pulse` and `/pulse list` show active/pending tasks, schedules and expirations.
- `/pulse remove NAME` permanently removes the **exact** named task, matching
  case-insensitively, from `pulse.md` and/or the pending proposals. Its saved
  last-run/completion state is also removed. Partial/fuzzy matches are not used.
- `/pulse_remove NAME` remains supported as a compatibility alias.
- `/pulse cleanup` immediately removes explicitly expired tasks and completed
  one-time tasks left behind by a previous cleanup failure, plus expired pending
  proposals. The scheduler performs the same cleanup automatically each cycle
  and before startup tasks run.
- `/reject NAME` discards a pending proposal without affecting an active task.
  Bare `/approve` and `/reject` work only when exactly one proposal is pending.

If there are duplicate headings with the same name, removal refuses rather than
risk deleting the wrong task. Resolve those duplicates in `pulse.md` first.
Malformed task sections can still be removed by exact heading name.

A task queued behind another model turn is revalidated after obtaining the agent
lock: removed, edited or expired tasks will not start from that old snapshot.
Removing an **already running** task does not interrupt its current model/tool
turn; the response says so. It prevents future runs and avoids restoring stale
state after that turn finishes.

## Expire a recurring task

Add `expires:` immediately after its `schedule:` line, before the blank line and
prompt:

```markdown
## temporary-price-check
schedule: @every 4h
expires: 2030-01-31

Check the product page and tell me if the price changed.
```

A date-only expiration includes the **whole specified day**: `2030-01-31`
expires at local midnight starting February 1. For an exact cutoff:

```markdown
## temporary-event-watch
schedule: 0 9 * * *
expires: 2030-01-31T17:00:00-05:00

Check for changes to the event announcement.
```

At or after the cutoff, the task is not allowed to start. The scheduler removes
its section/state on the next check; removal is logged as `pulse_removed` with
reason `expired`. There is no restart requirement when editing `pulse.md`.

**Important boundaries:**

- No expiration is inferred from prose such as “until Friday” or an old date
  mentioned in the prompt. Add a real `expires:` field.
- Without that field, recurring cron/interval/idle/reboot tasks are retained.
- Invalid expiration dates disable the task and show a warning; they do not
  cause guessed deletion. Correct the metadata or remove the task manually.
- Metadata must be adjacent to `schedule:`. An `expires:` line after the blank
  line is prompt text, not lifecycle metadata.
- Keep task names unique, and do not use names beginning with `_` (reserved for
  scheduler state). Prompts cannot contain unindented `##` or deeper headings,
  because those delimit tasks in this file format.
- Removal is permanent, not a move to a trash folder. Keep backups of personal
  task definitions if you might need them again.

## One-time scheduled work

Use `@once` to run an agent task once, then remove it automatically:

```markdown
## prepare-meeting-brief
schedule: @once 2030-02-01 08:30
expires: 2030-02-01 09:00

Search for the latest announcements and send a short meeting brief.
```

- `@once` requires a date **and time**, optionally with an ISO timezone offset.
- The task runs on the first check at or after its scheduled time.
- `expires:` is optional, but useful for a task that should not run late. It must
  be after the scheduled time.
- A missed `@once` task manually present in `pulse.md` catches up after downtime
  unless its explicit expiration has passed.
- Newly proposed one-time tasks must still be in the future when approved.
  Unapproved `@once` proposals whose scheduled time passes are discarded by
  automatic cleanup; they cannot silently activate late.
- If the model turn or delivery raises an exception, the task remains for retry
  on a later scheduler cycle (until it expires or you remove it).
- A successful `PULSE_OK` turn also completes/removes a one-time task, even though
  it sends no message.

Completion is recorded in `pulse_state.json` **before** deleting the task's
section. If the task file is locked or a restart happens during cleanup, that
marker prevents a successful one-time task from repeating; cleanup retries.
Markers are tied to the exact task definition, so editing/reusing a name does
not accidentally inherit another definition's completion state.

This is best-effort scheduling, not an exactly-once transaction system. A crash
between performing an external action/delivering a message and recording
completion can still lead to a retry. “Success” means the turn and any delivery
returned without exception, not independent verification that the model achieved
every part of the requested goal. Avoid non-idempotent/high-stakes actions.

## Ask the bot to propose an expiring task

Examples of natural requests:

- “Check for event updates every morning through January 31, 2030. Make the task
  expire after that day.”
- “At 8:30 on February 1, 2030, prepare a meeting brief once. Don't run it after 9.”

The model's tool grammar preserves existing additions and adds a suffix:

```text
<pulse>add event-watch | 0 9 * * * | Check the announcement | expires: 2030-01-31</pulse>
<pulse>add meeting-brief | @once 2030-02-01 08:30 | Prepare the brief | expires: 2030-02-01 09:00</pulse>
```

The proposal notification includes the expiration. It is **not active** until
**you** type `/approve NAME`. The model cannot approve additions or directly
remove/clean tasks. If asked to remove one, it should identify the exact name and
tell you to type `/pulse remove NAME`.

## Upgrade and test

Deploy **`pulse_tasks.py` together with the updated `barrel_v1.py`**. Also update
`dashboard.html` to show expiration metadata. Do not replace your own `pulse.md`:
existing task sections work without an expiration, and existing croniter aliases
and extended cron syntax remain accepted. Restart after updating Python code;
subsequent task-file edits apply automatically.

Related fixes:

- Dashboard-only chat ID `0` is a valid scheduler delivery target, not “disabled.”
- Interval/cron baselines start at approval; new idle/reboot tasks are not falsely
  marked as having fired. Human slash commands count as user contact.
- State is reloaded under a shared lock; scheduler and commands use atomic file
  replacement instead of racing with an old in-memory state snapshot.
- Parser warnings disappear once the offending task is fixed.

From the repository root, in the bot's virtual environment:

```bash
python -m unittest discover -s tests -p test_pulse.py -v
python -m unittest discover -s tests -v
```

Tests use temporary files and mocked model/delivery calls, not your personal
schedule or a running Ollama instance. After upgrading, try removing a disposable
named task, expiring another in a minute, and scheduling a one-time task a few
minutes ahead. Allow one polling interval for automatic cleanup.
