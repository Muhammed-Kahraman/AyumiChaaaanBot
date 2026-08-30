"""Small persistent OpenAI usage ledger with a hard spending cutoff."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass


class BudgetExceeded(RuntimeError):
    """Raised before an API call once the configured budget is exhausted."""


@dataclass(frozen=True)
class UsageCost:
    input_tokens: int = 0
    output_tokens: int = 0


_LOCK = threading.Lock()

# USD per one million tokens. Keep this explicit so the ledger is auditable.
_PRICES = {
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-4o-mini-transcribe": (1.25, 5.00),
}


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: str, value: dict) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(temporary, path)


def token_cost(model: str, usage: UsageCost) -> float:
    input_price, output_price = _PRICES.get(model, (0.0, 0.0))
    return (
        usage.input_tokens * input_price + usage.output_tokens * output_price
    ) / 1_000_000


class CostTracker:
    def __init__(self, path: str, limit_usd: float) -> None:
        self.path = path
        self.limit_usd = limit_usd

    def ensure_available(self) -> None:
        with _LOCK:
            state = _read(self.path)
            if float(state.get("spent_usd", 0.0)) >= self.limit_usd:
                raise BudgetExceeded(
                    f"OpenAI bütçesi doldu: ${self.limit_usd:.2f}"
                )

    def record(self, model: str, usage: UsageCost, operation: str) -> float:
        cost = token_cost(model, usage)
        with _LOCK:
            state = _read(self.path)
            state["spent_usd"] = round(
                float(state.get("spent_usd", 0.0)) + cost, 8
            )
            state["limit_usd"] = self.limit_usd
            state["last_operation"] = operation
            state["last_model"] = model
            state["last_usage"] = {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cost_usd": cost,
            }
            _write(self.path, state)
        return cost
