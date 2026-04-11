"""UsageTracker — wöchentliches Token/Kosten-Tracking in JSON-Datei."""
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .models import RunResult

_TZ = ZoneInfo('Europe/Berlin')


class UsageTracker:
    def __init__(self, usage_file: str):
        self.file = usage_file

    def record(self, run: RunResult) -> None:
        pre = self._load()
        self._save(
            in_tok    = pre[0] + run.in_tok,
            out_tok   = pre[1] + run.out_tok,
            cache_tok = pre[2] + run.cache_tok,
            cost      = round(pre[3] + run.cost, 6),
            tasks     = pre[4] + 1,
        )

    # ── intern ────────────────────────────────────────────────

    def _week_start(self) -> str:
        now              = datetime.now(_TZ)
        days_since_fri   = (now.weekday() - 4) % 7
        reset            = (now - timedelta(days=days_since_fri)).replace(
            hour=8, minute=0, second=0, microsecond=0
        )
        if now < reset:
            reset -= timedelta(weeks=1)
        return reset.strftime('%Y-%m-%d %H:%M MEZ')

    def _load(self) -> tuple:
        week = self._week_start()
        if os.path.exists(self.file):
            try:
                d = json.load(open(self.file))
                if d.get('week_start') == week:
                    return (
                        d.get('input_tokens', 0),
                        d.get('output_tokens', 0),
                        d.get('cache_tokens', 0),
                        d.get('cost_usd', 0.0),
                        d.get('tasks', 0),
                    )
            except Exception:
                pass
        return (0, 0, 0, 0.0, 0)

    def _save(self, in_tok, out_tok, cache_tok, cost, tasks) -> None:
        existing = {}
        if os.path.exists(self.file):
            try:
                existing = json.load(open(self.file))
            except Exception:
                pass
        data = {
            'week_start':    self._week_start(),
            'input_tokens':  in_tok,
            'output_tokens': out_tok,
            'cache_tokens':  cache_tok,
            'cost_usd':      cost,
            'tasks':         tasks,
            'last_run':      datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        for k in ('session_pct', 'usage_pct', 'pct_snapshot_at',
                  'session_reset', 'week_reset_raw'):
            if k in existing:
                data[k] = existing[k]
        json.dump(data, open(self.file, 'w'), indent=2)
