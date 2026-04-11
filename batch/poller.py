"""Einstiegspunkt — claimen und verarbeiten eines einzelnen Jobs."""
import sys

from .config import (
    DB_CFG, USAGE_FILE, OPENROUTER_MODELS, load_openrouter_key,
)
from .context import ContextBuilder
from .context_repo import ContextRepository
from .notifier import Notifier
from .processor import JobProcessor
from .repository import JobRepository
from .runners import OpenRouterRunner, ClaudeCliRunner
from .tracker import UsageTracker


def _build_runners() -> dict:
    key = load_openrouter_key()
    return {
        'qwen-free': OpenRouterRunner(OPENROUTER_MODELS['qwen-free'], key),
        'xiaomi':    OpenRouterRunner(OPENROUTER_MODELS['xiaomi'],    key),
        'mimo-pro':  OpenRouterRunner(OPENROUTER_MODELS['mimo-pro'],  key),
        'sonnet':    ClaudeCliRunner('sonnet'),
        'opus':      ClaudeCliRunner('opus'),
    }


def main():
    repo      = JobRepository()
    job       = repo.claim_next()
    if job is None:
        repo.close()
        return

    notifier  = Notifier()
    tracker   = UsageTracker(USAGE_FILE)
    context   = ContextBuilder(ContextRepository())
    runners   = _build_runners()
    processor = JobProcessor(repo, runners, context, notifier, tracker)

    try:
        processor.process(job)
    finally:
        repo.close()


if __name__ == '__main__':
    main()
