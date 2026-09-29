import chess
import chess.pgn

from lichess_opening_explorer.explorer import RepertoireBuilder
from lichess_opening_explorer.models import MoveNode, MoveStats
from lichess_opening_explorer.pgn_export import _add_nodes, build_pgn


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


def test_build_pgn_writes_valid_file(tmp_path):
    output_path = tmp_path / "test.pgn"

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

    build_pgn(
        chess.STARTING_FEN,
        nodes,
        str(output_path),
    )

    assert output_path.exists()

    with output_path.open() as f:
        game = chess.pgn.read_game(f)

    assert game is not None
    assert game.headers["Event"] == "Opening Repertoire"

    first_move = game.next()

    assert first_move is not None
    assert first_move.move == chess.Move.from_uci("e2e4")
    assert first_move.comment == "MISTAKE"

    punishment = first_move.next()

    assert punishment is not None
    assert punishment.move == chess.Move.from_uci("e7e5")
    assert punishment.comment == "PUNISHMENT"


def test_candidate_black_moves_include_master_moves_and_forcing_moves(
    monkeypatch,
):
    builder = RepertoireBuilder.__new__(RepertoireBuilder)
    builder.max_deviation_moves = 5

    board = chess.Board()
    board.push_san("e4")
    board.push_san("e5")
    board.push_san("Nf3")

    fake_data = type(
        "FakePositionData",
        (),
        {
            "moves": [
                MoveStats(
                    uci=board.parse_san("Nc6").uci(),
                    san="Nc6",
                    white=10,
                    draws=5,
                    black=20,
                )
            ]
        },
    )()

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: fake_data,
    )

    candidates = builder._candidate_black_moves(board)

    assert board.turn == chess.BLACK
    assert chess.Move.from_uci("b8c6") in candidates

    assert all(board.is_legal(move) for move in candidates)
    assert len(candidates) == len(set(candidates))


def test_candidate_black_moves_include_forcing_moves(monkeypatch):
    builder = RepertoireBuilder.__new__(RepertoireBuilder)
    builder.max_deviation_moves = 5

    board = chess.Board(
        "rnbqkbnr/pppp1ppp/8/4p3/"
        "4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 2"
    )

    fake_data = type(
        "FakePositionData",
        (),
        {"moves": []},
    )()

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: fake_data,
    )

    candidates = builder._candidate_black_moves(board)

    assert all(board.is_legal(move) for move in candidates)

    assert all(
        board.is_capture(move) or board.gives_check(move)
        for move in candidates
    )