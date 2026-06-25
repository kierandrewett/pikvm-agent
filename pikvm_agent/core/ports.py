"""Owned interfaces (ports).

These Protocols are the boundary between our runtime and the libraries it uses.
Adapters implement them; the runtime depends only on these, never on a library's
concrete type. This is what keeps OmniParser / PaddleOCR / OpenRouter as bounded
evidence-producers rather than owners of control flow.

Adapters:
    ComputerBackend        -> PiKVMBackend
    OCRProvider            -> PaddleOCRProvider | PiKVMOcrProvider (default)
    ScreenElementProvider  -> OmniParserProvider | NullElementProvider
    OperatorProvider       -> OpenRouterOperator | FakeOperator
    TransactionExecutor    -> GuardedTransactionExecutor
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from pikvm_agent.core.models import (
    CapturedFrame,
    ElementMap,
    GuardedTransaction,
    OCRResult,
    OperatorDecision,
    OperatorRequest,
    Region,
    TransactionResult,
)


@runtime_checkable
class ComputerBackend(Protocol):
    """The only thing that touches PiKVM. Deals in pixels and raw HID."""

    async def screenshot(self, region: Region | None = None) -> CapturedFrame: ...
    async def keypress(self, keys: list[str]) -> None: ...
    async def type_text(self, text: str, *, code: bool = False, secret: bool = False) -> None: ...
    async def print_text(self, text: str) -> None: ...
    async def click(self, x: int, y: int, button: str = "left") -> None: ...
    async def move_mouse(self, x: int, y: int) -> None: ...
    async def scroll(self, dx: int = 0, dy: int = 0) -> None: ...


@runtime_checkable
class OCRProvider(Protocol):
    async def ocr(self, image_path: Path, region: Region | None = None) -> OCRResult: ...


@runtime_checkable
class ScreenElementProvider(Protocol):
    async def parse_elements(
        self, image_path: Path, frame_id: int, world_version: int
    ) -> ElementMap: ...


@runtime_checkable
class OperatorProvider(Protocol):
    async def decide(self, request: OperatorRequest) -> OperatorDecision: ...


@runtime_checkable
class TransactionExecutor(Protocol):
    async def execute(self, tx: GuardedTransaction) -> TransactionResult: ...
