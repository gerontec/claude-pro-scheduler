#!/usr/bin/env python3
"""
trim-cache.py  –  Kürzt claude_context_cache[session-compact] auf < MAX_CHARS.

Algorithmus (3 Pässe):
  Pass 1 – Immer: ctrl+o-Artefakte, Spinner, reine Leerzeilen entfernen
  Pass 2 – P3-Bereinigung: Tool-Traces, ⎿-Blöcke, Diff-Fragmente wegwerfen
  Pass 3 – P2-Reduktion: ältere Nicht-P1-Zeilen von hinten kürzen
  Notfall: harter Schnitt mit Marker

Prioritäten pro Zeile:
  P1 (immer behalten):  ❯-Prompts, SQL-DDL, Dateipfade, URLs, kurze Sätze
  P2 (behalten wenn Platz): Assistenten-Antworten, Code-Blöcke
  P3 (zuerst wegwerfen): Tool-Call-Traces, ⎿-Blöcke, Diff-Zeilen, Spinner

Aufruf:  python3 trim-cache.py [--dry-run] [--max N]
Cron:    */5 * * * * python3 /home/gh/claude-pro-scheduler/scripts/trim-cache.py \
                     >> /tmp/trim-cache.log 2>&1
"""
import re
import json
import argparse
import pymysql
from datetime import datetime

MAX_CHARS = 50_000
DB = dict(host='localhost', user='gh', password='a12345',
          database='wagodb', charset='utf8mb4')

LOG = lambda msg: print(f'[{datetime.now():%H:%M:%S}] {msg}', flush=True)

# ── Regex-Muster ───────────────────────────────────────────────────────────

# Tool-Call-Traces: zusammengeklappte CamelCase-Wörter mit (...)
_TOOLS = r'(Bash|Write|Read|Edit|Glob|Grep|Explore|Update|Search|Task|Agent|Skill|WebFetch|WebSearch|NotebookEdit|TodoWrite|ToolSearch)'
RE_TOOL_TRACE  = re.compile(rf'^{_TOOLS}\(.{{0,400}}\)\s*$')

# ⎿  Output-Blöcke (Tool-Ergebnisse)
RE_BREVE       = re.compile(r'^⎿\s*')

# (ctrl+o...) Inline-Artefakte
RE_CTRL_O      = re.compile(r'\(ctrl\+o[^)]*\)')

# Diff-Zeilen: "123 + content" oder "123 - content"
RE_DIFF        = re.compile(r'^\d+\s+[+\-]\s')

# Spinner-Symbole am Zeilenanfang
RE_SPINNER     = re.compile(r'^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏✻⠿]\s')

# Reine Zahlen/Token-Counter
RE_ONLY_NUMS   = re.compile(r'^\d[\d\s·ktokens%]*$', re.I)

# "Done(N tool uses · X tokens · Ys)" Zusammenfassungszeilen
RE_DONE_LINE   = re.compile(r'^Done\(\d+\s+tool', re.I)

# ── Klassifizierung ────────────────────────────────────────────────────────

def classify(line: str) -> int:
    """1=immer, 2=wenn Platz, 3=wegwerfen"""
    s = line.strip()
    if not s:
        return 3

    # P1: Benutzer-Prompt-Marker
    if s.startswith('❯') or s.startswith('> '):
        return 1

    # P1: SQL DDL / DML Keywords
    if re.match(r'^(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE|SELECT|SHOW|DESCRIBE)\b', s, re.I):
        return 1

    # P1: Absolute Dateipfade
    if re.match(r'^/[\w.\-/]+\.(py|sh|php|js|ts|json|yaml|yml|conf|sql|md)\s*$', s):
        return 1

    # P1: URLs
    if re.match(r'^https?://', s):
        return 1

    # P1: Kurze abgeschlossene Sätze (Ergebnisse/Entscheidungen)
    if len(s) <= 150 and re.search(r'[.!?:»]\s*$', s) and not RE_TOOL_TRACE.match(s):
        return 1

    # P3: Tool-Call-Traces
    if RE_TOOL_TRACE.match(s):
        return 3

    # P3: ⎿ Output-Blöcke
    if RE_BREVE.match(s):
        return 3

    # P3: Diff-Fragmente
    if RE_DIFF.match(s):
        return 3

    # P3: Spinner / Lade-Artefakte
    if RE_SPINNER.match(s):
        return 3

    # P3: Token-Counter / reine Zahlen
    if RE_ONLY_NUMS.match(s):
        return 3

    # P3: Done(...)-Zeilen
    if RE_DONE_LINE.match(s):
        return 3

    return 2


# ── Kern-Algorithmus ───────────────────────────────────────────────────────

def trim(text: str, max_chars: int = MAX_CHARS) -> tuple[str, dict]:
    """Kürzt text auf max_chars. Gibt (neuer_text, stats) zurück."""
    original_len = len(text)
    stats = {
        'original':   original_len,
        'after_pass1': 0,
        'after_pass2': 0,
        'after_pass3': 0,
        'dropped_p3': 0,
        'dropped_p2': 0,
        'hard_cut':   False,
    }

    if original_len <= max_chars:
        stats.update({k: original_len for k in ('after_pass1','after_pass2','after_pass3')})
        return text, stats

    # ── Pass 1: Inline-Artefakte entfernen (immer) ─────────────────────────
    lines = text.splitlines()
    lines = [RE_CTRL_O.sub('', l) for l in lines]
    # Mehrfach-Leerzeilen auf eine reduzieren
    cleaned: list[str] = []
    prev_blank = False
    for l in lines:
        is_blank = not l.strip()
        if is_blank and prev_blank:
            continue
        cleaned.append(l)
        prev_blank = is_blank
    lines = cleaned
    stats['after_pass1'] = len('\n'.join(lines))

    # ── Pass 2: P3-Zeilen wegwerfen ────────────────────────────────────────
    classified = [(l, classify(l)) for l in lines]
    kept = [l for l, p in classified if p < 3]
    stats['dropped_p3'] = len(classified) - len(kept)
    current = '\n'.join(kept)
    stats['after_pass2'] = len(current)

    if len(current) <= max_chars:
        stats['after_pass3'] = len(current)
        return current, stats

    # ── Pass 3: P2-Zeilen von hinten kürzen ────────────────────────────────
    # Zeilen nach Priorität klassifizieren (P2 = Kandidaten zum Entfernen)
    classified2 = [(l, classify(l)) for l in kept]
    p2_indices = [i for i, (_, p) in enumerate(classified2) if p == 2]

    current_len = len(current)
    remove_set: set[int] = set()

    for idx in reversed(p2_indices):           # älteste zuletzt → neueste zuerst wegwerfen
        if current_len <= max_chars:
            break
        current_len -= len(classified2[idx][0]) + 1
        remove_set.add(idx)

    kept2 = [l for i, (l, _) in enumerate(classified2) if i not in remove_set]
    stats['dropped_p2'] = len(remove_set)
    current = '\n'.join(kept2)
    stats['after_pass3'] = len(current)

    # ── Notfall-Schnitt ────────────────────────────────────────────────────
    if len(current) > max_chars:
        current = current[:max_chars - 60].rsplit('\n', 1)[0] + \
                  '\n\n[KONTEXT GEKÜRZT – ursprünglich ' + str(original_len) + ' Zeichen]'
        stats['hard_cut'] = True
        stats['after_pass3'] = len(current)

    return current, stats


# ── DB-Zugriff ─────────────────────────────────────────────────────────────

def load_summary() -> tuple[str | None, str | None]:
    """Lädt context_json und summary aus session-compact. Gibt (raw_json, summary) zurück."""
    conn = pymysql.connect(**DB, cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT context_json
                FROM claude_context_cache
                WHERE scope = 'session-compact'
                LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return None, None
            ctx = json.loads(row['context_json'])
            return row['context_json'], ctx.get('summary', '')
    finally:
        conn.close()


def save_summary(new_summary: str, original_json: str, stats: dict) -> None:
    """Schreibt gekürzten Summary zurück in die DB."""
    ctx = json.loads(original_json)
    ctx['summary'] = new_summary
    ctx['trimmed_at'] = datetime.now().isoformat()
    ctx['trim_stats'] = stats
    new_json = json.dumps(ctx, ensure_ascii=False)

    conn = pymysql.connect(**DB, cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE claude_context_cache
                SET context_json = %s,
                    updated_at   = NOW(),
                    updated_by   = 'trim-cache'
                WHERE scope = 'session-compact'
            """, (new_json,))
        conn.commit()
    finally:
        conn.close()


# ── main ───────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description='Kürzt session-compact cache auf < MAX_CHARS')
    ap.add_argument('--dry-run', action='store_true', help='Nur analysieren, nicht schreiben')
    ap.add_argument('--max',     type=int, default=MAX_CHARS, help=f'Zeichenlimit (default {MAX_CHARS})')
    args = ap.parse_args()

    raw_json, summary = load_summary()
    if raw_json is None:
        LOG('Kein session-compact Eintrag gefunden – nichts zu tun.')
        return

    length = len(summary or '')
    LOG(f'Geladen: {length} Zeichen (Limit: {args.max})')

    if length <= args.max:
        LOG(f'Bereits im Limit – keine Aktion.')
        return

    new_summary, stats = trim(summary, args.max)

    LOG(f'Pass 1 (Artefakte):  {stats["original"]:>7} → {stats["after_pass1"]:>7} Zeichen')
    LOG(f'Pass 2 (P3-Drop):    {stats["after_pass1"]:>7} → {stats["after_pass2"]:>7} '
        f'(-{stats["dropped_p3"]} Zeilen)')
    LOG(f'Pass 3 (P2-Kürzen):  {stats["after_pass2"]:>7} → {stats["after_pass3"]:>7} '
        f'(-{stats["dropped_p2"]} Zeilen)')
    if stats['hard_cut']:
        LOG('⚠ Harter Schnitt angewendet!')
    LOG(f'Resultat: {stats["after_pass3"]} Zeichen '
        f'(Reduktion: {100 - 100*stats["after_pass3"]//stats["original"]}%)')

    if args.dry_run:
        LOG('--dry-run: nichts geschrieben.')
        # Vorschau der ersten 500 Zeichen ausgeben
        print('\n── Vorschau (erste 500 Zeichen) ──')
        print(new_summary[:500])
        return

    save_summary(new_summary, raw_json, stats)
    LOG('✓ session-compact erfolgreich gespeichert.')


if __name__ == '__main__':
    main()
