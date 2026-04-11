"""OpenRouterHttpClient — HTTP-Transport für die OpenRouter API."""
import http.client
import json
import time
import urllib.error
import urllib.request

from ..config import (
    OPENROUTER_URL, OPENROUTER_CREDITS,
    HTTP_TIMEOUT_SEC, HTTP_RETRIES, HTTP_RETRY_DELAY,
)


class OpenRouterHttpClient:
    """Kapselt alle HTTP-Kommunikation mit OpenRouter.

    Verantwortlich für:
    - POST /chat/completions mit Retry bei Netzwerkfehlern
    - GET /credits für Guthaben-Abfrage
    Nicht verantwortlich für: Agentic Loop, Tool-Dispatch, Job-Lifecycle.
    """

    def __init__(self, api_key: str):
        self._api_key = api_key

    def chat(self, model_id: str, messages: list, system_prompt: str,
             tools: list | None = None) -> dict:
        """Sendet Chat-Request, wiederholt bei transienten Fehlern."""
        last_err = None
        for attempt in range(1 + HTTP_RETRIES):
            try:
                return self._post_chat(model_id, messages, system_prompt, tools or [])
            except (urllib.error.URLError, urllib.error.HTTPError,
                    ConnectionError, TimeoutError,
                    http.client.IncompleteRead, http.client.RemoteDisconnected) as e:
                last_err = e
                code = getattr(e, 'code', 0)
                if code in (400, 401, 403, 404):
                    raise
                if attempt < HTTP_RETRIES:
                    wait = HTTP_RETRY_DELAY * (2 ** attempt)
                    import sys
                    print(f"    [Retry {attempt+1}] {e} — warte {wait}s", file=sys.stderr)
                    time.sleep(wait)
        raise last_err

    def get_credits(self) -> dict:
        """Gibt {'remaining': float, 'total': float, 'used': float} zurück."""
        req = urllib.request.Request(
            OPENROUTER_CREDITS,
            headers={'Authorization': f'Bearer {self._api_key}'},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            cr = json.loads(resp.read())['data']
        total     = float(cr.get('total_credits', 0))
        used      = float(cr.get('total_usage', 0))
        remaining = round(total - used, 6)
        return {'remaining': remaining, 'total': total, 'used': used}

    # ── intern ────────────────────────────────────────────────────

    def _post_chat(self, model_id: str, messages: list, system_prompt: str,
                   tools: list) -> dict:
        payload = json.dumps({
            'model':       model_id,
            'messages':    [{'role': 'system', 'content': system_prompt}] + messages,
            'tools':       tools,
            'tool_choice': 'auto',
        }).encode()
        req = urllib.request.Request(
            OPENROUTER_URL,
            data=payload,
            headers={
                'Authorization': f'Bearer {self._api_key}',
                'Content-Type':  'application/json',
                'HTTP-Referer':  'https://localhost',
            },
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            raw = resp.read()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(
                f'OpenRouter JSON-Parse-Fehler: {e} | '
                f'Response-Anfang: {raw[:500]!r}'
            )

