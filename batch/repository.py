"""DB-Schicht — alle SQL-Operationen auf claude_pro_batch.

Verbesserungen:
- Connection-Pooling via config.get_connection/release_connection
- claim_next() nutzt SELECT FOR UPDATE (Race-Condition-frei)
- Kontext-Blocks gecacht (5 Min TTL) statt pro Job neu laden
- write_result() prüft ob result leer blieb → Fallback-Text
"""
import os
import sys
import time

from .config import get_connection, release_connection, CONTEXT_CACHE_TTL
from .models import JobRecord, RunResult


class JobRepository:
    def __init__(self, db=None):
        self._own_conn = db is None
        self._db = db or get_connection()
        self._ctx_cache = {'ts': 0.0, 'data': None}

    @property
    def db(self):
        return self._db

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

    # ── Write ───────────────────────────────────────────────────

    def write_result(self, job_id: int, run: RunResult) -> None:
        """Persistiert Ergebnis. Agent-DB-Schreibung hat Vorrang (COALESCE).
        Prüft danach ob result wirklich in DB steht; schreibt Fallback falls leer."""
        db = get_connection()
        try:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE claude_pro_batch SET status=%s, "
                    "result=COALESCE(NULLIF(result,''), %s), "
                    "input_tokens=%s, output_tokens=%s, cache_tokens=%s, "
                    "cost_usd=%s, finished_at=COALESCE(finished_at,NOW()), "
                    "error_msg=%s WHERE id=%s AND status!='done'",
                    (run.status, run.result, run.in_tok, run.out_tok,
                     run.cache_tok, run.cost, run.error, job_id)
                )
                if cur.rowcount == 0:
                    print(
                        f"[write_result] WARNUNG: Job #{job_id} — rowcount=0 (status bereits 'done'), "
                        f"aktualisiere nur Tokens/Cost",
                        file=sys.stderr,
                    )
                    cur.execute(
                        "UPDATE claude_pro_batch SET input_tokens=%s, "
                        "output_tokens=%s, cache_tokens=%s, cost_usd=%s "
                        "WHERE id=%s AND (input_tokens IS NULL OR input_tokens=0)",
                        (run.in_tok, run.out_tok, run.cache_tok, run.cost, job_id)
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


    def complete_job(self, job_id: int, run: RunResult) -> str:
        """Einheitliches Write-Interface — deckt BEIDE Schreibwege ab.
        
        Logik:
        - DB-status='done' UND DB-result nicht leer: behalte result, update nur tokens/cost
        - DB-status='running': normaler write_result-Flow
        - Gibt immer den finalen DB-status zurück
        """
        db = get_connection()
        try:
            with db.cursor() as cur:
                cur.execute("SELECT status, result FROM claude_pro_batch WHERE id=%s", (job_id,))
                row = cur.fetchone()
            
            if not row:
                return 'unknown'
            
            current_status = row['status']
            current_result = row['result']
            
            if current_status == 'done' and current_result and current_result.strip():
                # Agent hat bereits status='done' + result geschrieben — nur Tokens/Cost updaten
                with db.cursor() as cur:
                    cur.execute(
                        "UPDATE claude_pro_batch SET input_tokens=%s, output_tokens=%s, "
                        "cache_tokens=%s, cost_usd=%s WHERE id=%s "
                        "AND (input_tokens IS NULL OR input_tokens=0)",
                        (run.in_tok, run.out_tok, run.cache_tok, run.cost, job_id)
                    )
                db.commit()
                print(f"[complete_job] Job #{job_id}: Agent-Result beibehalten (status war bereits 'done')",
                      file=sys.stderr)
                return 'done'
            else:
                # Normaler Processor-Write
                self.write_result(job_id, run)
                return run.status
        finally:
            release_connection(db)

    # ── Read ────────────────────────────────────────────────────

    def read_agent_result(self, job_id):
        with self._db.cursor() as cur:
            cur.execute("SELECT result FROM claude_pro_batch WHERE id=%s", (job_id,))
            row = cur.fetchone()
        return row['result'] if row and row['result'] else None

    def read_db_status(self, job_id) -> str | None:
        """Liest den aktuellen Status direkt aus der DB (Agent kann ihn direkt gesetzt haben)."""
        with self._db.cursor() as cur:
            cur.execute("SELECT status FROM claude_pro_batch WHERE id=%s", (job_id,))
            row = cur.fetchone()
        return row['status'] if row else None

    def is_killed(self, job_id):
        with self._db.cursor() as cur:
            cur.execute("SELECT status FROM claude_pro_batch WHERE id=%s", (job_id,))
            row = cur.fetchone()
        return bool(row and row['status'] == 'failed')

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
