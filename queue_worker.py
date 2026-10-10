"""Wake the existing app-owned conversation; never open a competing writer."""
import json
from contextlib import closing
import math
import os
from pathlib import Path
import re
import subprocess
import sqlite3
import time
import uuid
from stages import progress, clock_changed


def safe_error(text):
    text = re.sub(r"\x1b\[[0-9;]*m", "", text or "")
    text = re.sub(r"(?i)(bearer\s+|(?:token|password|secret|api[_-]?key|code)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[REDACTED JWT]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED KEY]", text)
    return text[-6000:]


def verified_receipt(receipt, run_id, now, max_age, instance):
    if not isinstance(receipt, dict):
        return False
    stamp = receipt.get("checked_at_epoch")
    return (receipt.get("run_id") == run_id
            and type(stamp) in (int, float) and math.isfinite(stamp)
            and 0 <= now - stamp <= max_age
            and receipt.get("extension_instance_id") == instance
            and receipt.get("browser_online") is True
            and receipt.get("muse_ack_verified") is True
            and receipt.get("live_recovery_verified") is True
            and receipt.get("requires_user_action") is not True
            and receipt.get("stop_requested") is not True)


def record_failure(app, reason, notice_id=None):
    """Durable private outbox. A saved/posted notice is NOT a MAIN delivery."""
    from supervisor import write_json, utc
    identity = notice_id or str(uuid.uuid4())
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identity):
        raise ValueError("Invalid notice ID")
    notice = {"id": identity, "created_at": utc(), "reason": reason,
              "recipient": "muse", "delivery_verified": False,
              "message": "Codex automatic relay is not confirmed working. "
                         "Failure: " + reason + ". Please show this status in Muse MAIN."}
    if app.status.get('stage'):
        notice['message'] += ' Last observed stage: ' + app.status['stage'] + '.'
    folder = app.base / "failure_outbox"
    folder.mkdir(exist_ok=True)
    write_json(folder / (identity + ".json"), notice)
    app.save(failure_notice_id=identity, notification_delivery_verified=False,
             notification_state="saved_locally_not_delivered")
    # The existing private mailbox is a store, not a verified wake or alert path.
    bridge = app.config.get("mailbox_bridge")
    python = app.config.get("python_exe")
    database = app.config.get("mailbox_db")
    if not bridge or not python or not database:
        return
    body = folder / (identity + ".txt")
    body.write_text(notice["message"], encoding="utf-8")
    try:
        result = subprocess.run([python, bridge, "--db", database, "post", "--channel", "health_status",
            "--sender", "codex", "--recipient", "muse", "--task-id", "HELPER-REPAIR-20261009",
            "--id", "helper-failure-" + identity, "--file", str(body)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        stored = False
        if result.returncode == 0:
            try:
                receipt = json.loads(result.stdout)
            except (ValueError, TypeError):
                receipt = {}
            if receipt.get("id") == "helper-failure-" + identity:
                # A separate read-only connection verifies the committed row.
                with closing(sqlite3.connect(Path(database).resolve().as_uri()+"?mode=ro", uri=True)) as db:
                    row = db.execute("SELECT sender,recipient,body FROM messages WHERE id=?",
                                     (receipt["id"],)).fetchone()
                    stored = row == ("codex", "muse", notice["message"])
        app.save(notification_state="mailbox_stored_unconfirmed" if stored
                 else "mailbox_storage_unverified", notification_exit_code=result.returncode)
    except (OSError, subprocess.TimeoutExpired, sqlite3.Error) as error:
        app.save(notification_state="mailbox_post_failed", notification_error=type(error).__name__)


def queue_cycle(app):
    from supervisor import TurnGate, read_json, write_json, utc
    config, base = app.config, app.base
    if app.stopped():
        return False, "user_stop"
    # Until the one-time probe truly arrives in the app, no unattended loop starts.
    probe = read_json(base / "queue-probe-received.json", {})
    if probe.get("marker") != "HELPER-QUEUE-PROBE-20261009-A":
        app.save(mode="needs_user_action", last_error="queue_wake_not_yet_verified")
        return False, "requires_user_action"
    pending_path = base / "pending_wake.json"
    pending = read_json(pending_path)
    if pending and pending.get("state") not in ("completed", "reconciled_closed"):
        app.save(mode="needs_user_action", last_error="outstanding_wake_needs_reconciliation")
        return False, "requires_user_action"
    run_id = str(uuid.uuid4())
    cycle_minutes = config.get('overall_timeout_seconds', 900) / 60
    prompt = (f"Relay wake {run_id}. Continue the owner's authorized chat relay. "
              f"First read {base / 'worker_prompt.txt'}, then run worker_state.py begin --run-id {run_id} from {base}. "
              "If STOP exists or this wake is no longer current, stop without browser actions. "
              "Use the existing conversation and Chrome instance. Do not launch another agent. "
              f"Run ID: {run_id}. Separate startup/browser/send/reply deadlines; overall cap {cycle_minutes:g} minutes. "
              "Do not apply the obsolete two-minute-from-enqueue rule. Record begin before browser setup.")
    pending = {"run_id": run_id, "state": "enqueue_started", "created_at": utc(),
               "created_at_epoch": time.time(), "cycle_minutes": cycle_minutes}
    enqueued_monotonic = time.monotonic()
    # Persist BEFORE sending. Uncertain results never trigger a duplicate send.
    write_json(pending_path, pending)
    lifecycle = TurnGate(config["rollout_path"], Path(config["rollout_path"]).stat().st_size, None)
    command = [config["codex_exe"], "queue", "--thread", config["thread_id"], "--message", prompt]
    app.save(mode="queueing_wake", run_id=run_id, attempts=app.status["attempts"] + 1,
             browser_online=False, last_error=None)
    try:
        result = subprocess.run(command, cwd=config["cwd"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        diagnostic = {"at": utc(), "executable": config["codex_exe"], "cwd": config["cwd"],
                      "exit_code": result.returncode, "stdout": safe_error(result.stdout),
                      "stderr": safe_error(result.stderr)}
        write_json(base / "last_launch.json", diagnostic)
    except (OSError, subprocess.TimeoutExpired) as error:
        write_json(base / "last_launch.json", {"at": utc(), "error_type": type(error).__name__,
            "error": safe_error(str(error))})
        pending["state"] = "enqueue_outcome_uncertain"
        write_json(pending_path, pending)
        app.save(mode="needs_user_action", last_error="queue_outcome_uncertain")
        record_failure(app, "queue_outcome_uncertain")
        return False, "requires_user_action"
    match = re.search(r"Queued message ([0-9a-f-]+) for thread", result.stdout)
    if result.returncode != 0 or not match:
        pending["state"] = "enqueue_failed_or_uncertain"
        write_json(pending_path, pending)
        app.save(mode="needs_user_action", last_error="queue_not_confirmed", exit_code=result.returncode)
        record_failure(app, "queue_not_confirmed")
        return False, "requires_user_action"
    pending.update(state="queued", queue_id=match.group(1), queue_accepted_at_epoch=time.time())
    write_json(pending_path, pending)
    app.save(mode="waiting_for_real_browser_receipt", queue_id=match.group(1))
    while not app.stopped():
        lifecycle.poll()
        if lifecycle.cancelled:
            (base / "STOP").write_text("Stopped after user cancellation\n", encoding="utf-8")
            return False, "user_stop"
        receipt = read_json(base / "heartbeat.json", {})
        now = time.time()
        stage, reason, deadline = progress(pending, receipt, config, now)
        if clock_changed(pending['created_at_epoch'], enqueued_monotonic, now, time.monotonic()):
            reason = 'CLOCK_CHANGED_REQUIRES_RECONCILIATION'
        verified = verified_receipt(receipt, run_id, now,
                                    config["heartbeat_timeout_seconds"], config["extension_instance_id"])
        same_run = receipt.get("run_id") == run_id
        if same_run and receipt.get("stop_requested") is True:
            (base / "STOP").write_text("Stopped from relay chat\n", encoding="utf-8")
            return False, "user_stop"
        if same_run and receipt.get("requires_user_action") is True:
            app.save(mode="needs_user_action", browser_online=False,
                     last_error="browser_or_account_requires_attention")
            record_failure(app, "browser_or_account_requires_attention")
            return False, "requires_user_action"
        if same_run and receipt.get('extension_instance_id') != config['extension_instance_id']:
            reason = 'WRONG_BROWSER_INSTANCE_RECEIPT'
        if stage == 'COMPLETE' and not reason:
            # Validate health at completion; final-response latency cannot refresh a poll.
            completion_valid = verified_receipt(receipt, run_id, receipt['completed_at_epoch'],
                                        config['heartbeat_timeout_seconds'], config['extension_instance_id'])
            if not completion_valid:
                reason = 'COMPLETION_WITHOUT_FRESH_BROWSER_AND_ACK'
            elif receipt.get('worker_turn_id') in lifecycle.completed_turn_ids:
                pending["state"] = "completed"
                pending['completed_at'] = utc()
                write_json(pending_path, pending)
                archive = base/'cycle_history'
                archive.mkdir(exist_ok=True)
                timings = {
                    'queue_to_worker_seconds':receipt['worker_started_at_epoch']-pending['queue_accepted_at_epoch'],
                    'worker_to_browser_seconds':receipt['browser_read_at_epoch']-receipt['worker_started_at_epoch'],
                    'browser_to_send_seconds':receipt['send_confirmed_at_epoch']-receipt['browser_read_at_epoch'],
                    'send_to_reply_seconds':receipt['destination_replied_at_epoch']-receipt['send_confirmed_at_epoch'],
                    'total_seconds':receipt['completed_at_epoch']-pending['queue_accepted_at_epoch']}
                write_json(archive/(run_id+'.json'), {'pending':pending,'receipt':receipt,'verified':True,'timings':timings})
                app.after_worker_offset = lifecycle.offset
                app.save(mode="between_cycles", browser_online=False, last_error=None,
                         live_recovery_verified=True, supervised_cycle_verified=True,
                         stage='COMPLETE', last_completed_run_id=run_id, last_cycle_timings=timings)
                return True, None
            elif now >= pending['queue_accepted_at_epoch'] + config.get('overall_timeout_seconds',900):
                reason = 'TURN_COMPLETION_TIMEOUT'
        elif stage == 'REPLIED' and not verified and not reason:
            reason = 'BROWSER_HEARTBEAT_EXPIRED'
        if reason:
            from supervisor import ReceiptLock
            lock = ReceiptLock(base/'receipt.lock')
            try:
                latest = read_json(base/'heartbeat.json',{})
                if latest != receipt:
                    continue
                pending["state"] = "expired_requires_reconciliation"
                pending['failure_reason'] = reason
                write_json(pending_path, pending)
            finally:
                lock.close()
            archive = base/'cycle_history'
            archive.mkdir(exist_ok=True)
            write_json(archive/(run_id+'.json'), {'pending':pending,'receipt':receipt,'verified':False})
            app.save(mode="needs_user_action", stage=stage, browser_online=False,
                     live_recovery_verified=False, last_error=reason)
            record_failure(app, reason)
            return False, "requires_user_action"
        app.save(mode='waiting_for_'+stage.lower(), stage=stage, next_deadline_epoch=deadline,
                 browser_online=bool(verified), live_recovery_verified=bool(verified),
                 worker_started_at_epoch=receipt.get('worker_started_at_epoch') if same_run else None,
                 last_browser_check=receipt.get('checked_at_epoch') if same_run else None,
                 peers=receipt.get('peers',{}) if same_run else {})
        if not app.nap(2):
            break
    return False, "user_stop"
