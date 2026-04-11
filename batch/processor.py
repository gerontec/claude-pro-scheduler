"""JobProcessor — orchestriert einen einzelnen Job von Claim bis Notify."""
import re
import subprocess
import sys
from datetime import datetime

from .config import (OPENROUTER_MODELS,
                     CACHE_SAVER_SCRIPT, CACHE_SAVER_LOG, load_openrouter_key)
from .runners.openrouter_http import OpenRouterHttpClient
from .context import ContextBuilder
from .models import JobRecord, RunResult
from .notifier import Notifier
from .pipeline import JobPipeline
from .repository import JobRepository
from .runners import ModelRunner
from .tracker import UsageTracker

_http_client = OpenRouterHttpClient(load_openrouter_key())


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
            run = self._enforce_quality(job, run)      # Quality-Gate vor Write
            run = self._repo.complete_job(job.id, run) # merged DB-result zurück
            self._tracker.record(run)
            self._fetch_openrouter_balance(job.id)
            if 'Killed by user' not in (run.error or '') and run.status == 'done':
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
        """Wählt Runner, führt Job durch dreiphasige Pipeline aus."""
        runner = self._runners.get(job.model)
        if runner is None:
            return RunResult(
                result='', status='failed',
                error=f'Unbekanntes Modell: {job.model}',
                in_tok=0, out_tok=0, cache_tok=0, cost=0.0,
            )

        infra_context = self._context.build_infra_context()
        pipeline      = JobPipeline(runner, infra_context=infra_context, repo=self._repo)
        run           = pipeline.run(
            job=job,
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

    _QUALITY_MIN_CHARS    = 400
    _QUALITY_MIN_SECTIONS = 2   # Anzahl ##-Überschriften

    def _enforce_quality(self, job: JobRecord, run: RunResult) -> RunResult:
        """Quality-Gate: schlägt der Check fehl, wird der Job auf 'failed' gesetzt.

        Nur 'done'-Jobs werden geprüft — failed/killed bleiben unverändert.
        Der Agent kann prompt-seitig angewiesen werden ausführlich zu schreiben,
        aber erst dieser Check erzwingt es strukturell.

        Wenn run.result leer ist (Pipeline-Modus: Reporter schreibt direkt in DB),
        wird das Ergebnis aus der DB gelesen.
        """
        if run.status != 'done':
            return run

        result = run.result or ''
        if not result.strip():
            result = self._repo.read_agent_result(job.id) or ''
        errors = []

        if len(result.strip()) < self._QUALITY_MIN_CHARS:
            errors.append(
                f'Ergebnis zu kurz ({len(result.strip())} Zeichen, Minimum {self._QUALITY_MIN_CHARS})'
            )

        sections = len(re.findall(r'^#{1,3} .+', result, re.MULTILINE))
        if sections < self._QUALITY_MIN_SECTIONS:
            errors.append(
                f'Zu wenige Abschnitte ({sections} ##-Überschriften, Minimum {self._QUALITY_MIN_SECTIONS})'
            )

        if not errors:
            return run

        quality_error = 'Qualitäts-Gate: ' + '; '.join(errors)
        new_id = self._repo.requeue_with_quality_feedback(job.id, quality_error)
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] "
            f"Job #{job.id}: {quality_error} → neu eingereiht als Job #{new_id}",
            file=sys.stderr,
        )
        run.status = 'failed'
        run.error  = f'{quality_error} → Job #{new_id} neu eingereiht'
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
        if not _http_client._api_key:
            return
        try:
            bal = _http_client.get_credits()
            self._repo.save_openrouter_balance(
                job_id, bal['remaining'], bal['total'], bal['used']
            )
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"OpenRouter Guthaben: ${bal['remaining']:.4f}",
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
            ['python3', CACHE_SAVER_SCRIPT, '--compact'],
            stdout=open(CACHE_SAVER_LOG, 'a'),
            stderr=subprocess.STDOUT,
        )
