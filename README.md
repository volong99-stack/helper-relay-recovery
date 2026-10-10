# helper-relay-recovery

An experimental relay worker that wakes an existing app-owned conversation and
supervises one full cycle through **split-stage timeouts**: instead of a single
deadline for the whole run, each transition has its own timeout so the operator
can see exactly where a cycle stalled:

1. **queue acceptance** — the wake message was queued into the app thread
2. **worker start** — the worker picked up the run and reported
   `worker_started_at_epoch`
3. **first read** — the worker's browser session was observed live
   (`browser_read_at_epoch`)
4. **confirmed send** — the relay send was confirmed
   (`send_confirmed_at_epoch`)
5. **observed reply** — the destination replied
   (`destination_replied_at_epoch`)

A stage receipt is only trusted when its timestamps are numeric, monotonic
across the chain, and fall inside the stage's deadline window; anything else is
flagged `INVALID_*` or `EXPIRED_*` rather than treated as delivery.

## How the stages work

- `stages.py` — pure logic: `progress(pending, receipt, config, now)` returns
  the current stage, the reason (if any), and the next deadline. A running
  process is never inferred as delivery — only receipts count.
- `queue_worker.py` — enqueues a wake via the configured executable,
  persists the pending state **before** sending (uncertain results never
  trigger a duplicate send), then polls a heartbeat receipt file and drives
  the stage chain from `stages.py` to completion or to a reconciled failure.
  Failures are written to a durable local outbox (`failure_outbox/`) and —
  where a mailbox bridge is configured — posted as health status for
  operator review.
- `config.json` — all wiring and timeouts (see below).

## Configuration (`config.json`)

| Key | Meaning |
|---|---|
| `codex_exe`, `cwd`, `thread_id` | executable / working dir / target thread for `queue` |
| `rollout_path`, `rollout_offset` | session log used to gate on completed turns |
| `startup_timeout_seconds` | worker-start deadline after queue acceptance (default 600) |
| `heartbeat_timeout_seconds` | freshness window for heartbeat receipts (default 180) |
| `browser_timeout_seconds` | first-live-browser-read deadline (default 180) |
| `send_timeout_seconds` | send-confirmation deadline (default 120) |
| `reply_timeout_seconds` | destination-reply deadline (default 120) |
| `overall_timeout_seconds` | hard cap for the whole cycle (default 900) |
| `worker_max_seconds`, `max_failures`, `cycle_minutes`, `cycle_delay_seconds` | scheduling guards |
| `python_exe`, `mailbox_bridge`, `mailbox_db` | optional local-mailbox bridge for failure notices |

All paths in the shipped `config.json` are placeholders — point them at your
own installation before running. This repo's `.gitignore` excludes `*.json`
runtime state; add the real config explicitly:

```bash
git add -f config.json
```

## Limitations

- **Experimental.** Timeouts are heuristic — they detect "no evidence yet",
  they cannot distinguish a slow worker from a dead one.
- **Live reliability is unresolved.** The chain only observes receipts; a
  stall that leaves no receipt (crashed supervisor, lost heartbeat file,
  wall-clock jump) ends in `requires_user_action`, not auto-recovery.
- Receipts are trusted by value, not by signature — anything that can write
  the heartbeat file can forge a stage.
- Clock changes are detected (`clock_changed`) but only force
  reconciliation; they don't repair the cycle.
- Written for a specific Windows-centric setup; paths and process flags
  (`CREATE_NO_WINDOW`) assume that environment.
