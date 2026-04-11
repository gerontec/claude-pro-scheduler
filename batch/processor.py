"""JobProcessor — orchestriert einen einzelnen Job von Claim bis Notify."""
import subprocess
import sys
import urllib.request
import json
from datetime import datetime

from .config import OPENROUTER_MODELS, OPENROUTER_CREDITS, OPENROUTER_KEY_FILE
from .context import ContextBuilder
from .models import JobRecord, RunResult
from .notifier import Notifier
from .repository import JobRepository
from .runners import ModelRunner
from .tracker import UsageTracker

import os
_OR_KEY = open(OPENROUTER_KEY_FILE).read().strip() \
    if os.path.exists(OPENROUTER_KEY_FILE) else ''


class JobProcessor:
    def __init__(
        self,
        repo:    JobRepository,
        runners: dict[str, ModelRunner],
        context: ContextBuilder,
        notifier: Notifier,
        tracker:  UsageTracker,
    ):
        self._repo    = repo
        self._runners = runners
        self._context = context
        self._notifier = notifier
        self._tracker  = tracker

    def process(self, job: JobRecord) -> None:
        """Vollständiger Job-Lifecycle nach dem Claim."""
        try:
            run = self._execute(job)
            run = self._maybe_escalate(job, run)
            self._repo.write_result(job.id, run)
            self._tracker.record(run)
            self._fetch_openrouter_balance(job.id)
            if 'Killed by user' not in (run.error or ''):
                self._notifier.notify(job, run)
            self._update_session_cache()
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"Job #{job.id} → {run.status} "
                f"({run.in_tok}/{run.out_tok} tok, ${run.cost})",
                file=sys.stderr,
            )
        except Exception as exc:
            run = RunResult(
                result=str(exc), status='failed',
                error=f'Processor-Fehler: {exc}',
                in_tok=0, out_tok=0, cache_tok=0, cost=0.0,
            )
            self._repo.write_result(job.id, run)
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"Job #{job.id} FEHLER: {exc}",
                file=sys.stderr,
            )

    # ── intern ────────────────────────────────────────────────

    def _execute(self, job: JobRecord) -> RunResult:
        """Wählt Runner, baut Prompt, führt aus."""
        runner = self._runners.get(job.model)
        if runner is None:
            return RunResult(
                result='', status='failed',
                error=f'Unbekanntes Modell: {job.model}',
                in_tok=0, out_tok=0, cache_tok=0, cost=0.0,
            )

        prompt        = self._context.build_prompt(job)
        system_prompt = self._context.system_prompt()

        run = runner.run(
            prompt=prompt,
            system_prompt=system_prompt,
            job_id=job.id,
            on_kill_check=lambda: self._repo.is_killed(job.id),
        )

        # Kill-Check nach Ausführung: nicht sys.exit() — write_result() soll
        # noch aufgerufen werden damit Kosten/Tokens erhalten bleiben.
        if self._repo.is_killed(job.id):
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"Job #{job.id} während Laufzeit abgebrochen (Kosten werden gespeichert).",
                file=sys.stderr,
            )
            run.status = 'failed'
            run.error  = run.error or 'Killed by user'

        return run

    def _maybe_escalate(self, job: JobRecord, run: RunResult) -> RunResult:
        """Eskaliert zu Sonnet wenn Modell Bedenken geäußert hat."""
        if not ContextBuilder.needs_escalation(job.model, run.result, OPENROUTER_MODELS):
            return run
        new_id = self._repo.escalate_to_sonnet(job.id)
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] "
            f"Job #{job.id}: Bedenken → eskaliert zu Sonnet als Job #{new_id}",
            file=sys.stderr,
        )
        run.status = 'failed'
        run.error  = f'Eskaliert zu Sonnet → Job #{new_id}'
        return run

    def _fetch_openrouter_balance(self, job_id: int) -> None:
        if not _OR_KEY:
            return
        try:
            req = urllib.request.Request(
                OPENROUTER_CREDITS,
                headers={'Authorization': f'Bearer {_OR_KEY}'},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                cr = json.loads(resp.read())['data']
            total     = float(cr.get('total_credits', 0))
            used      = float(cr.get('total_usage', 0))
            remaining = round(total - used, 6)
            self._repo.save_openrouter_balance(job_id, remaining, total, used)
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"OpenRouter Guthaben: ${remaining:.4f}",
                file=sys.stderr,
            )
        except Exception as e:
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"OpenRouter Guthaben Fehler: {e}",
                file=sys.stderr,
            )

    @staticmethod
    def _update_session_cache() -> None:
        subprocess.run(
            ['python3', '/home/gh/cache-saver.py', '--compact'],
            stdout=open('/tmp/cache-saver.log', 'a'),
            stderr=subprocess.STDOUT,
        )
