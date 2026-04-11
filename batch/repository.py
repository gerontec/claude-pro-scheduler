"""DB-Schicht — alle SQL-Operationen auf claude_pro_batch.

Status-Übergänge laufen ausschließlich über transition_status() (State Machine).
Direkte Status-Writes sind verboten — nur claim_next() und transition_status()
dürfen den Status ändern.
"""
import os
import sys
import time

from .config import get_connection, release_connection, CONTEXT_CACHE_TTL
from .models import JobRecord, RunResult

# ── Job State Machine ────────────────────────────────────────────────────────
# Erlaubte Übergänge. Terminale Zustände haben leeres Set.
VALID_TRANSITIONS: dict[str, set[str]] = {
    'queued':  {'running'},
    'running': {'done', 'failed'},
    'done':    set(),
    'failed':  set(),
}


class JobRepository:
    def __init__(self, db=None):
        self._own_conn = db is None
        self._db = db or get_connection()
        self._ctx_cache = {'ts': 0.0, 'data': None}

    @property
    def db(self):
        return self._db

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Holt eine frische Verbindung, führt SELECT aus, gibt Rows zurück."""
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        finally:
            release_connection(conn)

    # ── Claim ───────────────────────────────────────────────────

    def claim_next(self) -> JobRecord | None:
        """
        Atomares Claim: SELECT FOR UPDATE in einer Transaktion.
        Vermeidet Race-Condition (vorher: flock + zwei Cursor ohne TX).
        """
        conn = get_connection()
        try:
            conn.autocommit(False)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM claude_pro_batch "
                    "WHERE status='running'"
                )
                if cur.fetchone()['n'] >= 9:
                    conn.rollback()
                    return None

                cur.execute(
                    "SELECT id, targetdate, model, resume_session, prompt "
                    "FROM claude_pro_batch WHERE status='queued' "
                    "ORDER BY targetdate ASC, created_at ASC "
                    "LIMIT 1 FOR UPDATE"
                )
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return None

                cur.execute(
                    "UPDATE claude_pro_batch SET status='running', "
                    "started_at=NOW(), pid=%s, progress=0 WHERE id=%s",
                    (os.getpid(), row['id'])
                )
                conn.commit()

            return JobRecord(
                id=row['id'], model=row['model'], prompt=row['prompt'],
                targetdate=row['targetdate'],
                resume_session=bool(row['resume_session']),
            )
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return None
        finally:
            conn.autocommit(True)
            release_connection(conn)

    # ── State Machine ───────────────────────────────────────────

    def transition_status(
        self, job_id: int, new_status: str, error_msg: str = ''
    ) -> bool:
        """Atomarer, validierter Status-Übergang.

        Verwendet SELECT FOR UPDATE um Race Conditions bei parallelen Jobs
        auszuschließen. Gibt False zurück wenn der Übergang ungültig ist
        (z.B. done→running) — kein Exception.
        """
        conn = get_connection()
        try:
            conn.autocommit(False)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM claude_pro_batch WHERE id=%s FOR UPDATE",
                    (job_id,)
                )
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return False

                old_status = row['status']
                if new_status not in VALID_TRANSITIONS.get(old_status, set()):
                    print(
                        f"[SM] Ungültiger Übergang: {old_status}→{new_status} "
                        f"für Job #{job_id} — ignoriert",
                        file=sys.stderr,
                    )
                    conn.rollback()
                    return False

                extra_sql = ''
                params: list = [new_status]
                if new_status in ('done', 'failed'):
                    extra_sql += ', finished_at=COALESCE(finished_at, NOW())'
                if error_msg:
                    extra_sql += ', error_msg=%s'
                    params.append(error_msg)
                params.extend([job_id, old_status])

                cur.execute(
                    f"UPDATE claude_pro_batch SET status=%s{extra_sql} "
                    f"WHERE id=%s AND status=%s",
                    params,
                )
            conn.commit()
            return True
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[SM] transition_status Fehler: {exc}", file=sys.stderr)
            return False
        finally:
            conn.autocommit(True)
            release_connection(conn)

    # ── Write ───────────────────────────────────────────────────

    def write_result(self, job_id: int, run: RunResult) -> None:
        """Schreibt Ergebnis-Daten und überführt Status via State Machine.
        Agent-geschriebenes result wird bevorzugt wenn länger."""
        # Status-Übergang über SM — verhindert ungültige Writes
        self.transition_status(job_id, run.status, run.error or '')

        db = get_connection()
        try:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE claude_pro_batch SET "
                    "result=COALESCE(NULLIF(result,''), %s), "
                    "input_tokens=%s, output_tokens=%s, cache_tokens=%s, "
                    "cost_usd=%s WHERE id=%s",
                    (run.result, run.in_tok, run.out_tok,
                     run.cache_tok, run.cost, job_id)
                )
            db.commit()

            # ── Fallback-Check: result darf nie leer bleiben ──
            with db.cursor() as cur:
                cur.execute(
                    "SELECT result FROM claude_pro_batch WHERE id=%s", (job_id,)
                )
                row = cur.fetchone()
            if not row or not row['result'] or not row['result'].strip():
                fallback = (
                    f"Agent hat kein Ergebnis hinterlassen "
                    f"(Iters: {run.iters}, Status: {run.status})"
                )
                with db.cursor() as cur:
                    cur.execute(
                        "UPDATE claude_pro_batch SET result=%s WHERE id=%s",
                        (fallback, job_id)
                    )
                db.commit()
                print(
                    f"[write_result] Fallback-Text für Job #{job_id}: result war leer!",
                    flush=True
                )
        finally:
            release_connection(db)

    def complete_job(self, job_id: int, run: RunResult) -> RunResult:
        """Processor-seitiger Abschluss.

        Liest das agent-geschriebene result (falls vorhanden und länger als runner-result),
        merged es in den RunResult, schreibt dann via write_result.
        Der Processor setzt immer den finalen Status — kein blindes Akzeptieren
        von agent-gesetztem status='done'.
        Gibt den finalen RunResult zurück (mit gemergtem result für Notifier/Mail).
        """
        db = get_connection()
        try:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT result FROM claude_pro_batch WHERE id=%s", (job_id,)
                )
                row = cur.fetchone()
        finally:
            release_connection(db)

        agent_result = (row['result'] or '').strip() if row else ''
        runner_result = (run.result or '').strip()

        # Agent-Ergebnis bevorzugen wenn substantiell länger
        if len(agent_result) > len(runner_result):
            run = RunResult(
                result=agent_result,
                status=run.status, error=run.error,
                in_tok=run.in_tok, out_tok=run.out_tok,
                cache_tok=run.cache_tok, cost=run.cost, iters=run.iters,
            )
            print(
                f"[complete_job] Job #{job_id}: Agent-Result übernommen "
                f"({len(agent_result)} > {len(runner_result)} Zeichen)",
                file=sys.stderr,
            )

        self.write_result(job_id, run)
        return run

    # ── Read ────────────────────────────────────────────────────

    def read_agent_result(self, job_id):
        rows = self._query("SELECT result FROM claude_pro_batch WHERE id=%s", (job_id,))
        row = rows[0] if rows else None
        return row['result'] if row and row['result'] else None

    def read_db_status(self, job_id) -> str | None:
        """Liest den aktuellen Status direkt aus der DB (Agent kann ihn direkt gesetzt haben)."""
        rows = self._query("SELECT status FROM claude_pro_batch WHERE id=%s", (job_id,))
        row = rows[0] if rows else None
        return row['status'] if row else None

    def is_killed(self, job_id):
        rows = self._query("SELECT status FROM claude_pro_batch WHERE id=%s", (job_id,))
        row = rows[0] if rows else None
        return bool(row and row['status'] == 'failed')

    def requeue_with_quality_feedback(self, job_id: int, quality_error: str) -> int:
        """Reiht den Job erneut ein mit Qualitäts-Feedback im Prompt.

        Das neue Job-Prompt enthält:
        - Hinweis auf den Qualitätsfehler
        - Das bereits geschriebene (unzureichende) Ergebnis
        - Den originalen Prompt
        Gleiches Modell und targetdate wie Original.
        """
        db = get_connection()
        try:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT model, targetdate, prompt, result FROM claude_pro_batch WHERE id=%s",
                    (job_id,)
                )
                row = cur.fetchone()
            if not row:
                return -1

            prev_result = (row['result'] or '').strip()
            feedback_prompt = (
                f"[QUALITÄTS-GATE FEHLGESCHLAGEN — Job #{job_id}]\n\n"
                f"Fehler: {quality_error}\n\n"
                f"Das Ergebnis des vorherigen Laufs war unzureichend. "
                f"Lies es zuerst:\n"
                f"```\n{prev_result[:800]}\n```\n\n"
                f"Schreibe jetzt ein vollständiges, ausführliches Ergebnis mit "
                f"mindestens 3 Abschnitten (##) und mind. 400 Zeichen.\n\n"
                f"— Ursprüngliche Aufgabe —\n{row['prompt']}"
            )

            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO claude_pro_batch (targetdate, model, resume_session, prompt) "
                    "VALUES (%s, %s, 0, %s)",
                    (row['targetdate'], row['model'], feedback_prompt)
                )
                new_id = cur.lastrowid
            db.commit()
            return new_id
        finally:
            release_connection(db)

    def escalate_to_sonnet(self, job_id):
        with self._db.cursor() as cur:
            cur.execute(
                "INSERT INTO claude_pro_batch (targetdate,model,resume_session,prompt) "
                "SELECT targetdate,'sonnet',resume_session,prompt "
                "FROM claude_pro_batch WHERE id=%s", (job_id,)
            )
            new_id = cur.lastrowid
        self._db.commit()
        return new_id

    def get_session_cache(self):
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT JSON_UNQUOTE(JSON_EXTRACT(context_json,'$.summary')) AS s "
                "FROM claude_context_cache WHERE scope='session-compact' LIMIT 1"
            )
            row = cur.fetchone()
        v = row['s'] if row else None
        return v if v and v != 'NULL' else None

    # ── Context (gecacht) ───────────────────────────────────────

    def get_context_blocks(self) -> tuple[str, str]:
        now = time.time()
        if now - self._ctx_cache['ts'] < CONTEXT_CACHE_TTL and self._ctx_cache['data']:
            return self._ctx_cache['data']

        localhost_text = ''
        infra_text = ''
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT category,label,value FROM ki_localhost_cache "
                    "ORDER BY category,label"
                )
                rows = cur.fetchall()
            if rows:
                lines = ['## Batch-Server localhost (ki_localhost_cache)', '']
                cat = None
                for r in rows:
                    if r['category'] != cat:
                        cat = r['category']
                        lines.append(f"\n### {cat}")
                    lines.append(f"- **{r['label']}**: {r['value']}")
                localhost_text = '\n'.join(lines)

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ip_address,hostname,network_range,open_ports,"
                    "services,device_purpose,os_guess FROM ki_infrastructure "
                    "ORDER BY network_range,ip_address"
                )
                rows = cur.fetchall()
            if rows:
                lines = ['## Netzwerk-Infrastruktur (ki_infrastructure)', '']
                for r in rows:
                    p = [f"**{r['ip_address']}**"]
                    if r['hostname']:      p.append(f"({r['hostname']})")
                    if r['network_range']: p.append(f"[{r['network_range']}]")
                    if r['device_purpose']:p.append(f"→ {r['device_purpose']}")
                    if r['open_ports']:    p.append(f"| Ports: {r['open_ports']}")
                    if r['services']:      p.append(f"| Services: {r['services']}")
                    if r['os_guess']:      p.append(f"| OS: {r['os_guess']}")
                    lines.append('  '.join(p))
                infra_text = '\n'.join(lines)
        finally:
            release_connection(conn)

        self._ctx_cache = {'ts': now, 'data': (localhost_text, infra_text)}
        return self._ctx_cache['data']

    def save_openrouter_balance(self, job_id, remaining, total, used):
        db = get_connection()
        try:
            with db.cursor() as cur:
                for lbl, val in [
                    ('balance_usd', f"{remaining:.6f}"),
                    ('total_credits_usd', f"{total:.2f}"),
                    ('total_usage_usd', f"{used:.6f}"),
                    ('last_job_id', str(job_id)),
                ]:
                    cur.execute(
                        "INSERT INTO ki_localhost_cache (category,label,value) "
                        "VALUES ('openrouter',%s,%s) "
                        "ON DUPLICATE KEY UPDATE value=%s, updated_at=NOW()",
                        (lbl, val, val)
                    )
            db.commit()
        finally:
            release_connection(db)

    def close(self):
        if self._own_conn:
            release_connection(self._db)
