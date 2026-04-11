"""Claude CLI Runner (sonnet, opus) — spawnt claude binary als Subprocess."""
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from typing import Callable

from ..config import CLAUDE_BIN
from ..models import RunResult
from .base import ModelRunner

TIMEOUT_SEC = 4 * 3600  # 4 Stunden


class ClaudeCliRunner(ModelRunner):
    def __init__(self, model: str):
        self.model = model

    def run(
        self,
        prompt: str,
        system_prompt: str,
        job_id: int,
        on_kill_check: Callable[[], bool],
    ) -> RunResult:
        with tempfile.NamedTemporaryFile(
            prefix=f'claude_pro_{job_id}_', suffix='.json', delete=False
        ) as tmp:
            tmp_path = tmp.name

        try:
            return self._run_process(
                prompt, system_prompt, job_id, tmp_path, on_kill_check
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _run_process(
        self,
        prompt: str,
        system_prompt: str,
        job_id: int,
        tmp_path: str,
        on_kill_check: Callable[[], bool],
    ) -> RunResult:
        # ANTHROPIC_BASE_URL/AUTH_TOKEN entfernen → echte Claude API
        clean_env = {
            k: v for k, v in os.environ.items()
            if k not in ('ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN')
        }
        timed_out = False
        killed    = False

        with open(tmp_path, 'w') as out_f:
            proc = subprocess.Popen(
                [
                    CLAUDE_BIN,
                    '--model', self.model,
                    '--effort', 'low',
                    '--max-budget-usd', '0.25',
                    '--dangerously-skip-permissions',
                    '--append-system-prompt', system_prompt,
                    '--output-format', 'json',
                    '-p', prompt,
                ],
                stdin=subprocess.DEVNULL,
                stdout=out_f,
                stderr=subprocess.STDOUT,
                env=clean_env,
            )
            deadline = time.time() + TIMEOUT_SEC
            while proc.poll() is None:
                if time.time() > deadline:
                    proc.kill()
                    timed_out = True
                    break
                if on_kill_check():
                    proc.kill()
                    killed = True
                    break
                time.sleep(10)
            exit_code = proc.returncode if proc.returncode is not None else -1

        raw = open(tmp_path).read()

        if killed:
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id}: Prozess gekillt (UI)",
                file=sys.stderr,
            )
            return RunResult(
                result='', status='failed', error='Killed by user',
                in_tok=0, out_tok=0, cache_tok=0, cost=0.0,
            )

        if timed_out:
            return RunResult(
                result=f'Timeout: Job lief länger als {TIMEOUT_SEC // 3600} Stunden.',
                status='failed',
                error=f'Timeout nach {TIMEOUT_SEC // 3600}h',
                in_tok=0, out_tok=0, cache_tok=0, cost=0.0,
            )

        if exit_code != 0:
            first_line = raw.splitlines()[0] if raw.strip() else '(keine Ausgabe)'
            return RunResult(
                result=raw, status='failed',
                error=f'Exit-Code {exit_code}: {first_line}',
                in_tok=0, out_tok=0, cache_tok=0, cost=0.0,
            )

        try:
            d         = json.loads(raw)
            u         = d.get('usage', {})
            result    = d.get('result', '')
            in_tok    = u.get('input_tokens', 0)
            out_tok   = u.get('output_tokens', 0)
            cache_tok = (
                u.get('cache_creation_input_tokens', 0)
                + u.get('cache_read_input_tokens', 0)
            )
            cost = round(d.get('total_cost_usd', 0.0), 6)
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id}: "
                f"Claude CLI OK ({in_tok}/{out_tok} tok, ${cost})",
                file=sys.stderr,
            )
            return RunResult(
                result=result, status='done', error='',
                in_tok=in_tok, out_tok=out_tok,
                cache_tok=cache_tok, cost=cost,
            )
        except json.JSONDecodeError as e:
            first_line = raw.splitlines()[0] if raw.strip() else '(keine Ausgabe)'
            return RunResult(
                result=raw, status='failed',
                error=f'Kein gültiges JSON (Exit 0): {first_line} — {e}',
                in_tok=0, out_tok=0, cache_tok=0, cost=0.0,
            )
