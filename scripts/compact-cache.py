#!/usr/bin/env python3
"""
Claude Code /compact → MariaDB Cache
Resumiert die letzte (oder angegebene) Session via --fork-session,
sendet /compact, fängt den Summary-Text ab und speichert ihn in
claude_context_cache scope='session-compact'.

Manuell:  python3 /home/gh/compact-cache.py
Mit ID:   python3 /home/gh/compact-cache.py --session-id <uuid>
Cron:     */3 * * * * python3 /home/gh/compact-cache.py >> /tmp/compact-cache.log 2>&1
"""
import pexpect
import pymysql
import re
import json
import time
import glob
import os
import argparse
from datetime import datetime
from pathlib import Path

DB = dict(host='localhost', user='gh', password='a12345',
          database='wagodb', charset='utf8mb4')
SESSIONS_DIR = Path.home() / '.claude' / 'projects' / '-home-gh'
LOG = lambda msg: print(f'[{datetime.now():%H:%M:%S}] {msg}', flush=True)

ANSI = re.compile(r'\x1b\[[0-9;?>=!]*[A-Za-z@]|\x1b[()][AB012]|\x1b[=>]|[\x00-\x08\x0b-\x1f\x7f]|[█▌▐▒░▓▄▀■□●◆→←]')

def clean(t):
    return ANSI.sub('', t).strip()


# ── DB ────────────────────────────────────────────────────────────────────

def save_compact(summary_text: str, session_id: str, raw_output: str):
    ctx = {
        'session_id':   session_id,
        'compacted_at': datetime.now().isoformat(),
        'summary':      summary_text,
        'raw_output':   raw_output[:8000],
    }
    short = summary_text[:497] + '…' if len(summary_text) > 497 else summary_text
    conn = pymysql.connect(**DB, cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO claude_context_cache
                    (scope, context_json, updated_by, summary, ttl_hours)
                VALUES ('session-compact', %s, %s, %s, 336)
                ON DUPLICATE KEY UPDATE
                    context_json = VALUES(context_json),
                    updated_by   = VALUES(updated_by),
                    summary      = VALUES(summary),
                    version      = version + 1,
                    updated_at   = NOW()
            """, (json.dumps(ctx, ensure_ascii=False),
                  f'compact/{session_id[:8]}',
                  short))
        conn.commit()
        LOG(f'✓ cache[session-compact] gespeichert ({len(summary_text)} Zeichen)')
    finally:
        conn.close()


# ── Session-ID ermitteln ──────────────────────────────────────────────────

def latest_session_id() -> str | None:
    files = sorted(
        glob.glob(str(SESSIONS_DIR / '*.jsonl')),
        key=os.path.getmtime, reverse=True
    )
    if not files:
        return None
    return os.path.basename(files[0]).replace('.jsonl', '')


# ── /compact via pexpect ──────────────────────────────────────────────────

def run_compact(session_id: str) -> tuple[str, str]:
    """
    Startet claude --resume <id> --fork-session, sendet /compact,
    sammelt Output. Gibt (summary_text, raw_output) zurück.
    """
    cmd = f'claude --resume {session_id} --fork-session'
    LOG(f'Starte: {cmd}')

    child = pexpect.spawn(cmd, encoding='utf-8', timeout=120,
                          dimensions=(60, 200))
    child.delaybeforesend = 0.4
    collected = ''

    # ── Trust-Dialog ──────────────────────────────────────────────────────
    try:
        child.expect(r'cancel', timeout=15)
        child.send('\r')
        LOG('Trust bestätigt')
    except pexpect.TIMEOUT:
        LOG('Kein Trust-Dialog')

    # ── Prompt ❯ abwarten ─────────────────────────────────────────────────
    try:
        child.expect(r'❯', timeout=30)
        LOG('Prompt ❯ erkannt')
    except pexpect.TIMEOUT:
        LOG('Timeout auf ❯ — sende trotzdem')

    time.sleep(0.6)

    # ── /compact senden ───────────────────────────────────────────────────
    child.send('/compact\r')
    LOG('/compact gesendet')

    # ── Output sammeln bis neues ❯ oder 90s ──────────────────────────────
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            child.expect(r'\n', timeout=2)
            line = child.before + '\n'
            collected += line
            c = clean(line)
            # Fertig wenn neuer Prompt erscheint nach Content
            if len(collected) > 200 and re.search(r'❯\s*$', c):
                time.sleep(0.3)
                break
        except pexpect.TIMEOUT:
            if len(collected) > 100:
                break
        except pexpect.EOF:
            break

    LOG(f'{len(collected)} Zeichen gesammelt')

    # ── Aufräumen ─────────────────────────────────────────────────────────
    try:
        child.send('/exit\r')
        child.expect(pexpect.EOF, timeout=8)
    except Exception:
        pass
    child.close()

    return collected


# ── Summary aus Output extrahieren ───────────────────────────────────────

def extract_summary(raw: str) -> str:
    """
    Bereinigt den /compact-Output:
    - Entfernt ANSI, Spinner-Zeilen, leere Zeilen am Anfang/Ende
    - Entfernt die '/compact'-Zeile selbst und die abschließende ❯-Zeile
    - Gibt den Fließtext zurück
    """
    lines = []
    for line in raw.splitlines():
        c = clean(line)
        if not c:
            continue
        if c in ('/compact', '❯', '> '):
            continue
        if re.match(r'^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s', c):   # Spinner
            continue
        if re.match(r'^(Compacting|Compact|Context reduced|tokens)', c, re.I):
            continue
        lines.append(c)

    return '\n'.join(lines).strip()


# ── main ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--session-id', default=None, help='UUID der zu kompaktierenden Session')
    args = ap.parse_args()

    sid = args.session_id or latest_session_id()
    if not sid:
        LOG('FEHLER: Keine Session gefunden')
        return

    LOG(f'Session: {sid}')
    raw = run_compact(sid)

    if not raw.strip():
        LOG('FEHLER: Kein Output von /compact')
        return

    summary = extract_summary(raw)
    LOG(f'Summary: {len(summary)} Zeichen')
    if len(summary) < 20:
        LOG(f'WARN: Summary zu kurz, Raw: {repr(raw[:300])}')
        summary = raw[:2000]   # Fallback: Raw nehmen

    save_compact(summary, sid, raw)


if __name__ == '__main__':
    main()
