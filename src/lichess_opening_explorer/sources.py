"""
Where opening statistics come from.

The repertoire builder only ever asks one question: *which moves were
actually played in this position, and how often?*  The answer arrives as a
`PositionData`, whatever produced it.  Keeping the question this narrow is
what lets a local PGN replace the Lichess explorer without any change to
the selection, mistake detection, or export logic.
"""

from __future__ import annotations

from typing import Protocol

import chess

from .api import LichessClient
from .models import PositionData, position_key
from .pgn_index import PgnIndex, PgnIndexReport


class DataSource(Protocol):
    """Opening statistics for positions, from any origin."""

    def get_position(self, board: chess.Board) -> PositionData:
        """Statistics for the position `board` is currently in.

        `PositionData.total` MUST be the number of games that reached this
        position, and every `MoveStats.total` MUST be the number of those
        games that played that move.  Move frequency is computed as
        `MoveStats.total / PositionData.total`, so a source that reports
        anything else makes every frequency in the tree wrong.

        An empty `PositionData` for an unknown position is valid and means
        "no data here".  `moves` MUST be ordered by `total` descending,
        because the builder keeps the first qualifying deviations and the
        cap is meant to keep the moves a real opponent plays most often.
        """
        ...


class LichessDataSource:
    """Opening statistics from the Lichess Masters explorer."""

    def __init__(self, client: LichessClient | None = None) -> None:
        self._client = client if client is not None else LichessClient()
        self._owns_client = client is None
        self._cache: dict[str, PositionData] = {}

    def get_position(self, board: chess.Board) -> PositionData:
        key = position_key(board)

        if key not in self._cache:
            self._cache[key] = self._client.get_position(fen=key)

        return self._cache[key]

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class PgnDataSource:
    """Opening statistics from a prebuilt local PGN index.

    The index does the expensive work once, at load time. Answering a
    position is then a dictionary lookup, which is what lets the same
    builder run against a local file or the network without caring.
    """

    def __init__(self, index: PgnIndex) -> None:
        self._index = index
        self.max_depth = index.max_depth

    @property
    def report(self) -> PgnIndexReport:
        return self._index.report

    def get_position(self, board: chess.Board) -> PositionData:
        return self._index.get_position(board)

    def check_depth(self, required: int) -> None:
        """Refuse to run against an index that cannot answer what is asked.

        Without this, a builder configured for a deeper tree than the index
        was built for would silently produce a truncated repertoire.
        """
        if not self._index.covers(required):
            raise ValueError(
                f"PGN index covers {self._index.max_depth} plies but "
                f"{required} were requested. Rebuild the index with "
                f"max_depth={required}."
            )

    def close(self) -> None:
        """Present so both sources are interchangeable to the caller."""
