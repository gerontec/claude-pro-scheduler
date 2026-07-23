# Alibaba Cloud Model Studio — Setup & Batch-Integration

Stand: 2026-07-23 · Server: yt6 (dell-3660, 192.168.5.23, nur per IPv6 `yt6.heissa.de`)

## Ziel
LLM-Batch-Jobs **ohne OpenRouter** ausschließlich über den eigenen Alibaba-Account
(Model Studio / DashScope) abwickeln.

---

## Drei Zugangswege (Regionen)

| Zugang | Region | Endpoint (OpenAI-kompatibel) | Key-Datei (yt6) | Modelle |
|---|---|---|---|---|
| **Token Plan ($6 Abo)** | Singapur `ap-southeast-1` | `https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1` | `~/qwen_tokenplan.key` | 8 |
| **DashScope Frankfurt** | Frankfurt `eu-central-1` | `https://ws-a1ncrfpd8s8lkubr.eu-central-1.maas.aliyuncs.com/compatible-mode/v1` | `~/dashscope.key` | 73 |
| **DashScope intl** | Singapur `ap-southeast-1` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | (auf heissa.de: `DASHSCOPE_API_KEY`) | 149 |

### Token-Plan-Modelle (8, durch $6-Abo abgedeckt)
`qwen3.8-max-preview` · `qwen3.7-max` · `qwen3.7-plus` · `qwen3.6-flash` ·
`deepseek-v4-pro` · `glm-5.2` · `wan2.7-image` · `wan2.7-image-pro`

### Wichtige Modell-Verfügbarkeit (getestet 2026-07-23)
| Modell | Token Plan | Frankfurt | DashScope intl |
|---|---|---|---|
| `qwen3.8-max-preview` | ✅ | ❌ | ❌ |
| `qwen-flash` | ❌ | ✅ | ✅ |
| `qwen-turbo` | ❌ | ❌ | ✅ |
| `qwen3.6-flash` | ✅ | ❌ | ✅ |

> **Merke:** `qwen-turbo` gibt es nur über DashScope intl (Singapur), nicht im Token Plan
> und nicht in Frankfurt. `qwen-flash` (billiger/free) ist die Alternative in Frankfurt.

---

## Regionen-Empfehlung (Standort Deutschland)
- **Frankfurt** (`eu-central-1`): niedrigste Latenz (~10 ms), weniger Modelle → interaktive Nutzung.
- **Singapur** (`ap-southeast-1`): viele Modelle, dort liegt der $6-Token-Plan → Batch/Modellvielfalt.
- **China** (`cn-*`): **nicht empfohlen** — Great Firewall, hohes Routing/Latenz von DE aus.

---

## Batch-Server-Integration (claude-pro-scheduler)

### Worker: `/home/gh/batch-poller.py` (Cron: `* * * * *`)
- `QWEN_TP_MODELS`  = `{ 'qwen38': 'qwen3.8-max-preview' }`  → Token Plan (DEFAULT)
- `DASHSCOPE_MODELS` = `{ 'qwen-flash': 'qwen-flash' }`        → DashScope Frankfurt
- `run_openai_compatible(prompt, system, model_id, url, key)` — gemeinsamer OpenAI-kompatibler Aufruf.
- **OpenRouter entfernt** (ehem. `qwen/qwen-free/xiaomi/mimo-pro` + `/`-Passthrough raus).
- Dispatch-Reihenfolge: `LOCALP4` → `QWEN_TP_MODELS` → `DASHSCOPE_MODELS` → Claude CLI (Fallback).

### API: `/var/www/html/api/batch/api.php`
- `VALID_MODELS = ['qwen38','qwen-flash','LOCALP4']`
- Default-Modell: **`qwen38`** (qwen3.8-max-preview, solange Preview-Rabatt läuft).
- API-Key: `2a61f527ded09cc2832cb49f8829f299` (Header `X-API-Key` oder `?apikey=`).

### Job einreichen (Beispiel)
```bash
curl -s -X POST "http://192.168.5.23/api/batch/api.php" \
  -H "X-API-Key: 2a61f527ded09cc2832cb49f8829f299" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"Deine Aufgabe","model":"qwen-flash"}'   # oder "qwen38"
# Status: GET .../api.php?id=<ID>&apikey=<KEY>
```

---

## Keys auf yt6 (Rechte 600)
- `~/qwen_tokenplan.key` — Token Plan ($6 Abo, Singapur)
- `~/dashscope.key`      — DashScope Frankfurt Workspace (`ws-a1ncrfpd8s8lkubr`)

## Repo / Deployment
- Repo: `github.com/gerontec/claude-pro-scheduler` (Push via SSH, Key für `git@github.com` hinterlegt).
- **Achtung:** Live-Dateien (`/home/gh/batch-poller.py`, `/var/www/html/api/batch/api.php`)
  sind **Kopien**, keine Symlinks des Repos → Änderungen an beiden Stellen pflegen.
- Backups der Änderungen: `*.bak_dashscope_*`, `*.bak_qwentp_*`.

## Getestete Ergebnisse
- `qwen3.8-max-preview` (Token Plan): OK
- `qwen-flash` (Frankfurt): OK
- `qwen-turbo` (Frankfurt): „Model not exist" → nur DashScope intl
- Test-Job #24 (`qwen-free`/OpenRouter, vor dem Umbau): Ergebnis „TEST-BESTANDEN-42" → Pipeline funktioniert.
