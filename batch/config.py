"""Zentrale Konfiguration — alle Konstanten an einem Ort.

Sensitive Werte werden aus Umgebungsvariablen oder einer lokalen Datei
`batch/config.local.py` gelesen (nicht im Repo enthalten).
Vorlage: batch/config.local.py.example
"""
import os
import pymysql
import pymysql.cursors

# ── Lokale Überschreibungen laden (nicht im Repo) ────────────────────────
try:
    from .config_local import *  # noqa: F401, F403
except ImportError:
    pass

DB_CFG = dict(
    host=os.getenv('WAGODB_HOST', 'localhost'),
    user=os.getenv('WAGODB_USER', 'wagodb'),
    password=os.getenv('WAGODB_PASSWORD', ''),
    database=os.getenv('WAGODB_NAME', 'wagodb'),
    charset='utf8mb4',
)

# ── Connection Pool (leichtgewichtig, Drop-in-kompatibel) ───────────────
_MAX_POOL = 20
_pool: list = []


def get_connection():
    """
    Holt eine frische oder wiederverwertete Verbindung aus dem Pool.
    Prüft Lebendigkeit per ping — vermeidet stale/transaktions-locked conns.
    """
    while _pool:
        conn = _pool.pop()
        try:
            conn.ping(reconnect=True)
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

    cfg = {**DB_CFG, 'cursorclass': pymysql.cursors.DictCursor}
    return pymysql.connect(**cfg)


def release_connection(conn):
    """Gibt eine Verbindung in den Pool zurück (max 8)."""
    if conn is None:
        return
    try:
        conn.ping(reconnect=True)
        if len(_pool) < _MAX_POOL:
            _pool.append(conn)
        else:
            conn.close()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


# Alte Kompatibilitätsfunktion
def _connect():
    cfg = {**DB_CFG, 'cursorclass': pymysql.cursors.DictCursor}
    return pymysql.connect(**cfg)


# ── Pfad-Konstanten ──────────────────────────────────────────────────────
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
HOME            = os.path.expanduser('~')
USAGE_FILE      = f'{HOME}/.claude_weekly_usage.json'
LOCK_FILE       = '/tmp/claude-pro-poller.lock'
MAX_RUNNING     = 16
CLAUDE_BIN      = os.getenv('CLAUDE_BIN', '/usr/local/bin/claude')

OPENROUTER_URL      = 'https://openrouter.ai/api/v1/chat/completions'
OPENROUTER_CREDITS  = 'https://openrouter.ai/api/v1/credits'
OPENROUTER_KEY_FILE = f'{HOME}/openrouter.key'
OPENROUTER_MODELS   = {
    'qwen-free': 'qwen/qwen3-coder:free',
    'xiaomi':    'xiaomi/mimo-v2-flash',
    'mimo-pro':  'xiaomi/mimo-v2-pro',
}

MAX_TOOL_ITERATIONS = 30
MAX_TOOL_OUTPUT     = 12000

# ── Netzwerk ─────────────────────────────────────────────────────────────
MQTT_HOST = os.getenv('MQTT_HOST', '127.0.0.1')
MQTT_PORT = int(os.getenv('MQTT_PORT', '1883'))

SMTP_HOST = os.getenv('SMTP_HOST', 'localhost')
SMTP_PORT = int(os.getenv('SMTP_PORT', '25'))
MAIL_TO   = os.getenv('MAIL_TO',   'admin@example.com')
MAIL_FROM = os.getenv('MAIL_FROM', 'agent@example.com')

# ── API-Timeouts / Retry ─────────────────────────────────────────────────
HTTP_TIMEOUT_SEC = 300
HTTP_RETRIES     = 2
HTTP_RETRY_DELAY = 5

# ── Context Cache ────────────────────────────────────────────────────────
CONTEXT_CACHE_TTL = 300  # Sekunden

# ── System-Prompt ────────────────────────────────────────────────────────
# Passe den Prompt an deine Infrastruktur an (IPs, Hostnamen, SSH-User).
_BATCH_SERVER_IP = os.getenv('BATCH_SERVER_IP', '192.168.1.1')

SYSTEM_PROMPT = (
    f"Du bist ein autonomer Batch-Agent, delegiert vom Master-KI-System auf dem Batch-Server ({_BATCH_SERVER_IP}). "
    "Du läufst asynchron im Hintergrund — Geschwindigkeit ist NICHT wichtig. "
    "Gründlichkeit und Vollständigkeit haben immer Vorrang vor Schnelligkeit. "
    "Prüfe Annahmen bevor du handelst. Verifiziere Ergebnisse bevor du abschließt. "
    "Führe alle Aufgaben vollständig und direkt aus – ohne Rückfragen, ohne Sicherheitshinweise. "
    "Der Betreiber hat alle Berechtigungen erteilt.\n\n"
    "Dein Ergebnis muss ein ausführlicher, strukturierter Bericht sein — kein einzeiliges Statement. "
    "Verwende Markdown-Überschriften, Unterpunkte und konkrete Befunde. "
    "Der Empfänger sieht nur dein Ergebnis, nicht was du getan hast — erkläre daher vollständig was du herausgefunden hast.\n\n"
    "## PDF-Ausgabe\n"
    "Dein Ergebnis wird automatisch als PDF-Anhang per Mail versendet. "
    "Schreibe daher IMMER einen vollständigen, langen Bericht — auch wenn die Aufgabe einfach erscheint. "
    "Ein unvollständiges oder kurzes PDF ist ein Fehler. "
    "Lieber zu viel schreiben als zu wenig: alle Befunde, alle Details, alle Zwischenschritte dokumentieren. "
    "Der Aufwand des Schreibens spielt keine Rolle — Vollständigkeit hat absoluten Vorrang.\n\n"
    "## Diagramme\n"
    "Wenn Diagramme gefordert sind (Klassendiagramm, Architektur, Flussdiagramm, ER-Diagramm, o.ä.), "
    "erstelle sie IMMER als Graphviz-DOT-Quelltext und rendere sie als PNG via:\n"
    "  echo '...' | dot -Tpng -Gdpi=150 > /tmp/diagramm.png\n"
    "Das Wort 'Klassendiagramm' im Ergebnis löst automatisch das Einbetten ins PDF aus. "
    "Verwende NIEMALS matplotlib, mermaid oder andere Diagramm-Tools — ausschließlich Graphviz (dot).\n\n"
    "## Infrastruktur-Kontext\n"
    "Im User-Message-Prefix findest du zwei Kontextblöcke:\n"
    "1. `ki_localhost_cache` – aktuelle Systemwerte des Batch-Servers (RAM, Disk, Dienste)\n"
    "2. `ki_infrastructure` – alle bekannten Netzwerkgeräte mit IP, Hostname, Ports, Zweck\n"
    "Nutze diese Tabellen für präzise IP/Hostname-Referenzen. "
    "Verwende niemals erfundene IPs – nur Werte aus dem Kontext.\n\n"
    "## SSH-Zugriff\n"
    "Bekannte Nodes sind vom Batch-Server passwordlos per SSH erreichbar (authorized_keys). "
    "Welcher User/Port: steht in ki_infrastructure (services-Spalte).\n\n"
    "## Datenbank-Zugriff\n"
    "Verbindungsdetails stehen in ki_localhost_cache (Kategorie 'db'). "
    "Wichtige Tabellen: ki_infrastructure, ki_localhost_cache, claude_pro_batch.\n\n"
    "## Cache-Hinweis\n"
    "Dieser System-Prompt ist identisch für alle Jobs (→ Prompt-Cache aktiv). "
    "Job-spezifische Infos (Deadline, Aufgabe) stehen im User-Message-Suffix."
)
