#!/bin/bash
# ============================================================
# Batch-Ausführer für Claude Code (Claude Pro Abo)
# Liest:    /home/gh/claudebatch.txt
# Schreibt: /home/gh/claudeTaskResult.txt
# Tracking: /home/gh/.claude_weekly_usage.json
#
# Format claudebatch.txt:
#   Zeile 1: targetdate=tomorrow  (oder today, 2026-04-01 usw.)
#   Zeile 2+: Auftragstext (Prosa)
# ============================================================

BATCH_FILE="/home/gh/claudebatch.txt"
RESULT_FILE="/home/gh/claudeTaskResult.txt"
USAGE_FILE="/home/gh/.claude_weekly_usage.json"
SELF="$(realpath "$0")"

# ── Wochentracking (Fr 08:00 MEZ – Abo-Reset) ─────────────
_week_start() {
    # Letzten Freitag 08:00 MEZ berechnen (Abo-Reset)
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
            # Gleiche Woche – Werte laden
            python3 - <<EOF
import json
d = json.load(open('$USAGE_FILE'))
print(f"in={d.get('input_tokens',0)} out={d.get('output_tokens',0)} cache={d.get('cache_tokens',0)} cost={d.get('cost_usd',0.0):.4f} tasks={d.get('tasks',0)}")
EOF
            return
        fi
    fi
    # Neue Woche oder erste Ausführung
    echo "in=0 out=0 cache=0 cost=0.0000 tasks=0"
}

_save_usage() {
    local week in_tok out_tok cache_tok cost tasks
    week=$(_week_start)
    in_tok=$1; out_tok=$2; cache_tok=$3; cost=$4; tasks=$5
    python3 - <<EOF
import json
data = {
    "week_start":    "$week",
    "input_tokens":  $in_tok,
    "output_tokens": $out_tok,
    "cache_tokens":  $cache_tok,
    "cost_usd":      $cost,
    "tasks":         $tasks,
    "last_run":      "$(date '+%Y-%m-%d %H:%M:%S')"
}
json.dump(data, open('$USAGE_FILE', 'w'), indent=2)
EOF
}

_format_usage_block() {
    local label=$1 in_tok=$2 out_tok=$3 cache_tok=$4 cost=$5 tasks=$6
    printf "  %-8s │ Input: %6d  Output: %6d  Cache: %6d  │ Kosten: \$%.4f  (Tasks: %d)\n" \
        "$label" "$in_tok" "$out_tok" "$cache_tok" "$cost" "$tasks"
}

# ── Datei einlesen ─────────────────────────────────────────
if [ ! -f "$BATCH_FILE" ]; then
    echo "Fehler: $BATCH_FILE nicht gefunden." >&2
    exit 1
fi

FIRST_LINE=$(head -1 "$BATCH_FILE")
TARGET_RAW=$(echo "$FIRST_LINE" | grep -oP '(?i)targetdate=\K[^\s]+' | tr -d ' ')
MODEL_RAW=$(echo  "$FIRST_LINE" | grep -oP '(?i)model=\K[^\s]+'     | tr '[:upper:]' '[:lower:]')
TASK=$(tail -n +2 "$BATCH_FILE" | sed '/^[[:space:]]*$/d')

# Modell auflösen – Default: haiku
case "$MODEL_RAW" in
    sonnet)  MODEL="sonnet" ;;
    opus)    MODEL="opus"   ;;
    *)       MODEL="haiku"  ;;
esac

if [ -z "$TASK" ]; then
    echo "Fehler: Kein Auftragstext in $BATCH_FILE (ab Zeile 2)." >&2
    exit 1
fi

# ── Zieldatum auflösen ──────────────────────────────────────
case "${TARGET_RAW,,}" in
    tomorrow)   RUN_DATE=$(date -d "+1 day"      +%Y-%m-%d) ;;
    today|now)  RUN_DATE=$(date                  +%Y-%m-%d) ;;
    "")         RUN_DATE=$(date                  +%Y-%m-%d) ;;
    *)          RUN_DATE=$(date -d "$TARGET_RAW" +%Y-%m-%d 2>/dev/null) ;;
esac

if [ -z "$RUN_DATE" ]; then
    echo "Unbekanntes Datum '$TARGET_RAW' – starte sofort." >&2
    RUN_DATE=$(date +%Y-%m-%d)
fi

TODAY=$(date +%Y-%m-%d)

# ── Task ausführen ─────────────────────────────────────────
_run_task() {
    local tmp_json
    tmp_json=$(mktemp /tmp/claude_batch_XXXXXX.json)

    # ── Usage VOR dem Task ──────────────────────────────────
    eval "$(  _load_usage | sed 's/in=/PRE_IN=/;s/ out=/;PRE_OUT=/;s/ cache=/;PRE_CACHE=/;s/ cost=/;PRE_COST=/;s/ tasks=/;PRE_TASKS=/' )"

    {
        echo "╔══════════════════════════════════════════════════════════════╗"
        echo "║              CLAUDE BATCH TASK RESULT                       ║"
        echo "╚══════════════════════════════════════════════════════════════╝"
        echo ""
        echo "  Start:    $(date '+%Y-%m-%d %H:%M:%S')"
        echo "  Modell:   $MODEL  (Claude Pro Abo)"
        echo "  Effort:   low"
        echo "  Reset:    Freitag 08:00 MEZ │ Aktuelle Periode ab: $(_week_start)"
        echo ""
        echo "  ── WOCHENLIMIT VOR TASK ───────────────────────────────────"
        _format_usage_block "Bisher" "$PRE_IN" "$PRE_OUT" "$PRE_CACHE" "$PRE_COST" "$PRE_TASKS"
        echo ""
        echo "  ── AUFTRAG ────────────────────────────────────────────────"
        echo "$TASK" | sed 's/^/  /'
        echo ""
        echo "  ── ERGEBNIS ───────────────────────────────────────────────"
        echo ""
    } > "$RESULT_FILE"

    # ── Claude ausführen (JSON-Output für Token-Tracking) ───
    claude \
        --model "$MODEL" \
        --effort low \
        --dangerously-skip-permissions \
        --output-format json \
        -p "$TASK" \
        < /dev/null > "$tmp_json" 2>&1
    EXIT_CODE=$?

    # ── Ergebnis + Token-Daten aus JSON extrahieren ─────────
    local result in_tok out_tok cache_tok cost_usd
    if [ -f "$tmp_json" ] && python3 -c "import json,sys; json.load(open('$tmp_json'))" 2>/dev/null; then
        result=$(python3   -c "import json; d=json.load(open('$tmp_json')); print(d.get('result','(kein Ergebnis)'))")
        in_tok=$(python3   -c "import json; d=json.load(open('$tmp_json')); print(d.get('usage',{}).get('input_tokens',0))")
        out_tok=$(python3  -c "import json; d=json.load(open('$tmp_json')); print(d.get('usage',{}).get('output_tokens',0))")
        cache_tok=$(python3 -c "import json; d=json.load(open('$tmp_json')); u=d.get('usage',{}); print(u.get('cache_creation_input_tokens',0)+u.get('cache_read_input_tokens',0))")
        cost_usd=$(python3 -c "import json; d=json.load(open('$tmp_json')); print(f\"{d.get('total_cost_usd',0.0):.4f}\")")
    else
        result=$(cat "$tmp_json" 2>/dev/null || echo "(Fehler – keine Ausgabe)")
        in_tok=0; out_tok=0; cache_tok=0; cost_usd="0.0000"
    fi
    rm -f "$tmp_json"

    # ── Kumulierte Wochenwerte berechnen ────────────────────
    local tot_in tot_out tot_cache tot_cost tot_tasks
    tot_in=$(( PRE_IN    + in_tok    ))
    tot_out=$(( PRE_OUT  + out_tok   ))
    tot_cache=$(( PRE_CACHE + cache_tok ))
    tot_cost=$(python3 -c "print(f'{$PRE_COST + $cost_usd:.4f}')")
    tot_tasks=$(( PRE_TASKS + 1 ))

    _save_usage "$tot_in" "$tot_out" "$tot_cache" "$tot_cost" "$tot_tasks"

    # ── In Ergebnis-Datei schreiben ─────────────────────────
    {
        echo "$result"
        echo ""
        echo "  ── USAGE DIESER TASK ──────────────────────────────────────"
        _format_usage_block "Task" "$in_tok" "$out_tok" "$cache_tok" "$cost_usd" "1"
        echo ""
        echo "  ── WOCHENLIMIT NACH TASK ──────────────────────────────────"
        _format_usage_block "Gesamt" "$tot_in" "$tot_out" "$tot_cache" "$tot_cost" "$tot_tasks"
        echo ""
        echo "  ── ENDE ───────────────────────────────────────────────────"
        echo "  Fertig:   $(date '+%Y-%m-%d %H:%M:%S')"
        echo "  Status:   $( [ $EXIT_CODE -eq 0 ] && echo 'OK' || echo "Fehler (Code $EXIT_CODE)" )"
        echo "╚══════════════════════════════════════════════════════════════╝"
    } >> "$RESULT_FILE"

    echo "Ergebnis → $RESULT_FILE"
}

# ── Planen oder sofort ausführen ───────────────────────────
if [[ "$RUN_DATE" > "$TODAY" ]]; then
    RUN_MONTH=$(date -d "$RUN_DATE" +%-m)
    RUN_DAY=$(date   -d "$RUN_DATE" +%-d)

    (crontab -l 2>/dev/null | grep -v "$SELF"; \
     echo "30 8 $RUN_DAY $RUN_MONTH * $SELF --run && crontab -l | grep -v '$SELF' | crontab -") \
    | crontab -

    echo "Task eingeplant für $RUN_DATE um 08:30 Uhr."
    echo "Prüfen mit: crontab -l"

elif [[ "$1" == "--run" ]] || [[ "$RUN_DATE" == "$TODAY" ]]; then
    _run_task
else
    echo "Datum '$RUN_DATE' liegt in der Vergangenheit – starte sofort."
    _run_task
fi
