# OpenRouter — Xiaomi MiMo Models

This scheduler supports dispatching batch jobs to Xiaomi MiMo models via the
[OpenRouter](https://openrouter.ai) API as a low-cost alternative to the Anthropic Claude CLI.

---

## Setup

Store your OpenRouter API key in `~/openrouter.key` (never commit it):

```bash
echo 'sk-or-v1-...' > ~/openrouter.key
chmod 600 ~/openrouter.key
```

The poller reads the key at runtime from that file. No environment variable or config change needed.

---

## Available Models

### MiMo-7B-RL — `job.model = "xiaomi"` (free tier)

The original MiMo reasoning model, available for free on OpenRouter.

| | |
|---|---|
| OpenRouter ID | `xiaomi/mimo-7b-rl:free` |
| Modality | text → text |
| Context window | — |
| Input | **$0.00 / MTok** |
| Output | **$0.00 / MTok** |
| Cache-Read | **$0.00 / MTok** |

Use this for low-stakes tasks, experimentation, or when cost is the primary concern.

---

### MiMo-V2-Flash — *(not yet in dropdown, can be added)*

Mixture-of-Experts model (309B total / 15B active). Ranked **#1 open-source globally**
on SWE-bench Verified and SWE-bench Multilingual. Performance comparable to Claude Sonnet 4.5
at ~3.5% of the cost.

| | |
|---|---|
| OpenRouter ID | `xiaomi/mimo-v2-flash` |
| Modality | text → text |
| Context window | 262,144 tokens |
| Max output | 65,536 tokens |
| Input | **$0.09 / MTok** |
| Output | **$0.29 / MTok** |
| Cache-Read | **$0.045 / MTok** (50% off) |
| Supports reasoning | yes (`reasoning` parameter) |
| Supports tools | yes |
| Supports structured output | yes |

---

### MiMo-V2-Pro — `job.model = "mimo-pro"`

Xiaomi's flagship foundation model. Over 1T total parameters, 1M context window.
Deeply optimized for agentic scenarios. Benchmarks approaching Claude Opus 4.6 quality.

| | |
|---|---|
| OpenRouter ID | `xiaomi/mimo-v2-pro` |
| Modality | text → text |
| Context window | **1,048,576 tokens** (1M) |
| Max output | 131,072 tokens |
| Input | **$1.00 / MTok** |
| Output | **$3.00 / MTok** |
| Cache-Read | **$0.20 / MTok** (80% off) |
| Supports reasoning | yes (`reasoning` parameter) |
| Supports tools | yes |

---

### MiMo-V2-Omni — *(not yet in dropdown, can be added)*

Multimodal frontier model: processes text, image, video, and audio in a unified architecture.
Supports visual grounding, multi-step planning, tool use, and code execution.

| | |
|---|---|
| OpenRouter ID | `xiaomi/mimo-v2-omni` |
| Modality | text + image + audio + video → text |
| Context window | 262,144 tokens |
| Max output | 65,536 tokens |
| Input | **$0.40 / MTok** |
| Output | **$2.00 / MTok** |
| Cache-Read | **$0.08 / MTok** (80% off) |
| Supports reasoning | yes (`reasoning` parameter) |
| Supports tools | yes |

---

## Prompt Caching — How It Works

OpenRouter caches identical prompt prefixes **server-side automatically** — no special API
parameters are needed. The cache discount is applied transparently whenever a repeated prefix
is detected.

### Why this scheduler benefits significantly

Every batch job prepends the same two context blocks before the actual user prompt:

```
## Batch-Server localhost (ki_localhost_cache)
  [dozens of key/value rows — identical across all jobs]

## Netzwerk-Infrastruktur (ki_infrastructure)
  [network topology rows — identical across all jobs]

---
[System prompt with urgency hint]
```

These blocks are typically **3,000–12,000 tokens** and are **byte-for-byte identical** across
consecutive jobs. After the first job, OpenRouter caches this prefix and subsequent jobs pay
the cache-read rate for those tokens.

### Cost comparison per job (MiMo-V2-Pro, 8,000-token context prefix)

| Scenario | Prefix tokens | Rate | Cost |
|---|---|---|---|
| First job (cache miss) | 8,000 | $1.00/MTok | $0.008 |
| Subsequent jobs (cache hit) | 8,000 | $0.20/MTok | $0.0016 |
| **Saving per repeated job** | | | **$0.0064 (80%)** |

### Tracking in the Web UI

The poller reads `cache_read_input_tokens` from the OpenRouter usage response and stores it
in the `cache_tokens` DB column — the same column used for Claude's prompt cache. The Web UI
cost statistics therefore correctly reflect actual spending including the cache discount.

Response fields parsed:

```json
{
  "usage": {
    "prompt_tokens": 8420,
    "completion_tokens": 312,
    "cache_read_input_tokens": 7980,
    "cost": 0.002536
  }
}
```

---

## Adding More Models

To add a new OpenRouter model, edit the `OPENROUTER_MODELS` dict at the top of `batch-poller.py`:

```python
OPENROUTER_MODELS = {
    'xiaomi':     'xiaomi/mimo-7b-rl:free',
    'mimo-pro':   'xiaomi/mimo-v2-pro',
    'mimo-flash': 'xiaomi/mimo-v2-flash',   # ← add like this
}
```

Then add the option to `web/index.php` (dropdown + `modelBadge` map) and `web/job.php`
(`$colors` array), and copy both files to `/var/www/html/api/batch/`.

---

## Cost Comparison vs. Claude

| Model | Input/MTok | Cache-Read/MTok | Output/MTok | vs. Haiku |
|---|---|---|---|---|
| MiMo-7B-RL (free) | $0.00 | $0.00 | $0.00 | free |
| MiMo-V2-Flash | $0.09 | $0.045 | $0.29 | ~0.1× |
| MiMo-V2-Omni | $0.40 | $0.08 | $2.00 | ~0.5× |
| **MiMo-V2-Pro** | $1.00 | **$0.20** | $3.00 | ~1.3× |
| Claude Haiku 4.5 | $0.80 | — | $4.00 | 1× |
| Claude Sonnet 4.6 | $3.00 | — | $15.00 | ~4× |
| Claude Opus 4.6 | $15.00 | — | $75.00 | ~19× |

MiMo-V2-Pro with cache hits on a typical 8K-token context prefix effectively costs
**~$0.44/MTok blended input**, making it cheaper than Haiku for jobs with large repeated context.
