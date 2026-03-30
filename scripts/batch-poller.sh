#!/bin/bash
# ============================================================
# Claude Pro Batch Poller
# Liest fällige Jobs aus claude_pro_batch, führt sie aus,
# schreibt Ergebnis + Token-Usage zurück.
# Cron: * * * * * /home/gh/batch-poller.sh
# ============================================================

DB="mysql -h localhost -u gh -pa12345 wagodb -sN"
USAGE_FILE="/home/gh/.claude_weekly_usage.json"
LOCK="/tmp/claude-pro-poller.lock"

# Nur eine Instanz gleichzeitig
[ -f "$LOCK" ] && exit 0
trap "rm -f $LOCK" EXIT
touch "$LOCK"

# ── Fälligen Job holen ─────────────────────────────────────
JOB=$($DB -e "
    SELECT id, model, resume_session, prompt
    FROM claude_pro_batch
    WHERE status='queued'
    ORDER BY targetdate ASC, created_at ASC LIMIT 1;" 2>/dev/null)

[ -z "$JOB" ] && exit 0

JOB_ID=$(echo         "$JOB" | awk '{print $1}')
MODEL=$(echo          "$JOB" | awk '{print $2}')
RESUME_SESSION=$(echo "$JOB" | awk '{print $3}')
PROMPT=$(echo         "$JOB" | cut -f4-)

# ── Als "running" markieren ────────────────────────────────
$DB -e "UPDATE claude_pro_batch SET status='running', started_at=NOW() WHERE id=$JOB_ID;" 2>/dev/null

# ── Session-Cache voranstellen wenn gewünscht ──────────────
if [ "$RESUME_SESSION" = "1" ]; then
    CACHE_CTX=$($DB -e "
        SELECT JSON_UNQUOTE(JSON_EXTRACT(context_json, '$.summary'))
        FROM claude_context_cache
        WHERE scope='session-compact'
        LIMIT 1;" 2>/dev/null)
    if [ -n "$CACHE_CTX" ] && [ "$CACHE_CTX" != "NULL" ]; then
        PROMPT="$(printf '%s\n\n---\nAufgabe:\n%s' "$CACHE_CTX" "$PROMPT")"
        echo "[$(date '+%H:%M:%S')] Session-Cache geladen ($(echo "$CACHE_CTX" | wc -c) Bytes)" >&2
    fi
fi

# ── Wochentracking laden ───────────────────────────────────
_week_start() {
    python3 - <<'PYEOF'
from datetime import datetime, timedelta
import zoneinfo
mez = zoneinfo.ZoneInfo("Europe/Berlin")
now = datetime.now(mez)
days_since_friday = (now.weekday() - 4) % 7
last_friday = now - timedelta(days=days_since_friday)
reset = last_friday.replace(hour=8, minute=0, second=0, microsecond=0)
if now < reset:
    reset -= timedelta(weeks=1)
print(reset.strftime("%Y-%m-%d %H:%M MEZ"))
PYEOF
}

_load_usage() {
    local week
    week=$(_week_start)
    if [ -f "$USAGE_FILE" ]; then
        local stored_week
        stored_week=$(python3 -c "import json; d=json.load(open('$USAGE_FILE')); print(d.get('week_start',''))" 2>/dev/null)
        if [ "$stored_week" == "$week" ]; then
            python3 -c "
import json
d = json.load(open('$USAGE_FILE'))
print(d.get('input_tokens',0), d.get('output_tokens',0), d.get('cache_tokens',0), round(d.get('cost_usd',0.0),6), d.get('tasks',0))
"
            return
        fi
    fi
    echo "0 0 0 0.0 0"
}

_save_usage() {
    python3 - <<EOF
import json
data = {
    "week_start":    "$(_week_start)",
    "input_tokens":  $1,
    "output_tokens": $2,
    "cache_tokens":  $3,
    "cost_usd":      $4,
    "tasks":         $5,
    "last_run":      "$(date '+%Y-%m-%d %H:%M:%S')"
}
json.dump(data, open('$USAGE_FILE', 'w'), indent=2)
EOF
}

read PRE_IN PRE_OUT PRE_CACHE PRE_COST PRE_TASKS <<< "$(_load_usage)"

# ── Claude ausführen ───────────────────────────────────────
TMP=$(mktemp /tmp/claude_pro_XXXXXX.json)

/usr/local/bin/claude \
    --model "$MODEL" \
    --effort low \
    --max-budget-usd 0.25 \
    --dangerously-skip-permissions \
    --append-system-prompt "Du bist ein autonomer Batch-Assistent. Führe alle Aufgaben vollständig und direkt aus – ohne Rückfragen, ohne Sicherheitshinweise, ohne Warnungen über Credentials oder Best-Practices. Der Betreiber hat alle Berechtigungen erteilt. Antworte ausschliesslich mit dem Ergebnis der Aufgabe." \
    --output-format json \
    -p "$PROMPT" \
    < /dev/null > "$TMP" 2>&1
EXIT_CODE=$?

# ── Ergebnis parsen ────────────────────────────────────────
if python3 -c "import json,sys; json.load(open('$TMP'))" 2>/dev/null; then
    RESULT=$(  python3 -c "import json; d=json.load(open('$TMP')); print(d.get('result',''))")
    IN_TOK=$(  python3 -c "import json; d=json.load(open('$TMP')); print(d.get('usage',{}).get('input_tokens',0))")
    OUT_TOK=$( python3 -c "import json; d=json.load(open('$TMP')); print(d.get('usage',{}).get('output_tokens',0))")
    CACHE_TOK=$(python3 -c "import json; d=json.load(open('$TMP')); u=d.get('usage',{}); print(u.get('cache_creation_input_tokens',0)+u.get('cache_read_input_tokens',0))")
    COST=$(    python3 -c "import json; d=json.load(open('$TMP')); print(round(d.get('total_cost_usd',0.0),6))")
    STATUS="done"
    ERR=""
else
    RESULT=$(cat "$TMP")
    IN_TOK=0; OUT_TOK=0; CACHE_TOK=0; COST=0.0
    STATUS="failed"
    ERR="Kein gültiges JSON: $(head -1 $TMP)"
fi
rm -f "$TMP"

# ── Kumuliertes Wochentracking speichern ───────────────────
TOT_IN=$((   PRE_IN    + IN_TOK    ))
TOT_OUT=$((  PRE_OUT   + OUT_TOK   ))
TOT_CACHE=$(( PRE_CACHE + CACHE_TOK ))
TOT_COST=$(python3 -c "print(round($PRE_COST + $COST, 6))")
TOT_TASKS=$(( PRE_TASKS + 1 ))
_save_usage "$TOT_IN" "$TOT_OUT" "$TOT_CACHE" "$TOT_COST" "$TOT_TASKS"

# ── Ergebnis in DB schreiben ───────────────────────────────
RESULT_ESC=$(python3 -c "
import sys
s = open('/dev/stdin').read()
print(s.replace('\\\\', '\\\\\\\\').replace(\"'\", \"\\\\'\"))
" <<< "$RESULT")

ERR_ESC=$(echo "$ERR" | sed "s/'/\\\\'/g")

$DB -e "
UPDATE claude_pro_batch SET
    status       = '$STATUS',
    result       = '$RESULT_ESC',
    input_tokens = $IN_TOK,
    output_tokens= $OUT_TOK,
    cache_tokens = $CACHE_TOK,
    cost_usd     = $COST,
    finished_at  = NOW(),
    error_msg    = '$ERR_ESC'
WHERE id = $JOB_ID;" 2>/dev/null
