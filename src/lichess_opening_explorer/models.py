from __future__ import annotations

from dataclasses import dataclass, field

import chess


def position_key(board: chess.Board) -> str:
    """Identity of a position, for caches, APIs, and transposition merging.

    Only the first four FEN fields are kept.  The halfmove clock and the
    fullmove number say nothing about the position, so including them
    splits transpositions of one position across several cache entries
    and asks a data source about the same position more than once.

    The en passant field uses python-chess's "legal" rule, which records
    the square only when a capture is actually available.  The "fen" rule
    always records the square behind a double pawn push, so the same
    position reached by a different move order gets two different keys
    and misses the cache.
    """
    return " ".join(board.fen(en_passant="legal").split(" ")[:4])


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
    """Statistics for one position, as the moves actually played in it.

    `total` is the number of games that reached this position, and it is
    the denominator for every move frequency derived from `moves`. It is
    deliberately *not* the size of the database or the size of the
    ancestor position, so a line that is rare at its parent stays rare
    here rather than being renormalised to look common.

    `moves` must be ordered by `MoveStats.total` descending: the builder's
    deviation selection relies on it.
    """

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
