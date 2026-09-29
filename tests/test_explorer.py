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

    assert board.turn == chess.WHITE


def test_mistake_classification():
    builder = RepertoireBuilder.__new__(RepertoireBuilder)
    builder.mistake_threshold = 1.0

    assert builder._classify_mistake(0.5) is None
    assert builder._classify_mistake(1.0) == "MISTAKE"
    assert builder._classify_mistake(1.5) == "SERIOUS MISTAKE"
    assert builder._classify_mistake(3.0) == "BLUNDER"

def test_mistake_comment_extracts_first_real_move():
    builder = RepertoireBuilder.__new__(RepertoireBuilder)

    result = {
        "move": "Nxd5",
        "before": 0.20,
        "after": -3.30,
        "difference": 3.50,
        "best_line": "6. Qa4+! Qd7 7. Bxd8",
    }

    comment = builder._make_mistake_comment(
        result,
        "BLUNDER",
    )

    assert comment == (
        "BLUNDER: ...Nxd5?!\n"
        "Evaluation: +0.20 -> -3.30\n"
        "Swing: +3.50\n"
        "Best punishment: Qa4+!"
    )


from lichess_opening_explorer.models import MoveNode
from lichess_opening_explorer.pgn_export import _add_nodes

def test_pgn_comments_are_attached_to_moves():
    game = chess.pgn.Game()
    board = chess.Board()

    nodes = [
        MoveNode(
            san="e4",
            uci="e2e4",
            comment="MISTAKE",
            children=[
                MoveNode(
                    san="e5",
                    uci="e7e5",
                    comment="PUNISHMENT",
                )
            ],
        )
    ]

    _add_nodes(game, board, nodes)

    variation = game.variations[0]

    assert variation.move == chess.Move.from_uci("e2e4")
    assert variation.comment == "MISTAKE"

    punishment = variation.variations[0]

    assert punishment.move == chess.Move.from_uci("e7e5")
    assert punishment.comment == "PUNISHMENT"