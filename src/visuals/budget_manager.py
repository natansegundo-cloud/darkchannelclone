"""Controle simples e determinístico do orçamento de geração visual."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


def _money(value: Decimal | float | int | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.0001"))


@dataclass(frozen=True)
class BudgetEntry:
    scene_id: str
    tier: str
    attempt: int
    amount_usd: Decimal


class BudgetManager:
    """Reserva custo por tentativa e impede que o lote ultrapasse o teto."""

    def __init__(self, limit_usd: Decimal | float | int | str) -> None:
        self.limit_usd = _money(limit_usd)
        if self.limit_usd < 0:
            raise ValueError("budget must be non-negative")
        self._entries: list[BudgetEntry] = []

    @property
    def spent_usd(self) -> Decimal:
        return sum((entry.amount_usd for entry in self._entries), Decimal("0"))

    @property
    def remaining_usd(self) -> Decimal:
        return self.limit_usd - self.spent_usd

    def can_afford(self, amount_usd: Decimal | float | int | str) -> bool:
        return _money(amount_usd) <= self.remaining_usd

    def charge(
        self,
        *,
        scene_id: str,
        tier: str,
        attempt: int,
        amount_usd: Decimal | float | int | str,
    ) -> BudgetEntry:
        amount = _money(amount_usd)
        if not self.can_afford(amount):
            raise ValueError(
                f"budget exceeded: required ${amount}, remaining ${self.remaining_usd}"
            )
        entry = BudgetEntry(scene_id, tier, attempt, amount)
        self._entries.append(entry)
        return entry

    def settle(self, entry: BudgetEntry, amount_usd: Decimal | float | int | str) -> BudgetEntry:
        """Troca a reserva estimada pelo custo reportado pelo provider."""

        try:
            index = self._entries.index(entry)
        except ValueError as exc:
            raise ValueError("budget entry is not registered") from exc
        settled = BudgetEntry(
            entry.scene_id,
            entry.tier,
            entry.attempt,
            _money(amount_usd),
        )
        entries = list(self._entries)
        entries[index] = settled
        self._entries = entries
        return settled

    def seed(
        self,
        *,
        scene_id: str,
        tier: str,
        attempt: int,
        amount_usd: Decimal | float | int | str,
    ) -> BudgetEntry:
        """Reconcilia custos já registrados ao abrir uma operação de revisão."""

        entry = BudgetEntry(scene_id, tier, attempt, _money(amount_usd))
        self._entries.append(entry)
        return entry

    def snapshot(self) -> dict[str, Any]:
        return {
            "limit_usd": float(self.limit_usd),
            "spent_usd": float(self.spent_usd),
            "remaining_usd": float(self.remaining_usd),
            "entries": [
                {
                    "scene_id": entry.scene_id,
                    "tier": entry.tier,
                    "attempt": entry.attempt,
                    "amount_usd": float(entry.amount_usd),
                }
                for entry in self._entries
            ],
        }
