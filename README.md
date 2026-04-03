# Claude Pro Scheduler

Automatisierter Batch-Job-Scheduler für Claude Pro Abo (Claude Code CLI).

## Features

- **Web UI** (Bootstrap 5, Mobile-optimiert) zum Einreichen von Claude-Aufgaben
- **Automatische Ausführung** via Cron (jede Minute)
- **Wochenlimit-Tracking** mit Fortschrittsbalken (Reset: Freitag 08:00 MEZ)
- **Modellauswahl**: Haiku (Standard, ~1×) · Sonnet (~4×) · Opus (~19×) · Xiaomi MiMo (OpenRouter, free) · MiMo-V2-Pro (OpenRouter)
- **Kostentracking** pro Job + Wochensumme in MariaDB
- **fetch-usage.py**: Automatisches Auslesen des Claude Pro Limits via pexpect
- **Reschedule / Löschen** direkt im UI

## Architektur

```
Web UI (PHP) → MariaDB claude_pro_batch → batch-poller.sh → claude CLI → Ergebnis in DB
fetch-usage.py (Cron */30min) → ~/.claude_weekly_usage.json → Web UI
```

## Dateien

| Datei | Beschreibung |
|---|---|
| `scripts/batch-poller.sh` | Cron-Poller: liest Jobs aus DB, führt claude aus, schreibt Ergebnis zurück |
| `scripts/batch-claude.sh` | Manueller Batch-Runner via `claudebatch.txt` |
| `scripts/fetch-usage.py` | Liest Claude Pro Wochenlimit via pexpect, speichert JSON |
| `web/index.php` | Bootstrap Web UI: Job-Liste, Formular, Statistik, Kostenvergleich |
| `web/job.php` | Job-Detailseite mit Reschedule/Löschen |

## Setup

### Voraussetzungen
- Claude Code CLI (`/usr/local/bin/claude`) mit Pro-Abo (OAuth)
- MariaDB/MySQL
- Apache2 + PHP 8.x
- Python 3 + pexpect (`pip3 install pexpect`)
- Cron

### Datenbank
```sql
CREATE TABLE claude_pro_batch (
  id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  created_at   DATETIME        DEFAULT CURRENT_TIMESTAMP,
  targetdate   DATE            NOT NULL,
  model        ENUM('haiku','sonnet','opus') DEFAULT 'haiku',
  prompt       TEXT            NOT NULL,
  status       ENUM('queued','running','done','failed') DEFAULT 'queued',
  result       LONGTEXT,
  input_tokens INT UNSIGNED,
  output_tokens INT UNSIGNED,
  cache_tokens INT UNSIGNED,
  cost_usd     DECIMAL(10,6),
  started_at   DATETIME,
  finished_at  DATETIME,
  error_msg    TEXT
);
```

### Cron
```
* * * * * /home/gh/batch-poller.sh
*/30 * * * * python3 /home/gh/fetch-usage.py >> /tmp/fetch-usage.log 2>&1
```

### Web
Dateien aus `web/` nach `/var/www/html/api/batch/` kopieren.

## Costs

### Anthropic (via Claude Code CLI)

| Model | Input/MTok | Output/MTok | Factor |
|---|---|---|---|
| Haiku 4.5 | $0.80 | $4.00 | 1× |
| Sonnet 4.6 | $3.00 | $15.00 | ~4× |
| Opus 4.6 | $15.00 | $75.00 | ~19× |

### OpenRouter — Xiaomi MiMo models

OpenRouter models are dispatched directly via REST API (no Claude CLI required).
Store your key in `~/openrouter.key` (chmod 600) — it is never committed to the repo.

| Model | `job.model` value | Input/MTok | Cache-Read/MTok | Output/MTok |
|---|---|---|---|---|
| MiMo-7B-RL | `xiaomi` | free | free | free |
| MiMo-V2-Pro | `mimo-pro` | $1.00 | **$0.20** | $3.00 |

#### Automatic prompt caching (MiMo-V2-Pro)

OpenRouter caches identical prompt prefixes server-side — no extra API parameters needed.
Jobs that share the same system prompt and context blocks (`ki_localhost_cache`, `ki_infrastructure`)
will have those tokens billed at the **cache-read rate: 80% cheaper** than normal input.

The poller reads `cache_read_input_tokens` from the response and stores them in the DB
(`cache_tokens` column), so cache savings are visible in the Web UI cost statistics.
