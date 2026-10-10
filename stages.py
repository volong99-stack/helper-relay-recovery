"""Receipt validation and deadlines; never infer delivery from a running process."""
import math


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def clock_changed(start_wall, start_monotonic, now_wall, now_monotonic):
    return abs((now_wall-start_wall)-(now_monotonic-start_monotonic)) > 5


def progress(pending, receipt, config, now):
    accepted = pending.get('queue_accepted_at_epoch', pending['created_at_epoch'])
    overall = accepted + config.get('overall_timeout_seconds', 900)
    r = receipt if isinstance(receipt, dict) and receipt.get('run_id') == pending['run_id'] else {}
    started = r.get('worker_started_at_epoch')
    browser = r.get('browser_read_at_epoch')
    sent = r.get('send_confirmed_at_epoch')
    replied = r.get('destination_replied_at_epoch')
    chain = [('worker_started_at_epoch', started, config.get('startup_timeout_seconds', 600)),
             ('browser_read_at_epoch', browser, config.get('browser_timeout_seconds', 180)),
             ('send_confirmed_at_epoch', sent, config.get('send_timeout_seconds', 120)),
             ('destination_replied_at_epoch', replied, config.get('reply_timeout_seconds', 120))]
    previous = accepted
    missing = False
    reasons = ['WORKER_START_TIMEOUT', 'BROWSER_READ_TIMEOUT', 'SEND_UNCONFIRMED', 'REPLY_TIMEOUT']
    for i, (key, value, limit) in enumerate(chain):
        if value is None:
            missing = True
            continue
        if missing or not number(value) or not previous <= value <= now:
            return 'INVALID', 'INVALID_STAGE_RECEIPT', overall
        if value > min(previous + limit, overall):
            return 'EXPIRED', reasons[i], min(previous + limit, overall)
        previous = value
    stage = 'QUEUED'
    reason = 'WORKER_START_TIMEOUT'
    deadline = min(accepted + config.get('startup_timeout_seconds', 600), overall)
    if started is not None:
        stage, reason = 'STARTED', 'BROWSER_READ_TIMEOUT'
        deadline = min(started + config.get('browser_timeout_seconds', 180), overall)
    if browser is not None:
        stage, reason = 'BROWSER_READY', 'SEND_UNCONFIRMED'
        deadline = min(browser + config.get('send_timeout_seconds', 120), overall)
    if sent is not None:
        stage, reason = 'SENT', 'REPLY_TIMEOUT'
        deadline = min(sent + config.get('reply_timeout_seconds', 120), overall)
    if replied is not None:
        stage, reason, deadline = 'REPLIED', 'CYCLE_COMPLETION_TIMEOUT', overall
    if r.get('failure_reason'):
        return stage, r['failure_reason'], deadline
    if r.get('cycle_complete') and replied is None:
        return stage, 'CYCLE_ENDED_' + reason, deadline
    if r.get('cycle_complete') and replied is not None:
        finished = r.get('completed_at_epoch')
        if not number(finished) or not replied <= finished <= min(now, overall):
            return stage, 'INVALID_COMPLETION_RECEIPT', overall
        return 'COMPLETE', None, overall
    if now >= deadline:
        return stage, reason, deadline
    return stage, None, deadline
