"""OpenRouter agentic loop Runner (qwen-free, xiaomi, mimo-pro)."""
import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from typing import Callable

from ..config import (MAX_TOOL_ITERATIONS, MAX_TOOL_OUTPUT,
                      BATCH_API_URL, BATCH_API_KEY, MAX_PARALLEL_AGENTS)
from .openrouter_http import OpenRouterHttpClient
from ..models import RunResult
from .base import ModelRunner

TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'exec',
            'description': (
                'Führe einen Shell-Befehl auf dem Batch-Server (<BATCH_SERVER_IP>) aus. '
                'SSH zu allen bekannten Nodes ist passwordlos möglich. '
                'Beispiel: ssh pi@<RASPBERRY_PI_IP> "befehl"'
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
    },
    {
        'type': 'function',
        'function': {
            'name': 'delegate',
            'description': (
                'Beauftrage genau 1 Sub-Agenten mit einer klar abgegrenzten Teilaufgabe. '
                'Der Sub-Agent hat Shell-Zugriff (exec) und kann DB/Netzwerk abfragen. '
                'Nutze dies NUR wenn die Teilaufgabe völlig unabhängig ist und separat laufen kann. '
                'NICHT nutzen für einfache Shell-Befehle — die direkt mit exec ausführen. '
                'Standard-Modell: xiaomi (günstig). Für komplexe Analyse: mimo-pro.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'tasks': {
                        'type': 'array',
                        'description': 'Liste von Aufgaben-Prompts für Sub-Agenten (max 9)',
                        'items': {'type': 'string'},
                        'maxItems': 1,
                    },
                    'model': {
                        'type': 'string',
                        'description': 'Modell für alle Sub-Agenten (default: xiaomi)',
                        'enum': ['xiaomi', 'mimo-pro'],
                        'default': 'xiaomi',
                    },
                    'timeout_minutes': {
                        'type': 'integer',
                        'description': 'Warte-Timeout pro Sub-Job in Minuten (default: 10)',
                        'default': 10,
                    },
                },
                'required': ['tasks'],
            },
        },
    },
]


class OpenRouterRunner(ModelRunner):
    def __init__(
        self, model_id: str, api_key: str,
        batch_api_url: str = BATCH_API_URL,
        batch_api_key: str = BATCH_API_KEY,
    ):
        self.model_id       = model_id
        self.api_key        = api_key
        self._http          = OpenRouterHttpClient(api_key)
        self._batch_api_url = batch_api_url
        self._batch_api_key = batch_api_key

    def run(
        self,
        prompt: str,
        system_prompt: str,
        job_id: int,
        on_kill_check: Callable[[], bool],
        max_iter: int | None = None,
        tools: list | None = None,   # None = TOOLS-Default, [] = keine Tools
    ) -> RunResult:
        effective_max_iter = max_iter if max_iter is not None else MAX_TOOL_ITERATIONS
        effective_tools    = tools if tools is not None else TOOLS
        messages   = [{'role': 'user', 'content': prompt}]
        total_in   = total_out = total_cache = 0
        total_cost = 0.0
        stall_counts: dict[tuple, int] = {}
        cmd_counts: dict[str, int] = {}

        for iteration in range(1, effective_max_iter + 1):
            if on_kill_check():
                return RunResult(
                    result='', status='failed', error='Killed by user',
                    in_tok=total_in, out_tok=total_out,
                    cache_tok=total_cache, cost=total_cost, iters=iteration,
                )

            body = self._http.chat(self.model_id, messages, system_prompt, effective_tools)

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

        debug = self._build_max_iter_debug(messages, cmd_counts, effective_max_iter)
        return RunResult(
            result=debug,
            status='failed', error=f'Max iterations ({effective_max_iter}) erreicht',
            in_tok=total_in, out_tok=total_out,
            cache_tok=total_cache, cost=total_cost, iters=effective_max_iter,
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

    def _parse_args(self, raw: str) -> dict:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            # Modell hat kein valides JSON geliefert — XML-ähnlichen Fallback versuchen
            print(
                f"    [_parse_args] JSON-Parse fehlgeschlagen: {e} | "
                f"raw[:120]={raw[:120]!r}",
                file=sys.stderr,
            )
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
        if fn_name == 'delegate':
            tasks = args.get('tasks', [])
            model = args.get('model', 'xiaomi')
            print(
                f"    [delegate] {len(tasks)} Sub-Job(s) @ {model}",
                file=sys.stderr,
            )
            return self._dispatch_delegate(args)
        return f'Unbekanntes Tool: {fn_name}'

    def _dispatch_delegate(self, args: dict) -> str:
        """Reicht bis zu MAX_PARALLEL_AGENTS Sub-Jobs ein und wartet auf Ergebnisse."""
        import threading

        tasks          = args.get('tasks', [])[:MAX_PARALLEL_AGENTS]
        model          = args.get('model', 'xiaomi')
        timeout_min    = int(args.get('timeout_minutes', 10))
        today          = datetime.now().strftime('%Y-%m-%d')

        if not tasks:
            return 'FEHLER: Keine Aufgaben angegeben'
        if model not in ('xiaomi', 'mimo-pro'):
            model = 'xiaomi'

        # ── Alle Jobs parallel einreichen ──────────────────────────
        job_ids: list[int | None] = [None] * len(tasks)

        def _submit(idx: int, task: str) -> None:
            try:
                payload = json.dumps({
                    'model':      model,
                    'prompt':     task,
                    'targetdate': today,
                }).encode()
                req = urllib.request.Request(
                    self._batch_api_url,
                    data=payload,
                    headers={
                        'X-API-Key':    self._batch_api_key,
                        'Content-Type': 'application/json',
                    },
                    method='POST',
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read())
                job_ids[idx] = data['id']
                print(
                    f"      [delegate] Sub-Job {idx+1} eingereicht → #{data['id']}",
                    file=sys.stderr,
                )
            except Exception as exc:
                print(
                    f"      [delegate] Sub-Job {idx+1} Einreichung fehlgeschlagen: {exc}",
                    file=sys.stderr,
                )

        threads = [threading.Thread(target=_submit, args=(i, t)) for i, t in enumerate(tasks)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        # ── Auf Ergebnisse warten (Polling) ────────────────────────
        deadline  = time.time() + timeout_min * 60
        pending   = {jid for jid in job_ids if jid is not None}
        results:  dict[int, dict] = {}

        while pending and time.time() < deadline:
            time.sleep(8)
            for jid in list(pending):
                try:
                    url = (
                        f"{self._batch_api_url}"
                        f"?id={jid}&full=1&apikey={self._batch_api_key}"
                    )
                    with urllib.request.urlopen(url, timeout=10) as resp:
                        data = json.loads(resp.read())
                    if data.get('status') in ('done', 'failed'):
                        results[jid] = data
                        pending.discard(jid)
                        print(
                            f"      [delegate] Sub-Job #{jid} → {data['status']}",
                            file=sys.stderr,
                        )
                except Exception as exc:
                    print(
                        f"      [delegate] Poll-Fehler #{jid}: {exc}",
                        file=sys.stderr,
                    )

        # ── Ergebnisse formatieren ─────────────────────────────────
        lines = [f'## Sub-Agenten-Ergebnisse ({len(tasks)} Jobs, Modell: {model})\n']
        for i, (task, jid) in enumerate(zip(tasks, job_ids), 1):
            short_task = task[:120] + ('…' if len(task) > 120 else '')
            lines.append(f'### Sub-Agent {i} (Job #{jid})')
            lines.append(f'**Aufgabe:** {short_task}')
            if jid is None:
                lines.append('**Status:** Einreichung fehlgeschlagen\n')
            elif jid in results:
                r      = results[jid]
                status = r.get('status', '?')
                cost   = r.get('cost_usd', '?')
                result = r.get('result') or '(kein Ergebnis)'
                lines.append(f'**Status:** {status} | **Kosten:** ${cost}\n')
                lines.append(result)
            else:
                lines.append(f'**Status:** Timeout nach {timeout_min} min\n')
            lines.append('')

        return '\n'.join(lines)

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
