import sys

import chess
import chess.pgn
import pytest

from lichess_opening_explorer.explorer import RepertoireBuilder
from lichess_opening_explorer.models import MoveNode, MoveStats
from lichess_opening_explorer.pgn_export import _add_nodes, build_pgn
from lichess_opening_explorer.api import LichessClient
from lichess_opening_explorer import cli


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

def test_find_black_mistakes_classifies_and_sorts(monkeypatch):
    builder = RepertoireBuilder.__new__(RepertoireBuilder)

    builder.mistake_threshold = 1.0
    builder._punishment_cache = {}

    board = chess.Board()

    # Make it Black's turn.
    board.push_san("e4")

    moves = [
        board.parse_san("e5"),
        board.parse_san("Nc6"),
        board.parse_san("d5"),
        board.parse_san("Nf6"),
    ]

    monkeypatch.setattr(
        builder,
        "_candidate_black_moves",
        lambda board: moves,
    )

    differences = {
        "e7e5": 1.2,
        "b8c6": 3.5,
        "d7d5": 0.5,
        "g8f6": 2.0,
    }

    def fake_test_black_move(board, san):
        move = board.parse_san(san)

        return {
            "move": san,
            "before": 0.0,
            "after": -differences[move.uci()],
            "difference": differences[move.uci()],
            "best_line": "2. Qa4+!",
        }

    monkeypatch.setattr(
        builder,
        "test_black_move",
        fake_test_black_move,
    )

    monkeypatch.setattr(
        builder,
        "_make_mistake_comment",
        lambda result, classification: classification,
    )

    mistakes = builder._find_black_mistakes(board)

    assert len(mistakes) == 3

    assert [result["difference"] for result in mistakes] == [
        3.5,
        2.0,
        1.2,
    ]

    assert [result["classification"] for result in mistakes] == [
        "BLUNDER",
        "SERIOUS MISTAKE",
        "MISTAKE",
    ]

    assert all("comment" in result for result in mistakes)    

def test_find_black_mistakes_returns_empty_on_white_turn(monkeypatch):
    builder = RepertoireBuilder.__new__(RepertoireBuilder)

    board = chess.Board()

    def should_not_be_called(board):
        raise AssertionError(
            "_candidate_black_moves should not be called on White's turn"
        )

    monkeypatch.setattr(
        builder,
        "_candidate_black_moves",
        should_not_be_called,
    )

    mistakes = builder._find_black_mistakes(board)

    assert mistakes == []

def test_real_stockfish_can_analyse_position():
    fake_client = object()

    builder = RepertoireBuilder(
        client=fake_client,
        color=chess.WHITE,
        stockfish_time=0.05,
    )

    board = chess.Board(QGD_FEN)

    result = builder.analyse_position(board)

    assert result is not None
    assert "score" in result
    assert "pv" in result

    builder.engine.quit()


def test_black_move_calculates_evaluation_swing(monkeypatch):
    builder = RepertoireBuilder.__new__(RepertoireBuilder)
    builder.punishment_depth = 6

    board = chess.Board()
    board.push_san("e4")

    analysis_results = [
        {
            "score": 1.20,
            "pv": [],
        },
        {
            "score": -2.30,
            "pv": [],
        },
    ]

    def fake_analyse_position(board):
        return analysis_results.pop(0)

    monkeypatch.setattr(
        builder,
        "analyse_position",
        fake_analyse_position,
    )

    monkeypatch.setattr(
        builder,
        "get_score",
        lambda info, color: info["score"],
    )

    result = builder.test_black_move(
        board,
        "e5",
    )

    assert result["move"] == "e5"
    assert result["before"] == 1.20
    assert result["after"] == -2.30
    assert result["difference"] == -3.50
    assert result["best_line"] == ""

    expected_board = board.copy()
    expected_board.push_san("e5")

    assert result["board"].fen() == expected_board.fen()   

def test_find_black_mistakes_handles_negative_evaluation_change(
    monkeypatch,
):
    builder = RepertoireBuilder.__new__(RepertoireBuilder)

    builder.mistake_threshold = 1.0
    builder._punishment_cache = {}

    board = chess.Board()
    board.push_san("e4")

    move = board.parse_san("e5")

    monkeypatch.setattr(
        builder,
        "_candidate_black_moves",
        lambda board: [move],
    )

    monkeypatch.setattr(
        builder,
        "test_black_move",
        lambda board, san: {
            "move": san,
            "before": 1.20,
            "after": -2.30,
            "difference": -3.50,
            "best_line": "2. Nf3",
        },
    )

    monkeypatch.setattr(
        builder,
        "_make_mistake_comment",
        lambda result, classification: classification,
    )

    mistakes = builder._find_black_mistakes(board)

    assert len(mistakes) == 1
    assert mistakes[0]["classification"] == "BLUNDER" 


def test_find_black_mistakes_ignores_missing_difference(
    monkeypatch,
):
    builder = RepertoireBuilder.__new__(RepertoireBuilder)

    builder.mistake_threshold = 1.0
    builder._punishment_cache = {}

    board = chess.Board()
    board.push_san("e4")

    move = board.parse_san("e5")

    monkeypatch.setattr(
        builder,
        "_candidate_black_moves",
        lambda board: [move],
    )

    monkeypatch.setattr(
        builder,
        "test_black_move",
        lambda board, san: {
            "move": san,
            "before": None,
            "after": None,
            "difference": None,
            "best_line": "",
        },
    )

    mistakes = builder._find_black_mistakes(board)

    assert mistakes == []


def test_find_black_mistakes_sorts_by_magnitude(
    monkeypatch,
):
    builder = RepertoireBuilder.__new__(RepertoireBuilder)

    builder.mistake_threshold = 1.0
    builder._punishment_cache = {}

    board = chess.Board()
    board.push_san("e4")

    moves = [
        board.parse_san("e5"),
        board.parse_san("Nc6"),
        board.parse_san("d5"),
    ]

    monkeypatch.setattr(
        builder,
        "_candidate_black_moves",
        lambda board: moves,
    )

    differences = {
        "e7e5": -1.2,
        "b8c6": -3.5,
        "d7d5": -2.0,
    }

    def fake_test_black_move(board, san):
        move = board.parse_san(san)
        difference = differences[move.uci()]

        return {
            "move": san,
            "before": 1.0,
            "after": 1.0 + difference,
            "difference": difference,
            "best_line": "2. Nf3",
        }

    monkeypatch.setattr(
        builder,
        "test_black_move",
        fake_test_black_move,
    )

    monkeypatch.setattr(
        builder,
        "_make_mistake_comment",
        lambda result, classification: classification,
    )

    mistakes = builder._find_black_mistakes(board)

    assert [result["difference"] for result in mistakes] == [
        3.5,
        2.0,
        1.2,
    ]

    assert [result["classification"] for result in mistakes] == [
        "BLUNDER",
        "SERIOUS MISTAKE",
        "MISTAKE",
    ]


def test_build_creates_repertoire_tree(monkeypatch):
    builder = RepertoireBuilder.__new__(RepertoireBuilder)

    builder.max_depth = 2
    builder.max_moves = 2
    builder.min_games = 1
    builder.min_frequency = 0.0
    builder._position_cache = {}
    builder._tree_cache = {}
    builder._active = set()
    builder._punishment_cache = {}

    board = chess.Board()

    e4 = board.parse_san("e4")

    white_data = type(
        "FakePositionData",
        (),
        {
            "white": 60,
            "draws": 20,
            "black": 20,
            "moves": [
                MoveStats(
                    uci=e4.uci(),
                    san="e4",
                    white=50,
                    draws=10,
                    black=10,
                )
            ],
        },
    )()

    black_data = type(
        "FakePositionData",
        (),
        {
            "white": 60,
            "draws": 20,
            "black": 20,
            "moves": [],
        },
    )()

    def fake_get_position_data(board):
        if board.turn == chess.WHITE:
            return white_data

        return black_data

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        fake_get_position_data,
    )

    nodes = builder.build(chess.STARTING_FEN)

    assert len(nodes) == 1

    root = nodes[0]

    assert root.san == "e4"
    assert root.uci == "e2e4"
    assert root.comment == "MAIN LINE"
    assert root.children == []

def test_build_attaches_black_mistake_and_punishment(
    monkeypatch,
):
    builder = RepertoireBuilder.__new__(RepertoireBuilder)

    builder.max_depth = 2
    builder.max_moves = 1
    builder.min_games = 1
    builder.min_frequency = 0.0
    builder.punishment_depth = 2

    builder._position_cache = {}
    builder._tree_cache = {}
    builder._active = set()
    builder._punishment_cache = {}

    board = chess.Board()
    board.push_san("e4")

    e5 = board.parse_san("e5")

    def fake_get_position_data(board):
        return type(
            "FakePositionData",
            (),
            {
                "white": 0,
                "draws": 0,
                "black": 0,
                "moves": [],
            },
        )()

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        fake_get_position_data,
    )

    monkeypatch.setattr(
        builder,
        "_find_black_mistakes",
        lambda board: [
            {
                "move": "e5",
                "before": 1.0,
                "after": -2.5,
                "difference": 3.5,
                "classification": "BLUNDER",
                "comment": "BLUNDER: ...e5?!",
            }
        ],
    )

    def fake_build_punishment_nodes(
        board,
        plies,
    ):
        return [
            MoveNode(
                san="Nf3",
                uci="g1f3",
                children=[],
                comment="",
            )
        ]

    monkeypatch.setattr(
        builder,
        "_build_punishment_nodes",
        fake_build_punishment_nodes,
    )

    nodes = builder.build(
        "rnbqkbnr/pppppppp/8/8/"
        "4P3/8/PPPP1PPP/RNBQKBNR b "
        "KQkq - 0 1"
    )

    assert len(nodes) == 1

    mistake = nodes[0]

    assert mistake.san == "e5"
    assert mistake.uci == "e7e5"
    assert mistake.comment.startswith("MISTAKE")
    assert len(mistake.children) == 1

    punishment = mistake.children[0]

    assert punishment.san == "Nf3"
    assert punishment.uci == "g1f3"
    assert punishment.comment.startswith("PUNISHMENT")


def test_cli_builds_and_exports_repertoire(monkeypatch):
    output_path = "test-output.pgn"

    fake_nodes = [
        MoveNode(
            san="e4",
            uci="e2e4",
            children=[],
            comment="MAIN LINE",
        )
    ]

    captured = {}

    class FakeClient:
        def close(self):
            captured["client_closed"] = True

    class FakeBuilder:
        def __init__(
            self,
            client,
            color,
            min_games,
            threshold,
        ):
            captured["builder_args"] = (
                client,
                color,
                min_games,
                threshold,
            )

        def build(self, fen):
            captured["build_fen"] = fen
            return fake_nodes

    def fake_build_pgn(
        fen,
        nodes,
        output,
    ):
        captured["pgn_args"] = (
            fen,
            nodes,
            output,
        )

    monkeypatch.setattr(
        cli,
        "LichessClient",
        FakeClient,
    )

    monkeypatch.setattr(
        cli,
        "RepertoireBuilder",
        FakeBuilder,
    )

    monkeypatch.setattr(
        cli,
        "build_pgn",
        fake_build_pgn,
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lichess-opening-explorer",
            "--fen",
            chess.STARTING_FEN,
            "--min-games",
            "100",
            "--threshold",
            "0.6",
            "--output",
            output_path,
        ],
    )

    cli.main()

    client, color, min_games, threshold = (
        captured["builder_args"]
    )

    assert isinstance(client, FakeClient)
    assert color == chess.WHITE
    assert min_games == 100
    assert threshold == 0.6

    assert captured["build_fen"] == chess.STARTING_FEN

    assert captured["pgn_args"] == (
        chess.STARTING_FEN,
        fake_nodes,
        output_path,
    )

    assert captured["client_closed"] is True

def test_cli_rejects_invalid_fen(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lichess-opening-explorer",
            "--fen",
            "invalid-fen",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1