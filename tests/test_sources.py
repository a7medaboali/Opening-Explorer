"""
The contract every data source has to honour.

These run the same assertions against both sources on purpose. A source
that reported some other meaning for `PositionData.total` would make every
frequency in the tree wrong without raising anything, so the meaning is
pinned here rather than assumed.
"""

import chess
import pytest
from pgn_fixtures import pgn_text, write_pgn

from lichess_opening_explorer.models import PositionData, position_key
from lichess_opening_explorer.pgn_index import PgnIndex
from lichess_opening_explorer.sources import LichessDataSource, PgnDataSource

NIMZO_FEN = (
    "rnbqk2r/ppp2ppp/4pn2/3p2B1/"
    "1bPP4/2N5/PP2PPPP/R2QKBNR w KQkq - 4 5"
)

# Five distinct games that all start at the anchor, so the anchor's total is
# five. Repeating one line would not do: identical games are duplicates.
FIVE_GAMES = [
    ["e3", "Nc6"],
    ["e3", "e5"],
    ["e3", "c5"],
    ["a3", "Nc6"],
    ["a3", "c5"],
]


def nimzo_index(tmp_path, games, max_depth=8):
    path = write_pgn(tmp_path / "games.pgn", pgn_text(games, start_fen=NIMZO_FEN))
    return PgnIndex.from_path(path, NIMZO_FEN, max_depth)


def lichess_source(total):
    """A stub that knows the anchor and nothing else, as a real client would."""

    class StubClient:
        def get_position(self, fen):
            if fen == position_key(chess.Board(NIMZO_FEN)):
                return PositionData(total, 0, 0, [])

            return PositionData(0, 0, 0, [])

        def close(self):
            pass

    return LichessDataSource(StubClient())


@pytest.fixture(params=["lichess", "pgn"])
def source(request, tmp_path):
    if request.param == "lichess":
        return lichess_source(5)

    return PgnDataSource(nimzo_index(tmp_path, FIVE_GAMES))


def test_returns_position_data(source):
    assert isinstance(source.get_position(chess.Board(NIMZO_FEN)), PositionData)


def test_total_is_the_number_of_games_reaching_the_position(source):
    assert source.get_position(chess.Board(NIMZO_FEN)).total == 5


def test_unknown_position_returns_an_empty_result(source):
    """A position nobody played is not an error, it is simply empty."""
    elsewhere = chess.Board(NIMZO_FEN)
    elsewhere.push_san("Nf3")
    elsewhere.push_san("O-O")

    data = source.get_position(elsewhere)

    assert data.total == 0
    assert data.moves == []


def test_move_totals_never_exceed_the_position_total(source):
    board = chess.Board(NIMZO_FEN)
    data = source.get_position(board)

    for move in data.moves:
        assert move.total <= data.total


def test_move_ucis_are_always_legal_in_the_position(source):
    board = chess.Board(NIMZO_FEN)
    data = source.get_position(board)

    for move in data.moves:
        assert board.parse_uci(move.uci) in board.legal_moves


# --- PGN specific -------------------------------------------------


def test_pgn_source_reports_its_index(tmp_path):
    source = PgnDataSource(nimzo_index(tmp_path, [["e3", "Nc6"], ["a3", "c5"]]))

    assert source.report.games_anchored == 2
    assert "2 reached the anchor" in source.report.summary()


def test_check_depth_rejects_a_too_shallow_index(tmp_path):
    source = PgnDataSource(nimzo_index(tmp_path, [["e3"]], max_depth=3))

    source.check_depth(3)

    with pytest.raises(ValueError, match="index covers 3 plies"):
        source.check_depth(4)


def test_pgn_source_survives_a_terminal_anchor(tmp_path):
    """A finished position has no legal moves, so the tree is simply empty."""
    mate = chess.Board()
    mate.push_san("f3")
    mate.push_san("e5")
    mate.push_san("g4")

    path = write_pgn(tmp_path / "short.pgn", pgn_text([["e3"]], start_fen=NIMZO_FEN))
    index = PgnIndex.from_path(path, mate.fen(), 8)
    source = PgnDataSource(index)

    assert source.get_position(mate).total == 0


def test_pgn_source_close_is_harmless(tmp_path):
    PgnDataSource(nimzo_index(tmp_path, [["e3"]])).close()
