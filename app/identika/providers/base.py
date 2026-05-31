from __future__ import annotations

from abc import ABC, abstractmethod

from identika.models import CreateJobRequest, GenerationResult


class AiProvider(ABC):
    name: str

    @abstractmethod
    async def generate(self, request: CreateJobRequest) -> GenerationResult:
        raise NotImplementedError
