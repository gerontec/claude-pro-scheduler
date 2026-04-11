"""Einstiegspunkt — claimen und verarbeiten eines einzelnen Jobs."""
import os
import sys

from .config import (
    DB_CFG, USAGE_FILE, OPENROUTER_MODELS, OPENROUTER_KEY_FILE,
)
from .context import ContextBuilder
from .notifier import Notifier
from .processor import JobProcessor
from .repository import JobRepository
from .runners import OpenRouterRunner, ClaudeCliRunner
from .tracker import UsageTracker


def _build_runners() -> dict:
    key = open(OPENROUTER_KEY_FILE).read().strip() \
        if os.path.exists(OPENROUTER_KEY_FILE) else ''
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
    context   = ContextBuilder(repo)
    runners   = _build_runners()
    processor = JobProcessor(repo, runners, context, notifier, tracker)

    try:
        processor.process(job)
    finally:
        repo.close()


if __name__ == '__main__':
    main()
