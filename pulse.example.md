# Pulse

Scheduled tasks. Each task is a `## name` section with a `schedule:`
line in standard cron syntax (min hour day month weekday), or one of:
`@reboot` (once each time the bot starts), `@every 30m` (a repeating
interval, relative to its own last run), or `@idle 4h` (once after
that long with no contact from you — it won't fire again until you
speak to it). Durations use m, h, or d. Then comes
the prompt the agent runs at that time. If the agent decides nothing
is worth sending, it replies PULSE_OK and stays silent.

This file is re-read every cycle — edits take effect without a
restart.

## morning-brief
schedule: 0 7 * * 1-5

Good morning. Search for today's top technology and education news
headlines, then send a brief morning message: today's date, 2-3
headlines worth knowing about in one line each, and nothing else.

## weekly-history-review
schedule: 0 17 * * 5

Review your history for anything that is now outdated, resolved, or
was a one-time event that has passed. If everything still looks
current, reply PULSE_OK. Otherwise, send a short list of entries you
think could be removed and ask for confirmation.

## hydrate-nag
schedule: 30 14 * * 1-5

If you feel like it, send a one-line mid-afternoon nudge to step away
from the screen for a minute. Vary the phrasing; keep it dry, not
chipper. It's fine to skip some days — reply PULSE_OK to skip.

## startup-notice
schedule: @reboot

You just came back online after a reboot, crash, or restart. Send a
single short line confirming you're back up, with the current time.
No fanfare.

## check-in
schedule: @idle 2d

We haven't spoken in a couple of days. Send one short, low-pressure
line — something you noticed, or just hello. Don't ask me to do
anything. If nothing feels worth saying, reply PULSE_OK.
