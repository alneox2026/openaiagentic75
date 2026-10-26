"""Assemble streamed OpenAI text and preserve raw usage for the worker."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TurnAssembler:
    """Collect response text without pricing or retaining provider event history."""

    text_fragments: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    _text_length: int = field(default=0, init=False, repr=False)

    def add_text(self, value: str) -> str:
        if not value:
            return ""

        # The Agents API normally emits deltas. A terminal `output_text.done`
        # may repeat the entire response, so deduplicate it without holding a
        # second full copy of the response in memory.
        if len(value) >= self._text_length:
            current_text = "".join(self.text_fragments)
            if value.startswith(current_text):
                delta = value[len(current_text) :]
            else:
                delta = value[self._suffix_prefix_overlap(current_text, value) :]
        else:
            tail = self._tail(len(value))
            delta = value[self._suffix_prefix_overlap(tail, value) :]

        if delta:
            self.text_fragments.append(delta)
            self._text_length += len(delta)
        return delta

    def add_event(self, event: dict[str, Any]) -> None:
        # The gateway intentionally forwards reports unchanged. The worker
        # owns usage hydration, aggregation, pricing, ledger writes, and settle.
        if event.get("type") != "middleware.usage.raw":
            return
        usage = event.get("usage")
        if isinstance(usage, dict):
            self.usage = usage

    def reply_text(self) -> str:
        return "".join(self.text_fragments).strip()

    def _tail(self, count: int) -> str:
        remaining = count
        parts: list[str] = []
        for fragment in reversed(self.text_fragments):
            if remaining <= 0:
                break
            part = fragment[-remaining:]
            parts.append(part)
            remaining -= len(part)
        return "".join(reversed(parts))

    @staticmethod
    def _suffix_prefix_overlap(current_text: str, next_value: str) -> int:
        max_overlap = min(len(current_text), len(next_value))
        for overlap in range(max_overlap, 0, -1):
            if current_text[-overlap:] == next_value[:overlap]:
                return overlap
        return 0
