"""
Opening statistics read from a local PGN file.

The whole file is never held in memory and the whole file is never
indexed.  The anchor position is known up front, so the only positions a
repertoire can ever ask about are those within `max_depth` of it; anything
beyond that is unreachable and indexing it would cost memory for nothing.
Games stream past one at a time and only a running count survives each one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import chess
import chess.pgn

from .models import MoveStats, PositionData, position_key

EMPTY = PositionData(white=0, draws=0, black=0, moves=[])


def _fingerprint(board: chess.Board) -> tuple:
    """A cheap stand-in for `position_key`, used only to avoid rebuilding keys.

    `position_key` builds a FEN, and building a FEN scans all 64 squares one
    at a time: about 40 microseconds against 0.25 for this.  The two must
    agree on which positions are the same, so the fingerprint carries
    everything the FEN's first four fields carry apart from the en passant
    square, and adds that square only when the capture is actually legal.
    """
    return board._transposition_key() + (
        board.ep_square
        if board.ep_square is not None and board.has_legal_en_passant()
        else None,
    )


@dataclass
class PgnIndexReport:
    """What happened to the file, so a surprising tree can be explained."""

    games_read: int = 0
    games_anchored: int = 0
    games_without_anchor: int = 0
    games_malformed: int = 0
    games_unfinished: int = 0
    duplicates_removed: int = 0
    positions_indexed: int = 0
    plies_recorded: int = 0

    def summary(self) -> str:
        return (
            f"{self.games_read} read, "
            f"{self.games_anchored} reached the anchor, "
            f"{self.games_without_anchor} did not, "
            f"{self.games_malformed} malformed, "
            f"{self.duplicates_removed} duplicates, "
            f"{self.games_unfinished} unfinished, "
            f"{self.positions_indexed} distinct positions."
        )


class PgnIndex:
    """Move statistics for every position within `max_depth` of `anchor`."""

    def __init__(self, anchor: chess.Board, max_depth: int) -> None:
        self.anchor = anchor
        self.max_depth = max_depth
        self.anchor_key = position_key(anchor)
        self.anchor_fingerprint = _fingerprint(anchor)
        self.report = PgnIndexReport()

        self._positions: dict[str, PositionData] = {}
        self._moves: dict[str, dict[str, MoveStats]] = {}
        self._fingerprints: set[tuple] = set()
        self._key_cache: dict[tuple, str] = {}

    def _is_anchor(self, board: chess.Board) -> bool:
        """Whether `board` is the anchor, without building a FEN to find out.

        Most plies of most games are walked before the anchor is reached and
        can never be it, and building a FEN for each of them is the bulk of
        the time this file takes to index.  A position that is the anchor has
        to agree on piece placement, side to move, and castling rights, so a
        differing fingerprint settles the question on its own; only a
        fingerprint hit is checked against the real key.  The test stays
        exactly as strict as `position_key(board) == self.anchor_key`.
        """
        if _fingerprint(board) != self.anchor_fingerprint:
            return False

        return position_key(board) == self.anchor_key

    def _key_of(self, board: chess.Board) -> str:
        """`position_key(board)`, reusing the answer for a position seen before.

        Games walk the same opening over and over, so the same key is wanted
        again and again.  The cache holds one entry per position that was
        actually indexed, so it grows with the tree and not with the file.
        """
        fingerprint = _fingerprint(board)
        key = self._key_cache.get(fingerprint)

        if key is None:
            key = position_key(board)
            self._key_cache[fingerprint] = key

        return key

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        anchor_fen: str,
        max_depth: int,
    ) -> PgnIndex:
        index = cls(chess.Board(anchor_fen), max_depth)
        index._load(Path(path))
        return index

    def get_position(self, board: chess.Board) -> PositionData:
        """Statistics for `board`, or an empty result if it was never played."""
        return self._positions.get(position_key(board), EMPTY)

    def covers(self, max_depth: int) -> bool:
        """Whether the index window is deep enough for a builder at that depth.

        A shallower index does not fail loudly. Positions past the window
        simply look like positions with no data, and the tree truncates
        without saying why.
        """
        return self.max_depth >= max_depth

    # -------------------------------------------------------------
    # Loading
    # -------------------------------------------------------------

    def _load(self, path: Path) -> None:
        with path.open(encoding="utf-8", errors="replace") as handle:
            while True:
                try:
                    game = chess.pgn.read_game(handle)
                except Exception:
                    # A hard structural failure leaves the stream at an
                    # unknown offset, so the remaining records cannot be
                    # told apart from garbage. Stop and report rather than
                    # index whatever happens to parse next.
                    self.report.games_malformed += 1
                    break

                if game is None:
                    break

                self._index_game(game)

        self._finalize()

    def _index_game(self, game: chess.pgn.Game) -> None:
        if game.errors:
            self.report.games_malformed += 1
            return

        result = game.headers.get("Result", "*")

        # `MoveStats` has no bucket for "played, outcome unknown", so an
        # unfinished game would have to be filed as a draw and would make
        # `performance()` lie. They are counted so the loss is visible.
        if result == "*":
            self.report.games_unfinished += 1
            return

        board = game.board()
        moves = list(game.mainline_moves())
        fingerprint = self._fingerprint(board, moves)

        if fingerprint in self._fingerprints:
            self.report.duplicates_removed += 1
            return

        self._fingerprints.add(fingerprint)
        self.report.games_read += 1

        # The common case: the anchor is the game's own root, so it is
        # reached before its first move and no per-ply comparison is needed.
        anchored = self._is_anchor(board)

        if anchored:
            self.report.games_anchored += 1

        # A position counts one game once. A game that shuffles back to a
        # position it already visited is still one game, so its second
        # arrival must not add a second share to the denominator, and the
        # move it played there the first time already stands for it.
        seen: set[str] = set()
        depth = 0

        for move in moves:
            if not anchored:
                if self._is_anchor(board):
                    anchored = True
                    depth = 0
                    self.report.games_anchored += 1
                else:
                    board.push(move)
                    continue

            # Past the index window the key is never used: counting the
            # arrival and recording the move are both gated on the depth.  A
            # game that runs on into the middlegame would otherwise put every
            # position it passes through into the key cache, which grows with
            # the file rather than with the tree.  A position can only be
            # reached again later in the same game at a greater depth, so
            # dropping these keys cannot let anything be counted twice.
            if depth > self.max_depth:
                board.push(move)
                depth += 1
                continue

            key = self._key_of(board)

            if key not in seen:
                seen.add(key)
                self._attribute(key, result)

                if depth < self.max_depth:
                    self._record(key, board, move, result)

            board.push(move)
            depth += 1

        # A game that runs out of moves still reached the position it ended
        # in, so the anchor can be the last position a game reaches and the
        # check has to look there as well as before each move.
        if not anchored and self._is_anchor(board):
            anchored = True
            self.report.games_anchored += 1

        # Counting that arrival is what makes a position's total the number
        # of games that reached it, which is the denominator a share divides
        # by.
        if anchored and depth <= self.max_depth:
            key = self._key_of(board)

            if key not in seen:
                self._attribute(key, result)

        if not anchored:
            self.report.games_without_anchor += 1

    @staticmethod
    def _fingerprint(
        board: chess.Board,
        moves: list[chess.Move],
    ) -> tuple:
        return (
            position_key(board),
            tuple(move.uci() for move in moves),
        )

    def _position(self, key: str) -> PositionData:
        position = self._positions.get(key)

        if position is None:
            position = PositionData(white=0, draws=0, black=0, moves=[])
            self._positions[key] = position
            self._moves[key] = {}

        return position

    def _attribute(self, key: str, result: str) -> None:
        """Count one game's arrival at a position in that position's total."""
        position = self._position(key)

        if result == "1-0":
            position.white += 1
        elif result == "0-1":
            position.black += 1
        else:
            position.draws += 1

    def _record(
        self,
        key: str,
        board: chess.Board,
        move: chess.Move,
        result: str,
    ) -> None:
        self._position(key)

        uci = move.uci()
        table = self._moves[key]
        stats = table.get(uci)

        if stats is None:
            stats = MoveStats(uci=uci, san=board.san(move), white=0, draws=0, black=0)
            table[uci] = stats

        if result == "1-0":
            stats.white += 1
        elif result == "0-1":
            stats.black += 1
        else:
            stats.draws += 1

        self.report.plies_recorded += 1

    def _finalize(self) -> None:
        for key, table in self._moves.items():
            self._positions[key].moves = sorted(
                table.values(),
                key=lambda stats: stats.total,
                reverse=True,
            )

        self.report.positions_indexed = len(self._positions)
