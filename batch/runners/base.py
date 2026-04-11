"""Abstrakte Basisklasse für alle Model-Runner."""
from abc import ABC, abstractmethod
from typing import Callable

from ..models import RunResult


class ModelRunner(ABC):
    @abstractmethod
    def run(
        self,
        prompt: str,
        system_prompt: str,
        job_id: int,
        on_kill_check: Callable[[], bool],
    ) -> RunResult:
        """
        Führt den Job aus und gibt RunResult zurück.
        on_kill_check() → True bedeutet: Job wurde via UI abgebrochen.
        """
        ...
