#!/usr/bin/env python3
"""
call_claude.py — Startet Claude Code mit Batch-Job-System Kontext.

Läuft auf jedem PC mit Claude Code Installation.
Holt die aktuellen Hinweise (CLAUDE.md) von GitHub, fällt bei fehlender
Verbindung auf den eingebetteten Fallback zurück.

Verwendung:
  python3 call_claude.py                        # interaktive Session
  python3 call_claude.py -p "Analysiere X"      # einmaliger Aufruf
  python3 call_claude.py --model sonnet -p "…"  # mit Modell-Override

Installation (optional):
  chmod +x call_claude.py
  sudo ln -s $(pwd)/call_claude.py /usr/local/bin/ki
  ki                    # dann einfach als "ki" aufrufen
"""

import os
import sys
import subprocess
import urllib.request
import argparse
import shutil
from datetime import date

# ── Konfiguration ─────────────────────────────────────────────────────────────

GITHUB_RAW = (
    "https://raw.githubusercontent.com/gerontec/claude-pro-scheduler/main/CLAUDE.md"
)
API_KEY = "2a61f527ded09cc2832cb49f8829f299"

# Eingebetteter Fallback — wird genutzt wenn GitHub nicht erreichbar
FALLBACK_CONTEXT = """\
# Master-KI Hinweise — Batch-Job-System (gerontec/claude-pro-scheduler)

## Wichtigste Regel: Usage-Limit schonen

Das Claude-Abo hat ein Stunden-Limit. Wenn du merkst, dass eine Aufgabe
**rein textbasiert** ist (Analyse, Zusammenfassung, einfache Codegenerierung,
Netzwerk-Lookup, Erklärung) — delegiere sie an das lokale Batch-Job-System
statt sie selbst zu bearbeiten. Das schont dein Kontingent und ist 54× günstiger.

## Batch-Job-System (Sub-Agent-Delegation)

**API-Endpunkt:** `http://192.168.5.23/api/batch/api.php`
**Auth-Header:** `X-API-Key: {key}`
**Web-UI:** `http://192.168.5.23/api/batch/`

### Job einreichen

```bash
curl -s -X POST http://192.168.5.23/api/batch/api.php \\
  -H "X-API-Key: {key}" \\
  -H "Content-Type: application/json" \\
  -d '{{"model":"xiaomi","prompt":"Deine Aufgabe …","targetdate":"{today}"}}'
```

### Status prüfen

```bash
curl -s "http://192.168.5.23/api/batch/api.php?id=<ID>&full=1&apikey={key}"
```

### Bis zu 9 Jobs parallel

```bash
for prompt in "Aufgabe 1" "Aufgabe 2" "Aufgabe 3"; do
  curl -s -X POST http://192.168.5.23/api/batch/api.php \\
    -H "X-API-Key: {key}" -H "Content-Type: application/json" \\
    -d "{{\\"model\\":\\"xiaomi\\",\\"prompt\\":\\"$prompt\\"}}" &
done; wait
```

## Verfügbare Modelle

| model     | Einsatz                                        | Kosten      |
|-----------|------------------------------------------------|-------------|
| xiaomi    | Standard-Sub-Agent: Analyse, Lookup, Texte     | ~$0.0005/Job|
| mimo-pro  | Komplexes Reasoning, lange Dokumente           | ~$0.01/Job  |
| haiku     | Claude-Qualität, kein Tool-Zugriff nötig       | Abo-Limit   |
| sonnet    | Hohe Qualität + Tool-Zugriff via Claude CLI    | Abo-Limit   |

**Faustregel:** Textaufgabe → xiaomi | Komplex → mimo-pro | Tool/Datei → selbst erledigen

## Sub-Agent-Kontext (automatisch injiziert)

Jeder Job erhält als Prefix: ki_localhost_cache (System) + ki_infrastructure (Netzwerk).
Sub-Agents kennen alle Geräte-IPs — müssen nicht im Prompt angegeben werden.

## Wann NICHT delegieren

- Bash/Datei-Zugriff nötig → selbst erledigen
- Aufgaben < 5s Denkzeit — Overhead lohnt nicht
- Interaktiver Dialog — Sub-Agent antwortet nur einmalig
"""

# ── Kontext holen ─────────────────────────────────────────────────────────────

BATCH_SERVER = "192.168.5.23"

def fetch_context() -> tuple[str, str]:
    """
    Gibt (context_text, source_label) zurück.
    Reihenfolge: GitHub → lokaler Server (192.168.5.23) → Fallback.
    """
    # 1. GitHub (immer aktuell)
    try:
        with urllib.request.urlopen(GITHUB_RAW, timeout=4) as r:
            return r.read().decode(), "GitHub (aktuell)"
    except Exception:
        pass

    # 2. Lokaler Batch-Server
    try:
        url = f"http://{BATCH_SERVER}/api/batch/CLAUDE.md"
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.read().decode(), f"Server {BATCH_SERVER}"
    except Exception:
        pass

    # 3. Eingebetteter Fallback
    today = date.today().isoformat()
    text = FALLBACK_CONTEXT.format(key=API_KEY, today=today)
    return text, "eingebetteter Fallback"


# ── Haupt-Logik ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--no-context", action="store_true",
                        help="Kein Batch-Kontext injizieren — normaler claude-Aufruf")
    parser.add_argument("--show-context", action="store_true",
                        help="Nur Kontext anzeigen, Claude nicht starten")
    our_args, claude_args = parser.parse_known_args()

    # Claude-Binary finden
    claude_bin = shutil.which("claude")
    if not claude_bin:
        print("FEHLER: 'claude' nicht im PATH gefunden.", file=sys.stderr)
        print("Installation: https://claude.ai/code", file=sys.stderr)
        sys.exit(1)

    if our_args.no_context:
        os.execvp(claude_bin, [claude_bin] + claude_args)
        return  # wird nicht erreicht

    # Kontext laden
    context, source = fetch_context()

    if our_args.show_context:
        print(f"── Kontext-Quelle: {source} ──\n")
        print(context)
        return

    print(f"[ki] Kontext geladen: {source}", file=sys.stderr)
    print(f"[ki] Batch-API: http://{BATCH_SERVER}/api/batch/api.php", file=sys.stderr)
    print(f"[ki] Starte Claude …\n", file=sys.stderr)

    # Claude mit injiziertem Kontext starten
    # --append-system-prompt funktioniert für interaktive und -p Sessions
    cmd = [claude_bin, "--append-system-prompt", context] + claude_args

    # ANTHROPIC_BASE_URL nicht auf Ollama zeigen lassen
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")}

    os.execvpe(claude_bin, cmd, env)


if __name__ == "__main__":
    main()
