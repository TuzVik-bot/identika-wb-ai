from __future__ import annotations

from abc import ABC, abstractmethod

from identika.config import EffectiveSettings
from identika.models import CreateJobRequest, GenerationResult


class AiProvider(ABC):
    name: str

    @abstractmethod
    async def generate(
        self,
        request: CreateJobRequest,
        eff: EffectiveSettings | None = None,
    ) -> GenerationResult:
        raise NotImplementedError
