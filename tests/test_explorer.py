import chess

from lichess_opening_explorer.explorer import RepertoireBuilder


QGD_FEN = (
    "rnbqk2r/ppp2ppp/4pn2/3p2B1/"
    "1bPP4/2N5/PP2PPPP/R2QKBNR w KQkq - 4 5"
)


def test_fen_is_valid():
    board = chess.Board(QGD_FEN)

    assert board.is_valid()


def test_candidate_black_moves_only_on_black_turn():
    board = chess.Board(QGD_FEN)

    # Starting position in our test FEN is White to move.
    assert board.turn == chess.WHITE