# Box-agent residence

The Cursor box this deploy targets has **no systemd-as-init** and **no crontab**.

Recommended (and shipped at `/workspace/relay/start-box-agent.sh`):

- A `while true` supervisor that restarts `relay-box-agent` on crash.
- At most one agent process (`pgrep` gate).
- **Not** auto-started on box reboot — re-run `start-box-agent.sh` after boot (or whatever host hook you add outside this repo).

Do not add a timed health-check routine; crash-restart is enough.
