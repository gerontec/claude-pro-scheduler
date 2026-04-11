"""Notifier — Mail nach Job-Abschluss (mit PDF-Anhang)."""
import json
import smtplib
import socket
import sys
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .diagram import render_png
from .pdf import PdfRenderer

from .config import SMTP_HOST, SMTP_PORT, MAIL_FROM, MAIL_TO
from .models import JobRecord, RunResult


class Notifier:
    def notify(self, job: JobRecord, run: RunResult) -> None:
        """Sendet Mail (nur bei done)."""
        if run.status == 'done':
            self._mail(job, run)

    def send_mail_direct(self, job_id: int, status: str, model: str,
                         result: str, cost) -> None:
        """Direkt-Aufruf ohne JobRecord/RunResult (für Dispatcher-Zombie-Fix)."""
        self._send_mail(job_id, status, model, result, cost)

    # ── intern ────────────────────────────────────────────────

    def _mail(self, job: JobRecord, run: RunResult) -> None:
        self._send_mail(job.id, run.status, job.model, run.result, run.cost)

    def _send_mail(self, job_id: int, status: str, model: str,
                   result: str, cost) -> None:
        try:
            own_ipv4 = self._own_ipv4()
            own_ipv6 = self._own_ipv6()
            body = (
                f"Job #{job_id} abgeschlossen\n"
                f"Modell:  {model}\n"
                f"Status:  {status}\n"
                f"Kosten:  ${cost or 'n/a'}\n"
                f"Agent:   {own_ipv4}  /  {own_ipv6}\n"
                f"\n── Ergebnis ──────────────────────────────────────\n"
                f"{result or ''}"
            )
            msg = MIMEMultipart()
            msg['From']    = MAIL_FROM
            msg['To']      = MAIL_TO
            msg['Subject'] = f'[KI-Job #{job_id}] {status} — {model}'
            msg.attach(MIMEText(body, 'plain', 'utf-8'))

            # PNG-Klassendiagramm rendern (falls relevant)
            png_bytes = None
            diagram_keywords = ('klassendiagramm', 'class diagram', 'objektmodell',
                                 'oo-modell', 'klassenstruktur')
            if any(k in (result or '').lower() for k in diagram_keywords):
                try:
                    png_bytes = render_png()
                except Exception as png_err:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] PNG-Fehler Job #{job_id}: {png_err}",
                          file=sys.stderr)

            # PDF-Anhang (inkl. eingebettetes Diagramm)
            try:
                pdf_bytes = PdfRenderer().render(job_id, model, status, cost, result,
                                                 png_bytes=png_bytes)
                part = MIMEApplication(pdf_bytes, _subtype='pdf')
                part.add_header('Content-Disposition', 'attachment',
                                filename=f'ki-job-{job_id}.pdf')
                msg.attach(part)
            except Exception as pdf_err:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] PDF-Fehler Job #{job_id}: {pdf_err}",
                      file=sys.stderr)

            # PNG-Klassendiagramm als separaten Anhang anfügen (unverändert)
            if png_bytes:
                try:
                    img_part = MIMEApplication(png_bytes, _subtype='png')
                    img_part.add_header('Content-Disposition', 'attachment',
                                        filename=f'ki-job-{job_id}-diagram.png')
                    msg.attach(img_part)
                except Exception as png_err:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] PNG-Anhang-Fehler Job #{job_id}: {png_err}",
                          file=sys.stderr)

            smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
            smtp.sendmail(MAIL_FROM, [MAIL_TO], msg.as_string())
            smtp.quit()
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] Mail gesendet für Job #{job_id}",
                file=sys.stderr,
            )
        except Exception as e:
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] Mail-Fehler Job #{job_id}: {e}",
                file=sys.stderr,
            )

    def _mqtt(self, job: JobRecord, run: RunResult) -> None:
        try:
            import paho.mqtt.client as mqtt
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=f'ki-poller-{job.id}',
                clean_session=True,
            )
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=10)
            payload = json.dumps({
                'id':       job.id,
                'status':   run.status,
                'model':    job.model,
                'result':   run.result or '',
                'cost_usd': run.cost,
            })
            client.publish(f'ki/job/result/{job.id}', payload, qos=1)
            client.publish('ki/job/result', payload, qos=1)
            client.disconnect()
        except Exception:
            pass  # MQTT optional

    @staticmethod
    def _own_ipv4() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((SMTP_HOST, SMTP_PORT))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return 'n/a'

    @staticmethod
    def _own_ipv6() -> str:
        try:
            return socket.getaddrinfo(
                socket.gethostname(), None, socket.AF_INET6
            )[0][4][0]
        except Exception:
            return 'n/a'
