"""Движки анализа. Оба возвращают одну и ту же схему Extraction, поэтому взаимозаменяемы."""

from typing import Protocol

from ..schemas import EngineMeta, Extraction, IncomingMessage


class EngineError(RuntimeError):
    """Движок не смог разобрать обращение (нет связи, отказ модели и т. п.)."""


class Engine(Protocol):
    name: str

    def analyze(self, msg: IncomingMessage) -> tuple[Extraction, EngineMeta]: ...


def build_engine(kind: str, company: dict, model: str, effort: str) -> Engine:
    """Создаёт движок по имени. SDK Anthropic импортируется только для режима claude."""
    if kind == "claude":
        from .claude import ClaudeEngine

        return ClaudeEngine(company, model=model, effort=effort)
    from .rules import RulesEngine

    return RulesEngine(company)
