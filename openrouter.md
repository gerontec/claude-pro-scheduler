# OpenRouter — Xiaomi MiMo Sub-Agent Integration

This scheduler supports dispatching batch jobs to Xiaomi MiMo models via the
[OpenRouter](https://openrouter.ai) API as a low-cost sub-agent tier alongside the
Anthropic Claude CLI.

> **Rule:** Claude models (haiku/sonnet/opus) are **always** run via the owner's annual
> Anthropic subscription (Claude CLI). OpenRouter is used **exclusively** for Xiaomi MiMo models.

---

## Setup

Store your OpenRouter API key in `~/openrouter.key` (never commit it):

```bash
echo 'sk-or-v1-...' > ~/openrouter.key
chmod 600 ~/openrouter.key
```

The poller reads the key at runtime. No environment variable needed.

---

## Available Models

### MiMo-V2-Flash — `job.model = "xiaomi"` ← default sub-agent

Mixture-of-Experts (309B total / 15B active). Ranked **#1 open-source globally** on
SWE-bench Verified. Performance comparable to Claude Sonnet 4.5 at ~3.5% of the cost.
Best for: routine delegation, infra lookups, text summarisation.

| | |
|---|---|
| OpenRouter ID | `xiaomi/mimo-v2-flash` |
| Context window | 262,144 tokens |
| Max output | 65,536 tokens |
| Input | **$0.09 / MTok** |
| Output | **$0.29 / MTok** |
| Cache-Read | **$0.045 / MTok** (50% off) |
| Supports reasoning | yes |
| Supports tools | yes |

---

### MiMo-V2-Pro — `job.model = "mimo-pro"`

Xiaomi's flagship foundation model. 1M context window, approaching Claude Opus 4.6 quality.
Best for: complex multi-step reasoning, long-document analysis.

| | |
|---|---|
| OpenRouter ID | `xiaomi/mimo-v2-pro` |
| Context window | **1,048,576 tokens** (1M) |
| Max output | 131,072 tokens |
| Input | **$1.00 / MTok** |
| Output | **$3.00 / MTok** |
| Cache-Read | **$0.20 / MTok** (80% off) |
| Supports reasoning | yes |
| Supports tools | yes |

---

### MiMo-V2-Omni — *(can be added)*

Multimodal: text + image + video + audio → text. Visual grounding, tool use, code execution.

| | |
|---|---|
| OpenRouter ID | `xiaomi/mimo-v2-omni` |
| Input | **$0.40 / MTok** |
| Output | **$2.00 / MTok** |
| Cache-Read | **$0.08 / MTok** (80% off) |

---

## Federation API

External systems and the master-AI can submit jobs programmatically:

```
POST  /api/batch/api.php          Submit a job
GET   /api/batch/api.php?id=N     Job status + result
GET   /api/batch/api.php?list=1   Recent jobs (optional: &status=queued&limit=20)
```

**Auth:** `X-API-Key: <key>` header (or `?apikey=<key>` query param).

**POST body (JSON):**
```json
{
  "prompt": "Analysiere das Netzwerk …",
  "model": "xiaomi",
  "targetdate": "2026-04-04",
  "resume_session": false
}
```

**Response:**
```json
{"id": 37, "status": "queued", "model": "xiaomi", "targetdate": "2026-04-04"}
```

---

## Sub-Agent Context — Infrastructure Table

Every job automatically receives two context blocks prepended to the user prompt:

```
## Batch-Server localhost (ki_localhost_cache)
- system / hostname: pve
- memory / ram_total: 62 GiB
- openrouter / balance_usd: 9.9968
…

## Netzwerk-Infrastruktur (ki_infrastructure)
**192.168.178.43** (koditv.fritz.box) → Sharp 4K Android TV — Kodi 21 | Ports: 8022,8080
**192.168.178.218** (pi.fritz.box) → Raspberry Pi — Asterisk SIP, MQTT | Ports: 22,1883
…

---
[User task]

---
**Deadline:** 2026-04-04 (noch ca. 6h) – gründlich und kostensparend arbeiten.
```

Sub-agents use `ki_infrastructure` for accurate IP/hostname references — never invent IPs.
The context blocks are **byte-for-byte identical** across jobs, maximising prompt cache hits.

---

## Prompt Caching

OpenRouter caches identical prompt prefixes server-side automatically.
The shared context prefix (ki_localhost_cache + ki_infrastructure) is typically
**7,000–12,000 tokens** and produces cache hits from the second job onward.

**System prompt is kept stable** (no per-job data) so it is also cached.
Deadline and job-specific info are appended at the end of the user message only.

### Cost example (MiMo-V2-Flash, 7,700-token prefix)

| | Tokens | Rate | Cost |
|---|---|---|---|
| First job (cache miss) | 7,700 | $0.09/MTok | $0.00069 |
| Subsequent jobs (cache hit) | 7,700 | $0.045/MTok | $0.00035 |
| **Saving per repeated job** | | | **~50%** |

Benchmark result from 2026-04-04 (Job #37, xiaomi vs Job #38, haiku):

| | xiaomi (mimo-v2-flash) | haiku (Claude, own sub) |
|---|---|---|
| Cost | **$0.000538** | $0.029021 |
| Cache tokens | 927 | 29,037 |
| Ratio | **1×** | 54× more expensive |
| Correctness | ✓ same | ✓ same (more verbose) |

---

## Balance Reporting

After every job the poller fetches `/api/v1/credits` from OpenRouter and:

1. Logs to stderr: `OpenRouter Guthaben: $9.9968 (von $10.00 total, $0.003201 verbraucht)`
2. Persists to `ki_localhost_cache` (category `openrouter`):

| label | example value |
|---|---|
| `balance_usd` | `9.996799` |
| `total_credits_usd` | `10.00` |
| `total_usage_usd` | `0.003201` |
| `last_job_id` | `37` |
| `last_updated` | `2026-04-04 17:31:53` |

The balance is visible in the context of all subsequent jobs via `ki_localhost_cache`.

---

## Cost Comparison vs. Claude

| Model | Input/MTok | Cache-Read/MTok | Output/MTok | vs. Haiku 4.5 |
|---|---|---|---|---|
| MiMo-V2-Flash | $0.09 | $0.045 | $0.29 | **~0.1×** |
| MiMo-V2-Omni | $0.40 | $0.08 | $2.00 | ~0.5× |
| MiMo-V2-Pro | $1.00 | $0.20 | $3.00 | ~1.3× |
| Claude Haiku 4.5 (own sub) | $0.80 | — | $4.00 | 1× |
| Claude Sonnet 4.6 (own sub) | $3.00 | — | $15.00 | ~4× |
| Claude Opus 4.6 (own sub) | $15.00 | — | $75.00 | ~19× |

MiMo-V2-Flash is the default sub-agent for routine tasks.
Escalate to Claude (haiku/sonnet) only for tasks requiring tool use, file system access,
or higher reliability — via the Claude CLI and annual subscription, never via OpenRouter.

---

## Adding More OpenRouter Models

Edit `OPENROUTER_MODELS` in `batch-poller.py`:

```python
OPENROUTER_MODELS = {
    'xiaomi':   'xiaomi/mimo-v2-flash',  # default sub-agent
    'mimo-pro': 'xiaomi/mimo-v2-pro',
    # Claude models: NEVER here — use own subscription (Claude CLI only)
}
```

Then extend the DB ENUM, the PHP dropdown in `web/index.php`, and `$colors` in `web/job.php`.
