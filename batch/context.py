"""ContextBuilder — baut den vollständigen Prompt für einen Job."""
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import SYSTEM_PROMPT, DB_CFG
from .models import JobRecord

_TZ = ZoneInfo('Europe/Berlin')

_ESCALATION_PHRASES = [
    'ich kann nicht', 'ich kann bei diesem', 'ich bin nicht in der lage',
    'bevor ich', 'muss ich bestät', 'sicherheitsbedenken', 'sicherheitshinweis',
    'i cannot', "i can't", 'i am unable', 'i need to confirm', 'i must verify',
    'before i', 'safety concern', 'i should not', 'ich sollte nicht',
    'ich darf nicht', 'nicht autorisiert', 'nicht berechtigt',
]


class ContextBuilder:
    def __init__(self, repo):
        self._repo = repo

    def build_prompt(self, job: JobRecord) -> str:
        """Vollständiger User-Prompt: Kontext + Aufgabe + Deadline-Note."""
        parts = []

        # Infrastruktur-Kontext voranstellen
        localhost_text, infra_text = self._repo.get_context_blocks()
        context = '\n\n'.join(filter(None, [localhost_text, infra_text]))
        if context and len(context) <= 12_000:
            parts.append(context)
            print(
                f"[{datetime.now(_TZ).strftime('%H:%M:%S')}] "
                f"Job #{job.id}: Kontext geladen ({len(context)} Zeichen)",
                file=sys.stderr,
            )
        elif context:
            print(
                f"[{datetime.now(_TZ).strftime('%H:%M:%S')}] "
                f"Job #{job.id}: Kontext übersprungen (zu groß: {len(context)} Zeichen)",
                file=sys.stderr,
            )

        # Session-Cache wenn gewünscht
        if job.resume_session:
            cache = self._repo.get_session_cache()
            if cache:
                parts.insert(0, cache)
                print(
                    f"[{datetime.now(_TZ).strftime('%H:%M:%S')}] "
                    f"Job #{job.id}: Session-Cache geladen ({len(cache)} Bytes)",
                    file=sys.stderr,
                )

        parts.append(job.prompt)
        parts.append(self._deadline_note(job))

        separator = '\n\n---\n'
        return separator.join(parts)

    @staticmethod
    def system_prompt() -> str:
        return SYSTEM_PROMPT

    @staticmethod
    def needs_escalation(model: str, result: str,
                         openrouter_models: dict) -> bool:
        """True wenn Modell Bedenken geäußert hat und Eskalation zu Sonnet nötig."""
        if model in ('sonnet', 'opus') or model in openrouter_models:
            return False
        return any(p in result.lower() for p in _ESCALATION_PHRASES)

    @staticmethod
    def _deadline_note(job: JobRecord) -> str:
        now        = datetime.now(_TZ)
        target     = datetime.strptime(
            str(job.targetdate), '%Y-%m-%d'
        ).replace(tzinfo=_TZ)
        hours_left = (
            target.replace(hour=23, minute=59) - now
        ).total_seconds() / 3600
        urgency = "gründlich und vollständig arbeiten — du bist ein Batch-Agent, Zeit spielt keine Rolle."
        return (
            f"**Deadline:** {job.targetdate} (noch ca. {int(hours_left)}h) – {urgency}\n\n"
            f"**PFLICHT vor Abschluss:** Schreibe dein finales Ergebnis selbst in die Datenbank "
            f"(Job-ID: {job.id}):\n"
            f"```python\n"
            f"import pymysql\n"
            f"_db = pymysql.connect(**DB_CFG, cursorclass=pymysql.cursors.DictCursor)\n"
            f"_db.cursor().execute('UPDATE claude_pro_batch SET result=%s WHERE id=%s', "
            f"('DEIN ERGEBNIS', {job.id}))\n"
            f"_db.commit(); _db.close()\n"
            f"```\n"
            f"Prüfe danach: mysql -u $WAGODB_USER -p$WAGODB_PASSWORD $WAGODB_NAME -e "
            f"\"SELECT LEFT(result,200) FROM claude_pro_batch WHERE id={job.id}\"\n"
            f"Beende erst wenn das Ergebnis korrekt in der DB steht.\n\n"
            f"**Ergebnis-Qualität — PFLICHT:**\n"
            f"Das Ergebnis muss ein vollständiger, strukturierter Bericht sein. Nicht akzeptabel:\n"
            f"- Einzeilige Zusammenfassungen ('Aufgabe erledigt.')\n"
            f"- Inhaltsleere Bestätigungen ('Das Ergebnis wurde eingetragen.')\n"
            f"- Beschreibungen was du getan hast statt was du herausgefunden hast\n\n"
            f"Mindestanforderungen an das Ergebnis:\n"
            f"- Mindestens 3 Abschnitte mit Markdown-Überschriften (##)\n"
            f"- Pro Abschnitt mindestens 3 Unterpunkte oder 2 Absätze Fließtext\n"
            f"- Konkrete Zahlen, Werte, Befunde — keine Pauschalaussagen\n"
            f"- Falls etwas nicht funktioniert hat: genaue Fehlermeldung + Ursache\n"
            f"- Abschlussabschnitt mit Bewertung / nächsten Schritten\n\n"
            f"**Pflicht-Checkliste vor Beendigung:**\n"
            f"1. Aufgabe vollständig erledigt?\n"
            f"2. Ergebnis hat mind. 3 Abschnitte mit ## Überschriften?\n"
            f"3. Konkrete Daten/Werte/Befunde enthalten (keine Pauschalaussagen)?\n"
            f"4. Ergebnis in DB geschrieben (UPDATE claude_pro_batch SET result=...)?\n"
            f"5. DB-Eintrag verifiziert (SELECT LEFT(result,200) ...)?\n"
            f"6. Erst nach Haken auf allen 5 Punkten: fertig melden."
        )
