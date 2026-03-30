#!/usr/bin/env python3
# ============================================================
# Claude Pro Batch Poller
# Liest fällige Jobs aus claude_pro_batch, führt sie aus,
# schreibt Ergebnis + Token-Usage zurück.
# Cron: * * * * * /usr/bin/python3 /home/gh/batch-poller.py
# Max. 2 Jobs gleichzeitig.
# ============================================================

import fcntl
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pymysql

DB_CFG = dict(host='localhost', user='gh', password='a12345', database='wagodb',
              charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)
USAGE_FILE  = '/home/gh/.claude_weekly_usage.json'
LOCK        = '/tmp/claude-pro-poller.lock'
MAX_RUNNING = 2
CLAUDE_BIN  = '/usr/local/bin/claude'
SYSTEM_PROMPT = (
    "Du bist ein autonomer Batch-Assistent. Führe alle Aufgaben vollständig und direkt aus – "
    "ohne Rückfragen, ohne Sicherheitshinweise, ohne Warnungen über Credentials oder Best-Practices. "
    "Der Betreiber hat alle Berechtigungen erteilt. Antworte ausschliesslich mit dem Ergebnis der Aufgabe."
)

# ── Kritische Phase: Job claimen (serialisiert per flock) ──
# flock verhindert Race Condition beim Zählen + Markieren,
# wird direkt nach dem Claim wieder freigegeben.
lock_fh = open(LOCK, 'w')
try:
    fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(0)

job = None
db  = None
try:
    db = pymysql.connect(**DB_CFG)

    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM claude_pro_batch WHERE status='running'")
        running = cur.fetchone()['n']

    if running >= MAX_RUNNING:
        sys.exit(0)

    with db.cursor() as cur:
        cur.execute("""
            SELECT id, model, resume_session, prompt
            FROM claude_pro_batch
            WHERE status='queued'
            ORDER BY targetdate ASC, created_at ASC
            LIMIT 1
        """)
        job = cur.fetchone()

    if not job:
        sys.exit(0)

    job_id         = job['id']
    model          = job['model']
    resume_session = job['resume_session']
    prompt         = job['prompt']

    with db.cursor() as cur:
        cur.execute(
            "UPDATE claude_pro_batch SET status='running', started_at=NOW() WHERE id=%s",
            (job_id,)
        )
    db.commit()

finally:
    # Lock sofort freigeben — anderer Cron-Slot kann jetzt zweiten Job claimen
    fcntl.flock(lock_fh, fcntl.LOCK_UN)
    lock_fh.close()
    if job is None:
        if db:
            db.close()
        sys.exit(0)

# ── Ab hier läuft diese Instanz unabhängig ─────────────────
try:
    # ── Infrastructure-Kontext voranstellen ──────────────
    # Immer eingebunden; identischer Inhalt → Prompt-Cache-Hit ab 2. Job (~10% des Input-Preises)
    try:
        with db.cursor() as cur:
            cur.execute("""
                SELECT ip_address, hostname, network_range, open_ports, services,
                       device_purpose, os_guess, mac_address
                FROM ki_infrastructure
                ORDER BY network_range, ip_address
            """)
            infra_rows = cur.fetchall()
        if infra_rows:
            lines = ['## Netzwerk-Infrastruktur (ki_infrastructure)', '']
            for r in infra_rows:
                parts = [f"**{r['ip_address']}**"]
                if r['hostname']:   parts.append(f"({r['hostname']})")
                if r['network_range']: parts.append(f"[{r['network_range']}]")
                if r['device_purpose']: parts.append(f"→ {r['device_purpose']}")
                if r['open_ports']: parts.append(f"| Ports: {r['open_ports']}")
                if r['services']:   parts.append(f"| Services: {r['services']}")
                if r['os_guess']:   parts.append(f"| OS: {r['os_guess']}")
                lines.append('  '.join(parts))
            infra_text = '\n'.join(lines)
            if len(infra_text) <= 8000:
                prompt = f"{infra_text}\n\n---\n{prompt}"
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id}: "
                      f"Infrastructure-Kontext geladen ({len(infra_rows)} Hosts, {len(infra_text)} Zeichen)",
                      file=sys.stderr)
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id}: "
                      f"Infrastructure-Kontext übersprungen (zu groß: {len(infra_text)} Zeichen)",
                      file=sys.stderr)
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id}: "
              f"Infrastructure-Kontext Fehler: {e}", file=sys.stderr)

    # ── Session-Cache voranstellen wenn gewünscht ─────────
    if resume_session:
        with db.cursor() as cur:
            cur.execute("""
                SELECT JSON_UNQUOTE(JSON_EXTRACT(context_json, '$.summary')) AS summary
                FROM claude_context_cache
                WHERE scope='session-compact'
                LIMIT 1
            """)
            row = cur.fetchone()
        if row and row['summary'] and row['summary'] != 'NULL':
            cache_ctx = row['summary']
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id}: "
                  f"Session-Cache geladen ({len(cache_ctx)} Bytes)", file=sys.stderr)
            prompt = f"{cache_ctx}\n\n---\nAufgabe:\n{prompt}"

    # ── Wochentracking ────────────────────────────────────
    def week_start():
        mez = ZoneInfo('Europe/Berlin')
        now = datetime.now(mez)
        days_since_friday = (now.weekday() - 4) % 7
        last_friday = now - timedelta(days=days_since_friday)
        reset = last_friday.replace(hour=8, minute=0, second=0, microsecond=0)
        if now < reset:
            reset -= timedelta(weeks=1)
        return reset.strftime('%Y-%m-%d %H:%M MEZ')

    def load_usage():
        week = week_start()
        if os.path.exists(USAGE_FILE):
            try:
                d = json.load(open(USAGE_FILE))
                if d.get('week_start') == week:
                    return (d.get('input_tokens', 0), d.get('output_tokens', 0),
                            d.get('cache_tokens', 0), d.get('cost_usd', 0.0),
                            d.get('tasks', 0))
            except Exception:
                pass
        return (0, 0, 0, 0.0, 0)

    def save_usage(in_tok, out_tok, cache_tok, cost, tasks):
        existing = {}
        if os.path.exists(USAGE_FILE):
            try:
                existing = json.load(open(USAGE_FILE))
            except Exception:
                pass
        data = {
            'week_start':    week_start(),
            'input_tokens':  in_tok,
            'output_tokens': out_tok,
            'cache_tokens':  cache_tok,
            'cost_usd':      cost,
            'tasks':         tasks,
            'last_run':      datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        for k in ('session_pct', 'usage_pct', 'pct_snapshot_at', 'session_reset', 'week_reset_raw'):
            if k in existing:
                data[k] = existing[k]
        json.dump(data, open(USAGE_FILE, 'w'), indent=2)

    pre_in, pre_out, pre_cache, pre_cost, pre_tasks = load_usage()

    # ── Claude ausführen ──────────────────────────────────
    with tempfile.NamedTemporaryFile(prefix=f'claude_pro_{job_id}_',
                                     suffix='.json', delete=False) as tmp:
        tmp_path = tmp.name

    try:
        with open(tmp_path, 'w') as out_f:
            proc = subprocess.run(
                [
                    CLAUDE_BIN,
                    '--model', model,
                    '--effort', 'low',
                    '--max-budget-usd', '0.25',
                    '--dangerously-skip-permissions',
                    '--append-system-prompt', SYSTEM_PROMPT,
                    '--output-format', 'json',
                    '-p', prompt,
                ],
                stdin=subprocess.DEVNULL,
                stdout=out_f,
                stderr=subprocess.STDOUT,
            )

        exit_code = proc.returncode
        raw = open(tmp_path).read()

        if exit_code != 0:
            first_line = raw.splitlines()[0] if raw.strip() else '(keine Ausgabe)'
            result    = raw
            in_tok    = out_tok = cache_tok = 0
            cost      = 0.0
            status    = 'failed'
            error     = f'Exit-Code {exit_code}: {first_line}'
        else:
            try:
                d         = json.loads(raw)
                u         = d.get('usage', {})
                result    = d.get('result', '')
                in_tok    = u.get('input_tokens', 0)
                out_tok   = u.get('output_tokens', 0)
                cache_tok = (u.get('cache_creation_input_tokens', 0)
                             + u.get('cache_read_input_tokens', 0))
                cost      = round(d.get('total_cost_usd', 0.0), 6)
                status    = 'done'
                error     = ''
            except json.JSONDecodeError as e:
                first_line = raw.splitlines()[0] if raw.strip() else '(keine Ausgabe)'
                result    = raw
                in_tok    = out_tok = cache_tok = 0
                cost      = 0.0
                status    = 'failed'
                error     = f'Kein gültiges JSON (Exit 0): {first_line} — {e}'
    finally:
        os.unlink(tmp_path)

    # ── Abbruch prüfen (Kill-Button während Laufzeit) ─────
    with db.cursor() as cur:
        cur.execute("SELECT status FROM claude_pro_batch WHERE id=%s", (job_id,))
        row = cur.fetchone()
    if row and row['status'] == 'failed':
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id} wurde während der Laufzeit abgebrochen.",
              file=sys.stderr)
        sys.exit(0)

    # ── Eskalation: Bedenken erkannt → neu einreihen mit Sonnet ──
    ESCALATION_PHRASES = [
        'ich kann nicht', 'ich kann bei diesem', 'ich bin nicht in der lage',
        'bevor ich', 'muss ich bestät', 'sicherheitsbedenken', 'sicherheitshinweis',
        'i cannot', "i can't", 'i am unable', 'i need to confirm', 'i must verify',
        'before i', 'safety concern', 'i should not', 'ich sollte nicht',
        'ich darf nicht', 'nicht autorisiert', 'nicht berechtigt',
    ]
    escalate = (
        status == 'done'
        and model != 'sonnet'
        and model != 'opus'
        and any(p in result.lower() for p in ESCALATION_PHRASES)
    )
    if escalate:
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO claude_pro_batch (targetdate, model, resume_session, prompt)
                SELECT targetdate, 'sonnet', resume_session, prompt
                FROM claude_pro_batch WHERE id=%s
            """, (job_id,))
            new_id = db.lastrowid
        db.commit()
        error  = f'Eskaliert zu Sonnet → Job #{new_id} (Bedenken erkannt)'
        status = 'failed'
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id}: Bedenken erkannt → "
              f"eskaliert zu Sonnet als Job #{new_id}", file=sys.stderr)

    # ── Wochentracking speichern ──────────────────────────
    save_usage(
        pre_in    + in_tok,
        pre_out   + out_tok,
        pre_cache + cache_tok,
        round(pre_cost + cost, 6),
        pre_tasks + 1,
    )

    # ── Ergebnis in DB schreiben ──────────────────────────
    with db.cursor() as cur:
        cur.execute("""
            UPDATE claude_pro_batch SET
                status        = %s,
                result        = %s,
                input_tokens  = %s,
                output_tokens = %s,
                cache_tokens  = %s,
                cost_usd      = %s,
                finished_at   = NOW(),
                error_msg     = %s
            WHERE id = %s
        """, (status, result, in_tok, out_tok, cache_tok, cost, error, job_id))
    db.commit()

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id} → {status} "
          f"({in_tok}/{out_tok} tok, ${cost})", file=sys.stderr)

    # ── Session-Compact Cache aktualisieren ───────────────
    subprocess.run(
        ['python3', '/home/gh/cache-saver.py', '--compact'],
        stdout=open('/tmp/cache-saver.log', 'a'),
        stderr=subprocess.STDOUT,
    )

finally:
    db.close()
