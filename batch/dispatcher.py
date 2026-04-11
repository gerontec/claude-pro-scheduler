"""Dispatcher — startet Poller-Instanzen und bereinigt Zombie-Jobs."""
import os
import subprocess
import sys
import time

from .config import get_connection, release_connection, MAX_RUNNING
from .notifier import Notifier


class Dispatcher:
    def __init__(self, notifier: Notifier | None = None):
        self._notifier = notifier or Notifier()

    def run(self, n: int = MAX_RUNNING) -> None:
        """Zombie-Cleanup, dann Pollers für freie Slots starten."""
        self._cleanup_zombies()

        # Wie viele Jobs laufen gerade? Nur fehlende Slots befüllen.
        try:
            db = get_connection()
            with db.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS running FROM claude_pro_batch "
                    "WHERE status='running'"
                )
                currently_running = cur.fetchone()['running']
                cur.execute(
                    "SELECT COUNT(*) AS queued FROM claude_pro_batch "
                    "WHERE status='queued'"
                )
                queued = cur.fetchone()['queued']
            release_connection(db)
        except Exception:
            currently_running = 0
            queued = n

        free_slots = max(0, n - currently_running)
        to_start   = min(free_slots, queued)

        if to_start == 0:
            return

        poller_script = os.path.join(os.path.dirname(__file__), '..', 'batch-poller.py')
        poller_script = os.path.normpath(poller_script)
        for _ in range(to_start):
            subprocess.Popen([sys.executable, poller_script])

    def _cleanup_zombies(self) -> None:
        """
        Setzt Jobs auf done/failed wenn ihr PID nicht mehr läuft.
        Hat der Agent ein Ergebnis hinterlassen → done + Mail.
        Kein Ergebnis → failed.
        """
        try:
            db = get_connection()
            with db.cursor() as cur:
                cur.execute(
                    "SELECT id, pid, result, model, cost_usd "
                    "FROM claude_pro_batch WHERE status='running'"
                )
                rows = cur.fetchall()

            for row in rows:
                pid = row['pid']
                if pid is None:
                    # Job steckt in 'running' ohne PID — direkt auflösen
                    has_result = bool(row.get('result', ''))
                    new_status = 'done' if has_result else 'failed'
                    error_note = None if has_result else 'Zombie ohne PID — Prozess nie gestartet'
                    with db.cursor() as cur:
                        cur.execute(
                            "UPDATE claude_pro_batch SET status=%s, "
                            "error_msg=%s, finished_at=COALESCE(finished_at, NOW()) "
                            "WHERE id=%s AND status='running'",
                            (new_status, error_note, row['id'])
                        )
                        affected = cur.rowcount
                    db.commit()
                    print(f"Zombie-Job #{row['id']} (kein PID) → {new_status}")
                    if new_status == 'done' and affected > 0:
                        self._notifier.send_mail_direct(
                            job_id=row['id'],
                            status='done',
                            model=row.get('model', '?'),
                            result=row.get('result', ''),
                            cost=row.get('cost_usd'),
                        )
                    continue

                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    has_result = bool(row.get('result', ''))
                    new_status = 'done' if has_result else 'failed'
                    # error_msg nur bei failed setzen — done-Jobs sollen sauber bleiben
                    error_note = (
                        None if has_result else
                        f'Zombie: Prozess {pid} nicht mehr aktiv'
                    )
                    with db.cursor() as cur:
                        cur.execute(
                            "UPDATE claude_pro_batch SET status=%s, "
                            "error_msg=%s, finished_at=COALESCE(finished_at, NOW()) "
                            "WHERE id=%s AND status='running'",
                            (new_status, error_note, row['id'])
                        )
                        affected = cur.rowcount
                    db.commit()
                    print(f"Zombie-Job #{row['id']} (PID {pid}) → {new_status}")

                    if new_status == 'done' and affected > 0:
                        self._notifier.send_mail_direct(
                            job_id=row['id'],
                            status='done',
                            model=row.get('model', '?'),
                            result=row.get('result', ''),
                            cost=row.get('cost_usd'),
                        )
                except PermissionError:
                    pass
            release_connection(db)
        except Exception as e:
            print(f"Zombie-Cleanup Fehler: {e}")


def main():
    Dispatcher().run()


if __name__ == '__main__':
    main()
