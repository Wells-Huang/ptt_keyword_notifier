# Project Working Agreements

- Keep the monitor dependency-free: use Python's standard library only.
- Never put DISCORD_WEBHOOK_URL, a webhook token, or any credential in source,
  fixtures, state, logs, workflow output, or commit messages.
- Do not modify user-level or system-level PATH or shared runtime settings.
- The workflow may update only data/state.json and data/heartbeat.txt.
- Preserve the six imported notified article IDs unless the user explicitly
  asks for a reset.
- Any change to the scheduled monitor requires unit tests, a dry-run smoke test,
  and a manual workflow verification before the local scheduler is removed.
