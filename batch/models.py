"""Datenklassen — JobRecord und RunResult."""
from dataclasses import dataclass, field
from datetime import date
from typing import Literal


@dataclass
class JobRecord:
    id: int
    model: str
    prompt: str
    targetdate: date
    resume_session: bool


@dataclass
class RunResult:
    result: str
    status: Literal['done', 'failed']
    in_tok: int
    out_tok: int
    cache_tok: int
    cost: float
    error: str = ''
    iters: int = 1
