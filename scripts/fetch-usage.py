#!/usr/bin/env python3
"""
Startet claude, bestätigt Trust-Dialog, sendet /usage,
parst Wochenlimit und speichert in ~/.claude_weekly_usage.json
Cron: */30 * * * * python3 /home/gh/fetch-usage.py >> /tmp/fetch-usage.log 2>&1
"""
import pexpect, re, json, sys, time
from datetime import datetime
from pathlib import Path

USAGE_FILE = Path.home() / '.claude_weekly_usage.json'

def strip_ansi(t):
    t = re.sub(r'\x1b\[[0-9;?>=!]*[A-Za-z@]', '', t)
    t = re.sub(r'\x1b[()][AB012]', '', t)
    t = re.sub(r'\x1b[=>]', '', t)
    t = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]', '', t)
    return t

def parse_usage(text):
    clean = strip_ansi(text)
    clean = re.sub(r'[█▌▐▒░▓▄▀■□]', 'X', clean)
    result = {}
    pcts = re.findall(r'(\d+)%\s*used', clean)
    if len(pcts) >= 2:
        result['session_pct'] = int(pcts[0])
        result['usage_pct']   = int(pcts[1])
    elif len(pcts) == 1:
        result['usage_pct'] = int(pcts[0])
    resets = re.findall(r'Resets\s+([^\r\n\x1b]{5,60})', clean)
    if len(resets) >= 2:
        result['session_reset']  = resets[0].strip()
        result['week_reset_raw'] = resets[1].strip()
    elif len(resets) == 1:
        result['week_reset_raw'] = resets[0].strip()
    return result

def run():
    existing = {}
    if USAGE_FILE.exists():
        try:
            existing = json.loads(USAGE_FILE.read_text())
        except Exception:
            pass

    print(f'[{datetime.now():%H:%M:%S}] Starte claude …', flush=True)
    child = pexpect.spawn('claude', encoding='utf-8', timeout=45,
                          dimensions=(50, 160))
    child.delaybeforesend = 0.3

    # ── Trust-Dialog bestätigen ─────────────────────────────
    try:
        child.expect(r'cancel', timeout=15)
        child.send('\r')
        print(f'[{datetime.now():%H:%M:%S}] Trust bestätigt', flush=True)
    except pexpect.TIMEOUT:
        print(f'[{datetime.now():%H:%M:%S}] Kein Trust-Dialog', flush=True)

    # ── Haupt-Prompt ❯ abwarten ─────────────────────────────
    try:
        child.expect(r'❯', timeout=18)
        print(f'[{datetime.now():%H:%M:%S}] Prompt ❯ erkannt', flush=True)
    except pexpect.TIMEOUT:
        print(f'[{datetime.now():%H:%M:%S}] Timeout auf ❯, sende trotzdem', flush=True)

    time.sleep(0.5)

    # ── /usage senden ───────────────────────────────────────
    child.send('/usage\r')
    print(f'[{datetime.now():%H:%M:%S}] /usage gesendet', flush=True)

    # ── Ausgabe bis "Extra usage" oder 15s sammeln ──────────
    collected = ''
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            child.expect(r'\n', timeout=1.5)
            line = child.before + '\n'
            collected += line
            if 'Extra usage' in strip_ansi(line):
                time.sleep(0.5)
                break
        except pexpect.TIMEOUT:
            if re.search(r'\d+%\s*used', strip_ansi(collected)):
                break

    print(f'[{datetime.now():%H:%M:%S}] {len(collected)} Zeichen gesammelt', flush=True)

    # ── Aufräumen ───────────────────────────────────────────
    child.sendcontrol('c')
    child.send('/exit\r')
    try:
        child.expect(pexpect.EOF, timeout=5)
    except Exception:
        pass
    child.close()

    # ── Parsen & Speichern ──────────────────────────────────
    parsed = parse_usage(collected)
    print(f'[{datetime.now():%H:%M:%S}] Geparst: {parsed}', flush=True)

    if not parsed:
        print('FEHLER: Keine Daten. Raw:')
        print(repr(strip_ansi(collected)[:600]))
        sys.exit(1)

    existing.update(parsed)
    existing['pct_snapshot_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    USAGE_FILE.write_text(json.dumps(existing, indent=2))
    print(f'[{datetime.now():%H:%M:%S}] ✓  Woche {parsed.get("usage_pct","?")}%  '
          f'Session {parsed.get("session_pct","?")}%  → {USAGE_FILE}', flush=True)

    # ── Automatischer Compact + Cache-Sicherung bei hohem Session-Limit ──
    import subprocess
    session_pct = parsed.get('session_pct', 0)

    if session_pct >= 80:
        print(f'[{datetime.now():%H:%M:%S}] ⚡ Session {session_pct}% – starte /compact Sicherung', flush=True)
        subprocess.Popen(
            ['python3', '/home/gh/compact-cache.py'],
            stdout=open('/tmp/compact-cache.log', 'a'),
            stderr=subprocess.STDOUT
        )

    if session_pct >= 95:
        print(f'[{datetime.now():%H:%M:%S}] ⚠  Session {session_pct}% – Notfall cache-saver', flush=True)
        subprocess.Popen(
            ['python3', '/home/gh/cache-saver.py',
             '--emergency', '--trigger', f'fetch-usage-session-{session_pct}pct'],
            stdout=open('/tmp/cache-saver.log', 'a'),
            stderr=subprocess.STDOUT
        )

if __name__ == '__main__':
    run()
