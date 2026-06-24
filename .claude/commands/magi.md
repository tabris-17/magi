---
description: Run the magi CLI — upgrade/launch/workflow/run/deploy/migrate
argument-hint: upgrade dev|prod · launch dev|prod · stop dev|prod · workflow · run --env dev|prod · deploy [target] · migrate up
allowed-tools: Bash
model: haiku
---
Run `./magi $ARGUMENTS` from the repo root (/Users/kai/Documents/development/magi) and
report the result. If `$ARGUMENTS` is empty, run `./magi --help` and show the usage.

Handle a few verbs specially:
- `launch dev` and `workflow` end in a foreground server — run the command in the
  BACKGROUND, wait until the app answers, then report the local URL (and for
  `workflow`, the progress of each of its three stages).
- `upgrade prod` / `deploy` — surface the pre-flight `*.db` gate result and the service
  restart status; if it aborts, report why.
- `launch prod` — report which mini services restarted.
- `stop dev` — report whether a local server was stopped. `stop prod` — report which mini services were booted out.
- `upgrade dev` — report which prod DBs were pulled and where the local `.bak` backups are.
