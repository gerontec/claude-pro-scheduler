"""OpenRouter agentic loop Runner (qwen-free, xiaomi, mimo-pro)."""
import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from typing import Callable

from ..config import (OPENROUTER_URL, MAX_TOOL_ITERATIONS, MAX_TOOL_OUTPUT,
                      HTTP_TIMEOUT_SEC, HTTP_RETRIES, HTTP_RETRY_DELAY)
from ..models import RunResult
from .base import ModelRunner

TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'exec',
            'description': (
                'Führe einen Shell-Befehl auf dem Batch-Server aus. '
                'SSH zu allen bekannten Nodes ist passwordlos möglich. '
                'IPs und Hostnamen stehen im ki_infrastructure-Kontext.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'command': {
                        'type': 'string',
                        'description': 'Shell-Befehl (wird via bash -c ausgeführt)',
                    },
                    'timeout': {
                        'type': 'integer',
                        'description': 'Timeout in Sekunden (default 60)',
                        'default': 60,
                    },
                },
                'required': ['command'],
            },
        },
    }
]


class OpenRouterRunner(ModelRunner):
    def __init__(self, model_id: str, api_key: str):
        self.model_id = model_id
        self.api_key  = api_key

    def run(
        self,
        prompt: str,
        system_prompt: str,
        job_id: int,
        on_kill_check: Callable[[], bool],
    ) -> RunResult:
        messages   = [{'role': 'user', 'content': prompt}]
        total_in   = total_out = total_cache = 0
        total_cost = 0.0
        # Loop-Erkennung: (befehl, output_hash) → wie oft identisch gesehen
        # Gleicher Befehl mit gleichem Output = kein Fortschritt = Loop.
        # Gleicher Befehl mit anderem Output (z.B. nach Dateiänderung) = OK.
        stall_counts: dict[tuple, int] = {}
        cmd_counts: dict[str, int] = {}  # für Debug-Report

        for iteration in range(1, MAX_TOOL_ITERATIONS + 1):
            if on_kill_check():
                return RunResult(
                    result='', status='failed', error='Killed by user',
                    in_tok=total_in, out_tok=total_out,
                    cache_tok=total_cache, cost=total_cost, iters=iteration,
                )

            body = self._call_api_with_retry(messages, system_prompt)

            if 'error' in body:
                raise ValueError(f"OpenRouter API Error: {body['error']}")

            usage       = body.get('usage', {})
            total_in   += usage.get('prompt_tokens', 0)
            total_out  += usage.get('completion_tokens', 0)
            total_cache += (
                usage.get('cache_read_input_tokens', 0)
                + usage.get('prompt_tokens_details', {}).get('cached_tokens', 0)
            )
            total_cost = round(total_cost + float(usage.get('cost', 0) or 0), 6)

            choice        = body['choices'][0]
            finish_reason = choice.get('finish_reason', '')
            msg           = choice['message']
            tool_calls    = msg.get('tool_calls') or []

            if tool_calls:
                messages.append({
                    'role':       'assistant',
                    'content':    msg.get('content'),
                    'tool_calls': tool_calls,
                })
                for tc in tool_calls:
                    fn_name     = tc['function']['name']
                    args        = self._parse_args(tc['function']['arguments'])

                    tool_result = self._dispatch_tool(fn_name, args)

                    # Loop-Erkennung: Befehl + Output-Hash — nur echter Stillstand ist Loop
                    if fn_name == 'exec':
                        cmd_key = args.get('command', '')
                        cmd_counts[cmd_key] = cmd_counts.get(cmd_key, 0) + 1
                        out_sig  = hash(tool_result[:500])
                        stall_key = (cmd_key, out_sig)
                        stall_counts[stall_key] = stall_counts.get(stall_key, 0) + 1
                        if stall_counts[stall_key] >= 3:
                            print(
                                f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id}: "
                                f"Loop-Abbruch — Befehl+Output {stall_counts[stall_key]}× "
                                f"identisch: {cmd_key[:80]}",
                                file=sys.stderr,
                            )
                            return RunResult(
                                result=(
                                    f'[LOOP-ABBRUCH nach {iteration} Iterationen]\n'
                                    f'Befehl+Output {stall_counts[stall_key]}× identisch '
                                    f'(kein Fortschritt):\n{cmd_key[:200]}\n\n'
                                    f'Output (Anfang):\n{tool_result[:300]}'
                                ),
                                status='failed',
                                error=f'Endlos-Loop erkannt (Iter {iteration})',
                                in_tok=total_in, out_tok=total_out,
                                cache_tok=total_cache, cost=total_cost,
                                iters=iteration,
                            )
                    messages.append({
                        'role':         'tool',
                        'tool_call_id': tc['id'],
                        'content':      tool_result,
                    })
                continue  # Nächste Iteration mit Tool-Ergebnissen

            # Kein Tool-Call → finish_reason=stop → Ergebnis fertig
            result_text = msg.get('content') or ''

            if '<tool_call>' in result_text or '"name": "exec"' in result_text:
                result_text = (
                    '[WARNUNG: Modell hat tool_call als Text ausgegeben]\n\n'
                    + result_text
                )

            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id}: "
                f"OpenRouter OK ({total_in}/{total_out} tok, "
                f"{iteration} Iter., ${total_cost})",
                file=sys.stderr,
            )
            return RunResult(
                result=result_text, status='done', error='',
                in_tok=total_in, out_tok=total_out,
                cache_tok=total_cache, cost=total_cost, iters=iteration,
            )

        debug = self._build_max_iter_debug(messages, cmd_counts, MAX_TOOL_ITERATIONS)
        return RunResult(
            result=debug,
            status='failed', error=f'Max iterations ({MAX_TOOL_ITERATIONS}) erreicht',
            in_tok=total_in, out_tok=total_out,
            cache_tok=total_cache, cost=total_cost, iters=MAX_TOOL_ITERATIONS,
        )

    @staticmethod
    def _build_max_iter_debug(
        messages: list, cmd_counts: dict, max_iter: int
    ) -> str:
        """Erstellt einen lesbaren Debug-Report wenn Max-Iterations erreicht wurde."""
        lines = [
            f'# MAX_ITERATIONS={max_iter} erreicht — Debug-Report',
            '',
        ]

        # Häufigste Befehle
        if cmd_counts:
            lines.append('## Befehlshäufigkeit (Loop-Kandidaten)')
            for cmd, count in sorted(cmd_counts.items(), key=lambda x: -x[1]):
                marker = ' ← LOOP' if count >= 3 else ''
                lines.append(f'  {count}× {cmd[:120]}{marker}')
            lines.append('')

        # Letzte 6 Nachrichten der Konversation
        lines.append(f'## Letzte Konversationsschritte (von {len(messages)} gesamt)')
        tail = messages[-6:]
        for i, msg in enumerate(tail, start=len(messages) - len(tail) + 1):
            role = msg.get('role', '?')
            if role == 'user':
                content = str(msg.get('content', ''))[:200]
                lines.append(f'\n### [{i}] user\n{content}')
            elif role == 'assistant':
                content = str(msg.get('content') or '')[:300]
                tool_calls = msg.get('tool_calls') or []
                lines.append(f'\n### [{i}] assistant')
                if content:
                    lines.append(content)
                for tc in tool_calls:
                    fn = tc.get('function', {})
                    try:
                        args = json.loads(fn.get('arguments', '{}'))
                        cmd = args.get('command', '')[:200]
                    except Exception:
                        cmd = str(fn.get('arguments', ''))[:200]
                    lines.append(f'  → exec: {cmd}')
            elif role == 'tool':
                content = str(msg.get('content', ''))
                # Nur Anfang + Ende wenn lang
                if len(content) > 400:
                    content = content[:300] + f'\n… [{len(content)} Zeichen gesamt] …\n' + content[-80:]
                lines.append(f'\n### [{i}] tool-result\n{content}')

        return '\n'.join(lines)

    def _call_api_with_retry(self, messages: list, system_prompt: str) -> dict:
        last_err = None
        for attempt in range(1 + HTTP_RETRIES):
            try:
                return self._call_api(messages, system_prompt)
            except (urllib.error.URLError, urllib.error.HTTPError,
                    ConnectionError, TimeoutError) as e:
                last_err = e
                code = getattr(e, 'code', 0)
                if code in (400, 401, 403, 404):
                    raise
                if attempt < HTTP_RETRIES:
                    wait = HTTP_RETRY_DELAY * (2 ** attempt)
                    print(f"    [Retry {attempt+1}] {e} — warte {wait}s", file=sys.stderr)
                    time.sleep(wait)
        raise last_err

    def _call_api(self, messages: list, system_prompt: str) -> dict:
        payload = json.dumps({
            'model':       self.model_id,
            'messages':    [{'role': 'system', 'content': system_prompt}] + messages,
            'tools':       TOOLS,
            'tool_choice': 'auto',
        }).encode()
        req = urllib.request.Request(
            OPENROUTER_URL,
            data=payload,
            headers={
                'Authorization': f'Bearer {self.api_key}',
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

    def _parse_args(self, raw: str) -> dict:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, Exception):
            args = {}
            for m in re.finditer(
                r'<parameter=(\w+)>(.*?)</parameter>', raw, re.DOTALL
            ):
                args[m.group(1)] = m.group(2).strip()
            return args

    def _dispatch_tool(self, fn_name: str, args: dict) -> str:
        if fn_name == 'exec':
            cmd     = args.get('command', '')
            timeout = int(float(str(args.get('timeout', 60)).split('>')[0].strip() or 60))
            print(f"    [exec] {cmd[:120]}", file=sys.stderr)
            return self._exec_tool(cmd, timeout)
        return f'Unbekanntes Tool: {fn_name}'

    @staticmethod
    def _exec_tool(command: str, timeout: int = 60) -> str:
        try:
            proc = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                timeout=timeout, env={**__import__('os').environ, 'TERM': 'dumb'},
            )
            out = proc.stdout + proc.stderr
            if len(out) > MAX_TOOL_OUTPUT:
                out = out[:MAX_TOOL_OUTPUT] + f'\n... [OUTPUT TRUNCATED at {MAX_TOOL_OUTPUT} chars]'
            return out or '(kein Output)'
        except subprocess.TimeoutExpired:
            return f'TIMEOUT nach {timeout}s'
        except Exception as exc:
            return f'EXEC-ERROR: {exc}'
