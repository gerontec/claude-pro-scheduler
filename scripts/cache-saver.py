#!/usr/bin/env python3
"""
Claude Context Cache Saver
Sichert Job-Kontext alle 3 Minuten in claude_context_cache (MariaDB).
Notfall-Snapshot wenn Session-Limit >95% (via fetch-usage.py).

Cron: */3 * * * * python3 /home/gh/cache-saver.py >> /tmp/cache-saver.log 2>&1
"""
import pymysql
import json
import sys
import glob
import os
import argparse
from datetime import datetime
from pathlib import Path

DB = dict(host='localhost', user='gh', password='a12345',
          database='wagodb', charset='utf8mb4')
USAGE_FILE   = Path.home() / '.claude_weekly_usage.json'
SESSIONS_DIR = Path.home() / '.claude' / 'projects' / '-home-gh'
LOG_PREFIX   = lambda: f'[{datetime.now():%H:%M:%S}]'


# ── DB-Helfer ──────────────────────────────────────────────────────────────

def get_conn():
    return pymysql.connect(**DB, cursorclass=pymysql.cursors.DictCursor)


def upsert_cache(scope: str, context: dict, updated_by: str = None,
                 ttl_hours: int = 168, summary: str = None):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO claude_context_cache
                    (scope, context_json, updated_by, summary, ttl_hours)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    context_json = VALUES(context_json),
                    updated_by   = VALUES(updated_by),
                    summary      = VALUES(summary),
                    ttl_hours    = VALUES(ttl_hours),
                    version      = version + 1,
                    updated_at   = NOW()
            """, (scope, json.dumps(context, ensure_ascii=False, default=str),
                  updated_by, summary, ttl_hours))
        conn.commit()
        print(f'{LOG_PREFIX()} ✓ cache[{scope}] gespeichert  (by={updated_by})', flush=True)
    finally:
        conn.close()


# ── Session-Kontext Extraktion ─────────────────────────────────────────────

def extract_session_context(jsonl_path: str) -> dict:
    """
    Liest eine Claude-Code-Session JSONL und extrahiert die Q&A-Paare
    (User-Fragen + Assistant-Text-Antworten) als strukturierten Kontext.
    Tool-Calls werden übersprungen — nur Erkenntnisse/Antworten bleiben.
    """
    qa_pairs   = []
    files_mod  = set()
    cur_user   = None

    with open(jsonl_path, encoding='utf-8') as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue

            t = d.get('type')

            # User-Nachricht: nur echte Texte (str), keine Tool-Results (list)
            if t == 'user':
                parts = d.get('message', {}).get('content', '')
                if isinstance(parts, str) and parts.strip():
                    cur_user = parts.strip()   # echte Nutzerfrage
                # list → tool_result → ignorieren, cur_user NICHT überschreiben

            # Assistant-Antwort: nur Text-Blöcke ≥ 30 Zeichen sammeln
            elif t == 'assistant':
                content = d.get('message', {}).get('content', [])
                texts = [c['text'] for c in content
                         if isinstance(c, dict) and c.get('type') == 'text'
                         and len(c.get('text','')) >= 30]
                if texts and cur_user:
                    qa_pairs.append({
                        'q': cur_user[:400],
                        'a': '\n'.join(texts)[:1200],
                    })
                    cur_user = None   # nach erstem Match zurücksetzen

            # Modifizierte Dateien aus file-history-snapshot
            elif t == 'file-history-snapshot':
                backups = d.get('snapshot', {}).get('trackedFileBackups', {})
                if isinstance(backups, dict):
                    files_mod.update(backups.keys())
                elif isinstance(backups, list):
                    for b in backups:
                        fp = (b.get('filePath') or b.get('path','')) if isinstance(b,dict) else b
                        if fp:
                            files_mod.add(fp)

    return {
        'session_id':    os.path.basename(jsonl_path).replace('.jsonl', ''),
        'session_date':  datetime.fromtimestamp(os.path.getmtime(jsonl_path)).strftime('%Y-%m-%d'),
        'extracted_at':  datetime.now().isoformat(),
        'entry_count':   len(qa_pairs),
        'files_modified': sorted(files_mod),
        'qa':            qa_pairs,
    }


def snapshot_session(session_id: str = None):
    """
    Extrahiert die neueste (oder angegebene) Claude-Code-Session und
    speichert die Q&A-Erkenntnisse in claude_context_cache scope='session'.
    """
    if not SESSIONS_DIR.exists():
        print(f'{LOG_PREFIX()} WARN: Sessions-Dir nicht gefunden: {SESSIONS_DIR}', flush=True)
        return

    pattern = str(SESSIONS_DIR / (f'{session_id}.jsonl' if session_id else '*.jsonl'))
    files   = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)

    if not files:
        print(f'{LOG_PREFIX()} WARN: Keine Session-JSONL gefunden', flush=True)
        return

    jsonl = files[0]
    print(f'{LOG_PREFIX()} Extrahiere Session: {os.path.basename(jsonl)}', flush=True)

    ctx  = extract_session_context(jsonl)
    n    = ctx['entry_count']
    mods = ctx['files_modified']

    # Kurze Summary für schnelles Lesen durch Folge-Jobs
    files_short = ', '.join(os.path.basename(p) for p in mods[:6])
    summary = f"{n} Q&A-Paare | {ctx['session_date']} | Dateien: {files_short}"[:500]

    upsert_cache(
        scope      = 'session',
        context    = ctx,
        updated_by = f"cache-saver/session/{ctx['session_id'][:8]}",
        summary    = summary,
        ttl_hours  = 336,   # 2 Wochen
    )
    print(f'{LOG_PREFIX()} Session-Cache: {n} Q&A, {len(mods)} Dateien', flush=True)


# ── Snapshot-Funktionen ────────────────────────────────────────────────────

def snapshot_running(job_id=None):
    """Laufende/wartende Jobs sichern – optional gefiltert auf job_id."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            where = f'AND id = {int(job_id)}' if job_id else ''
            cur.execute(f"""
                SELECT id, model, prompt, status, started_at,
                       input_tokens, output_tokens, cache_tokens
                FROM   claude_pro_batch
                WHERE  status IN ('running', 'queued')
                       {where}
                ORDER  BY created_at DESC
                LIMIT  30
            """)
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return

    ctx = {
        'snapshot_at': datetime.now().isoformat(),
        'trigger':     'running-jobs',
        'jobs': [
            {
                'id':           r['id'],
                'model':        r['model'],
                'status':       r['status'],
                'started_at':   str(r['started_at']) if r['started_at'] else None,
                'prompt_head':  (r['prompt'] or '')[:300],
                'tokens': {
                    'input':  r['input_tokens']  or 0,
                    'output': r['output_tokens'] or 0,
                    'cache':  r['cache_tokens']  or 0,
                }
            }
            for r in rows
        ]
    }
    scope      = f'job-{job_id}' if job_id else 'batch'
    updated_by = f'cache-saver/job-{job_id}' if job_id else 'cache-saver/cron'
    upsert_cache(scope, ctx, updated_by=updated_by)


def snapshot_recent_results():
    """Letzte abgeschlossene Jobs (24 h) als globalen Kontext-Cache sichern."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, model, prompt, result, finished_at,
                       input_tokens, output_tokens, cache_tokens, cost_usd
                FROM   claude_pro_batch
                WHERE  status = 'done'
                  AND  finished_at > NOW() - INTERVAL 24 HOUR
                ORDER  BY finished_at DESC
                LIMIT  50
            """)
            rows = cur.fetchall()
    finally:
        conn.close()

    ctx = {
        'snapshot_at': datetime.now().isoformat(),
        'trigger':     'recent-results',
        'results': [
            {
                'id':           r['id'],
                'model':        r['model'],
                'finished_at':  str(r['finished_at']),
                'prompt_head':  (r['prompt'] or '')[:300],
                'result_head':  (r['result'] or '')[:600],
                'tokens': {
                    'input':  r['input_tokens']  or 0,
                    'output': r['output_tokens'] or 0,
                    'cache':  r['cache_tokens']  or 0,
                },
                'cost_usd': float(r['cost_usd'] or 0),
            }
            for r in rows
        ]
    }

    # Weekly-Usage anhängen
    if USAGE_FILE.exists():
        try:
            ctx['weekly_usage'] = json.loads(USAGE_FILE.read_text())
        except Exception:
            pass

    upsert_cache('global', ctx, updated_by='cache-saver/cron')


def emergency_snapshot(trigger: str = 'unknown'):
    """Vollständiger Notfall-Snapshot (Session-Limit >95%)."""
    print(f'{LOG_PREFIX()} ⚠  NOTFALL-SNAPSHOT (trigger={trigger})', flush=True)
    snapshot_session()
    snapshot_running()
    snapshot_recent_results()
    # Usage-Datei direkt auch einspeichern
    if USAGE_FILE.exists():
        try:
            usage = json.loads(USAGE_FILE.read_text())
            usage['emergency_trigger'] = trigger
            usage['emergency_at']      = datetime.now().isoformat()
            upsert_cache('usage-emergency', usage,
                         updated_by=f'cache-saver/{trigger}', ttl_hours=48)
        except Exception as e:
            print(f'{LOG_PREFIX()} WARN usage-load: {e}', flush=True)


# ── main ───────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description='Claude Context Cache Saver')
    ap.add_argument('--scope',        default=None,  help='Expliziter Scope')
    ap.add_argument('--job-id',       default=None,  help='Job-ID des laufenden Jobs')
    ap.add_argument('--context-json', default=None,  help='JSON-String direkt speichern')
    ap.add_argument('--emergency',    action='store_true', help='Notfall-Snapshot')
    ap.add_argument('--session',      action='store_true', help='Session-Kontext extrahieren')
    ap.add_argument('--session-id',   default=None,  help='Bestimmte Session-UUID')
    ap.add_argument('--trigger',      default='cron', help='Auslöser (für updated_by)')
    args = ap.parse_args()

    if args.emergency:
        emergency_snapshot(trigger=args.trigger)
        return

    # Direkter Kontexteintrag (z.B. aus batch-poller.sh)
    if args.context_json and args.scope:
        try:
            ctx = json.loads(args.context_json)
        except json.JSONDecodeError:
            ctx = {'raw': args.context_json}
        updated_by = f'job-{args.job_id}' if args.job_id else args.trigger
        upsert_cache(args.scope, ctx, updated_by=updated_by)
        return

    # Job-spezifischer Snapshot (aus batch-poller.sh aufrufbar)
    if args.job_id:
        snapshot_running(job_id=args.job_id)
        return

    # Expliziter Session-Snapshot
    if args.session:
        snapshot_session(session_id=args.session_id)
        return

    # Standard Cron-Lauf: Session + laufende Jobs + Ergebnisse
    snapshot_session()
    snapshot_running()
    snapshot_recent_results()


if __name__ == '__main__':
    main()
