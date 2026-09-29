"""Mistake detection against a real PGN source and a real engine.

These are the only tests that prove the two halves of the feature meet:
positions indexed from a file, evaluated by Stockfish, then punished.
Everything here needs a real Stockfish binary and is skipped, not failed,
when one is not available.

Two traps this file has to avoid:

*  Repeating a line does not create more games. The index collapses games
   with an identical mainline, so ten copies of one line are one game.
   Every fixture below lists genuinely distinct games.

*  The anchor here is White to move, so a game's first SAN is White's move
   and its second is Black's. Asking a White-to-move position for Black's
   mistakes is not possible at all: the builder returns immediately.
"""

import sys
from pathlib import Path

import chess
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from pgn_fixtures import pgn_text, write_pgn

from lichess_opening_explorer.explorer import RepertoireBuilder
from lichess_opening_explorer.pgn_index import PgnIndex
from lichess_opening_explorer.sources import PgnDataSource

# Nimzo-Indian, 1.d4 d5 2.c4 e6 3.Nc3 Nf6 4.Bg5 Bb4 -- White to move.
NIMZO_FEN = (
    "rnbqk2r/ppp2ppp/4pn2/3p2B1/"
    "1bPP4/2N5/PP2PPPP/R2QKBNR w KQkq - 4 5"
)


def build_source(tmp_path, games, max_depth=6):
    path = write_pgn(
        tmp_path / "games.pgn", pgn_text(games, start_fen=NIMZO_FEN)
    )
    index = PgnIndex.from_path(path, NIMZO_FEN, max_depth)

    assert index.report.games_anchored == len(games), (
        "fixtures must be distinct games, not repeats of one line"
    )

    return PgnDataSource(index)


def test_a_playable_move_nobody_played_is_never_a_deviation(tmp_path):
    """
    The source is the only thing that nominates moves.

    `e4` and `Nf3` are perfectly legal in this position and Stockfish
    would happily evaluate either, but nobody in this database played
    them, so they must not become repertoire lines or steal the MAIN
    label from `e3`.
    """
    games = [
        ["e3", "Nc6"],
        ["e3", "Nc6", "Nf3"],
        ["e3", "Nc6", "a3"],
        ["e3", "Nc6", "e4"],
    ]

    try:
        builder = RepertoireBuilder(
            source=build_source(tmp_path, games),
            color=chess.WHITE,
            stockfish_depth=10,
            min_games=1,
            threshold=0.1,
            max_depth=1,
            max_moves=3,
            max_mistakes=1,
        )
    except RuntimeError as exc:
        pytest.skip(f"Stockfish is not available: {exc}")

    try:
        nodes = builder.build(NIMZO_FEN)
    finally:
        builder.close()

    assert [node.san for node in nodes] == ["e3"]


def test_a_played_but_bad_move_is_classified_and_punished(tmp_path):
    """
    The engine must be able to call a real played move a mistake.

    Every game here answers 5.a3 with 5...c5, which walks straight into
    6.axb4 and gives Black's bishop away. The builder is asked about the
    position *after* White's move, because that is the only place the
    opponent can be to move.
    """
    games = [
        ["a3", "c5"],
        ["a3", "c5", "e4"],
        ["a3", "c5", "Nf3"],
        ["a3", "c5", "e4", "Nc6"],
    ]

    try:
        builder = RepertoireBuilder(
            source=build_source(tmp_path, games),
            color=chess.WHITE,
            stockfish_depth=10,
            min_games=1,
            threshold=0.1,
            max_depth=1,
            max_moves=3,
            max_mistakes=1,
        )
    except RuntimeError as exc:
        pytest.skip(f"Stockfish is not available: {exc}")

    board = chess.Board(NIMZO_FEN)
    board.push_san("a3")

    assert board.turn == chess.BLACK

    try:
        mistakes = builder._find_opponent_mistakes(board)
    finally:
        builder.close()

    assert mistakes, "a move that hangs a bishop must be flagged"

    # The dictionary describes the move by SAN under "move", and carries the
    # punishment it implies in the comment.
    assert mistakes[0]["move"] == "c5"
    assert float(mistakes[0]["difference"]) > 0
    assert "Punishment:" in str(mistakes[0]["comment"])
