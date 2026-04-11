"""Dreiphasige Job-Pipeline: Planner → Executor → Reporter.

Jede Phase startet mit frischem Konversations-Kontext und kommuniziert
ausschließlich über eine physische Plan-Datei. Das verhindert den
Orientierungsverlust durch wachsende Konversationsgeschichte.

Ablauf:
  Planner  (max 2 Iter.)  — schreibt job-{id}.plan
  Executor (max n×2 Iter.)— liest Plan, führt Schritt für Schritt aus
  Reporter (max 2 Iter.)  — liest fertigen Plan, schreibt DB-Ergebnis
"""
from __future__ import annotations

import os
import re
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .models import JobRecord, RunResult
from .runners.base import ModelRunner

PLAN_DIR = '/var/www/html/api/batch/doc'


# ── Phasen-Kontext (fließt durch die Chain) ─────────────────────────────────

@dataclass
class PhaseContext:
    job:        JobRecord
    plan_path:  str
    infra:      str = ''      # Infrastruktur-Kontext (ki_localhost_cache + ki_infrastructure)
    plan:       str = ''      # Inhalt der Plan-Datei (nach Planner befüllt)
    step_count: int = 3       # Anzahl Schritte (nach Planner gesetzt, steuert Executor max_iter)
    total_in:   int = 0
    total_out:  int = 0
    total_cache:int = 0
    total_cost: float = 0.0
    iters:      int = 0


# ── Abstrakte Basis ──────────────────────────────────────────────────────────

class JobPhase(ABC):
    name: str = 'phase'

    @abstractmethod
    def max_iter(self, ctx: PhaseContext) -> int: ...

    @abstractmethod
    def system_prompt(self, ctx: PhaseContext) -> str: ...

    @abstractmethod
    def user_prompt(self, ctx: PhaseContext) -> str: ...

    def tools(self) -> list | None:
        """None = Runner-Default (alle Tools). [] = keine Tools."""
        return None

    def on_complete(self, ctx: PhaseContext, run: RunResult) -> None:
        """Nachbearbeitung nach erfolgreichem Phasen-Lauf (optional)."""


# ── Phase 1: Planner ─────────────────────────────────────────────────────────

class PlannerPhase(JobPhase):
    """Analysiert die Aufgabe und schreibt einen konkreten Schritt-Plan."""
    name = 'planner'

    def max_iter(self, ctx): return 6   # Basis 4 (mkdir+write+verify+stop) + 2 Aufschlag

    def system_prompt(self, ctx):
        return (
            "Du bist ein präziser Aufgaben-Planer. "
            "Deine einzige Aufgabe: Analysiere die Aufgabe und schreibe einen "
            "Ausführungsplan als Markdown-Checkliste in eine Datei. "
            "NICHT ausführen. NICHT recherchieren. Nur den Plan schreiben, "
            "dann verifizieren dass die Datei existiert."
        )

    def user_prompt(self, ctx):
        return (
            f"Aufgabe:\n{ctx.job.prompt}\n\n"
            f"Schreibe einen Ausführungsplan nach: {ctx.plan_path}\n\n"
            f"Exaktes Format:\n"
            f"```\n"
            f"# Job #{ctx.job.id} Plan\n\n"
            f"## Aufgabe\n"
            f"[Ein Satz: was soll erreicht werden]\n\n"
            f"## Schritte\n"
            f"- [ ] Schritt 1: [konkreter Shell-Befehl oder Aktion]\n"
            f"- [ ] Schritt 2: [konkreter Shell-Befehl oder Aktion]\n"
            f"- [ ] Schritt 3: Abschlussbericht in DB schreiben\n"
            f"```\n\n"
            f"Maximal 5 Schritte. Letzter Schritt ist immer der DB-Write.\n"
            f"Dann prüfen: cat {ctx.plan_path}"
        )

    def on_complete(self, ctx: PhaseContext, run: RunResult) -> None:
        try:
            ctx.plan = open(ctx.plan_path).read()
        except OSError:
            raise RuntimeError(f"Plan-Datei fehlt nach Planner-Phase: {ctx.plan_path}")
        ctx.step_count = max(1, len(re.findall(r'- \[ \]', ctx.plan)))
        print(
            f"[pipeline] Planner: {ctx.step_count} Schritte in {ctx.plan_path}",
            file=sys.stderr,
        )


# ── Phase 2: Executor ────────────────────────────────────────────────────────

class ExecutorPhase(JobPhase):
    """Führt die Plan-Schritte der Reihe nach aus. Liest Plan zu Beginn jeder Iteration."""
    name = 'executor'

    def max_iter(self, ctx):
        # Pro Schritt 3 Iterationen (lesen + ausführen + aktualisieren), mind. 6, + 2 Aufschlag
        return max(6, ctx.step_count * 3) + 2

    def system_prompt(self, ctx):
        return (
            "Du bist ein präziser Ausführer. Strikte Arbeitsweise:\n"
            f"1. Lies IMMER zuerst den Plan: cat {ctx.plan_path}\n"
            "2. Identifiziere den nächsten offenen Schritt (- [ ])\n"
            "3. Führe genau diesen einen Schritt aus\n"
            "4. Aktualisiere den Plan: '- [ ] Schritt N' → '- [x] Schritt N → [Ergebnis]'\n"
            "   Befehl: sed -i 's/- \\[ \\] Schritt N:/- [x] Schritt N: ERGEBNIS ←/' {plan}\n"
            "5. Lies den Plan erneut zur Verifikation\n"
            "6. Wenn alle Schritte [x]: gib 'ALLE SCHRITTE ABGESCHLOSSEN' aus und stoppe\n\n"
            "Beginne JEDE Iteration mit cat {plan} — das ist dein Orientierungsanker."
        ).format(plan=ctx.plan_path)

    def user_prompt(self, ctx):
        infra_prefix = f"{ctx.infra}\n\n---\n\n" if ctx.infra else ''
        return (
            f"{infra_prefix}"
            f"Führe alle Schritte des Plans aus.\n"
            f"Plan-Datei: {ctx.plan_path}\n\n"
            f"Original-Aufgabe:\n{ctx.job.prompt}\n\n"
            f"Starte mit: cat {ctx.plan_path}"
        )

    def on_complete(self, ctx: PhaseContext, run: RunResult) -> None:
        try:
            ctx.plan = open(ctx.plan_path).read()
        except OSError:
            pass  # Plan-Datei optional für Reporter


# ── Phase 3: Reporter ────────────────────────────────────────────────────────

class ReporterPhase(JobPhase):
    """Liest den fertigen Plan, gibt Abschlussbericht als Text zurück.

    Keine Tools — verhindert dass der Reporter weiter Befehle ausführt.
    Die Pipeline schreibt den Bericht selbst in die DB (on_complete).
    """
    name = 'reporter'

    def max_iter(self, ctx): return 3   # Nur Text-Ausgabe, kein Tool-Call nötig

    def tools(self) -> list:
        return []   # Keine Tools — nur Text-Antwort erlaubt

    def system_prompt(self, ctx):
        return (
            "Du bist ein technischer Redakteur. "
            "Du hast KEINE Tools zur Verfügung — schreibe nur Text. "
            "Basierend auf dem Ausführungsplan erstellst du einen vollständigen Abschlussbericht. "
            "Mindestanforderungen: mindestens 3 Abschnitte (## Überschriften), "
            "konkrete Befunde mit Zahlen/Werten, keine Pauschalaussagen. "
            "Deine Antwort wird direkt als Ergebnis gespeichert."
        )

    def user_prompt(self, ctx):
        plan_content = ctx.plan or '(kein Plan verfügbar)'
        return (
            f"Abgeschlossener Ausführungsplan:\n```\n{plan_content}\n```\n\n"
            f"Schreibe jetzt den vollständigen Abschlussbericht als Markdown. "
            f"Dokumentiere alle Befunde, Ergebnisse und nächsten Schritte. "
            f"Deine Antwort wird automatisch als Ergebnis gespeichert — "
            f"kein DB-Write, kein Code, nur Markdown-Text."
        )

    def on_complete(self, ctx: PhaseContext, run: RunResult) -> None:
        """Schreibt den Bericht-Text direkt in die DB."""
        report = (run.result or '').strip()
        if not report:
            return
        try:
            from .config import get_connection, release_connection
            conn = get_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE claude_pro_batch SET result=%s WHERE id=%s",
                        (report, ctx.job.id),
                    )
                conn.commit()
            finally:
                release_connection(conn)
            print(
                f"[pipeline] Reporter: {len(report)} Zeichen in DB geschrieben (Job #{ctx.job.id})",
                file=sys.stderr,
            )
        except Exception as exc:
            print(f"[pipeline] Reporter DB-Write fehlgeschlagen: {exc}", file=sys.stderr)


# ── Pipeline-Orchestrator ────────────────────────────────────────────────────

class JobPipeline:
    """Erzwingt Planner → Executor → Reporter für jeden Job.

    Jede Phase erhält einen frischen Konversations-Kontext.
    State-Transfer erfolgt ausschließlich über die physische Plan-Datei.
    """

    # Fortschritts-Bits pro Phase (beim Start gesetzt)
    _PHASE_PROGRESS: dict[str, int] = {
        'planner':  1,   # Analyse
        'executor': 4,   # Hauptarbeit
        'reporter': 32,  # Bericht
    }

    def __init__(self, runner: ModelRunner, infra_context: str = '', repo=None):
        self._runner = runner
        self._infra  = infra_context
        self._repo   = repo   # optional — für Progress-Updates
        self._phases: list[JobPhase] = [
            PlannerPhase(),
            ExecutorPhase(),
            ReporterPhase(),
        ]

    def _set_progress(self, job_id: int, bit: int) -> None:
        if self._repo is None or not bit:
            return
        try:
            from .config import get_connection, release_connection
            conn = get_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE claude_pro_batch SET progress=progress|%s WHERE id=%s",
                        (bit, job_id),
                    )
                conn.commit()
            finally:
                release_connection(conn)
        except Exception as exc:
            print(f"[pipeline] progress update failed: {exc}", file=sys.stderr)

    def run(self, job: JobRecord, on_kill_check: Callable[[], bool]) -> RunResult:
        plan_path = os.path.join(PLAN_DIR, f'job-{job.id}.plan')
        ctx = PhaseContext(job=job, plan_path=plan_path, infra=self._infra)

        for phase in self._phases:
            if on_kill_check():
                return self._aborted(ctx)

            # Fortschritt beim Phase-Start setzen
            self._set_progress(job.id, self._PHASE_PROGRESS.get(phase.name, 0))

            max_it = phase.max_iter(ctx)
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"Job #{job.id}: Pipeline [{phase.name}] max_iter={max_it}",
                file=sys.stderr,
            )

            run = self._runner.run(
                prompt=phase.user_prompt(ctx),
                system_prompt=phase.system_prompt(ctx),
                job_id=job.id,
                on_kill_check=on_kill_check,
                max_iter=max_it,
                tools=phase.tools(),
            )

            ctx.total_in    += run.in_tok
            ctx.total_out   += run.out_tok
            ctx.total_cache += run.cache_tok
            ctx.total_cost  =  round(ctx.total_cost + run.cost, 6)
            ctx.iters       += run.iters

            if run.status == 'failed':
                # Executor-Fehler: Reporter trotzdem versuchen wenn Plan-Datei existiert
                if phase.name == 'executor' and os.path.exists(ctx.plan_path):
                    print(
                        f"[pipeline] Executor fehlgeschlagen — Reporter läuft trotzdem "
                        f"(Plan vorhanden: {ctx.plan_path})",
                        file=sys.stderr,
                    )
                    try:
                        ctx.plan = open(ctx.plan_path).read()
                    except OSError:
                        pass
                else:
                    return RunResult(
                        result=run.result,
                        status='failed',
                        error=f'[{phase.name}] {run.error}',
                        in_tok=ctx.total_in, out_tok=ctx.total_out,
                        cache_tok=ctx.total_cache, cost=ctx.total_cost,
                        iters=ctx.iters,
                    )

            try:
                phase.on_complete(ctx, run)
            except RuntimeError as exc:
                return RunResult(
                    result=run.result,
                    status='failed',
                    error=f'[{phase.name}] {exc}',
                    in_tok=ctx.total_in, out_tok=ctx.total_out,
                    cache_tok=ctx.total_cache, cost=ctx.total_cost,
                    iters=ctx.iters,
                )

        # Alle Phasen abgeschlossen
        self._set_progress(job.id, 64 | 128)   # DB-Write + Verifikation

        # Reporter hat Ergebnis in DB geschrieben → complete_job() liest es von dort
        return RunResult(
            result='',
            status='done', error='',
            in_tok=ctx.total_in, out_tok=ctx.total_out,
            cache_tok=ctx.total_cache, cost=ctx.total_cost,
            iters=ctx.iters,
        )

    @staticmethod
    def _aborted(ctx: PhaseContext) -> RunResult:
        return RunResult(
            result='', status='failed', error='Killed by user',
            in_tok=ctx.total_in, out_tok=ctx.total_out,
            cache_tok=ctx.total_cache, cost=ctx.total_cost,
            iters=ctx.iters,
        )
