from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MoveStats:
    uci: str
    san: str
    white: int
    draws: int
    black: int

    @property
    def total(self) -> int:
        return self.white + self.draws + self.black

    def performance(self) -> float:
        """Performance score from White's perspective: win% + 0.5 * draw%."""
        if self.total == 0:
            return 0.0
        return (self.white + 0.5 * self.draws) / self.total


@dataclass
class PositionData:
    white: int
    draws: int
    black: int
    moves: list[MoveStats]

    @property
    def total(self) -> int:
        return self.white + self.draws + self.black


@dataclass
class MoveNode:
    san: str
    uci: str
    children: list[MoveNode] = field(default_factory=list)
    comment: str = ""
