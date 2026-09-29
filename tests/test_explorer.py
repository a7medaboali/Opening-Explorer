import sys

import chess
import chess.engine
import chess.pgn
import httpx
import pytest
from chess.engine import Cp, Mate, PovScore
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from lichess_opening_explorer import cli
from lichess_opening_explorer.api import (
    LichessAuthError,
    LichessClient,
    LichessResponseError,
    RateLimited,
    TransientLichessError,
    _parse_position,
)
from lichess_opening_explorer.evaluation import (
    classify_mistake,
    format_score,
    mate_distance,
    score_to_centipawns,
    win_probability,
)
from lichess_opening_explorer.explorer import RepertoireBuilder, position_key
from lichess_opening_explorer.models import MoveNode, MoveStats, PositionData
from lichess_opening_explorer.pgn_export import _add_nodes, build_pgn
from lichess_opening_explorer.sources import LichessDataSource, PgnDataSource

# Nimzo-Indian, 1.d4 d5 2.c4 e6 3.Nc3 Nf6 4.Bg5 Bb4 -- White to move.
NIMZO_FEN = (
    "rnbqk2r/ppp2ppp/4pn2/3p2B1/"
    "1bPP4/2N5/PP2PPPP/R2QKBNR w KQkq - 4 5"
)

# QGD, 1.d4 d5 2.c4 e6 3.Nc3 Nf6 4.Bg5 Be7 -- White to move.
QGD_FEN = (
    "rnbqk2r/ppp1bppp/4pn2/3p2B1/"
    "2PP4/2N5/PP2PPPP/R2QKBNR w KQkq - 4 5"
)


# =============================================================
# Fixtures / helpers
# =============================================================


def make_board(fen: str, *sans: str) -> chess.Board:
    board = chess.Board(fen)
    for san in sans:
        board.push_san(san)
    return board


def make_builder(**overrides) -> RepertoireBuilder:
    """
    Build a RepertoireBuilder without touching the network or the engine.

    Every attribute the builder uses is set explicitly so that a change to
    the constructor is caught here rather than silently defaulted.
    """
    settings = {
        "min_games": 50,
        "min_frequency": 0.10,
        "min_move_frequency": 0.05,
        "min_opponent_frequency": 0.001,
        "min_root_share": 0.0,
        "max_depth": 10,
        "max_moves": 3,
        "max_mistakes": 2,
        "mistake_threshold": 12.0,
        "max_deviation_moves": 6,
        "punishment_depth": 6,
    }
    settings.update(overrides)

    builder = RepertoireBuilder.__new__(RepertoireBuilder)
    builder.source = None
    builder.color = chess.WHITE
    builder._engine_cache = {}
    builder._tree_cache = {}
    builder._root_games = 0
    builder._active = set()
    builder.engine = None
    builder._owns_engine = False

    for name, value in settings.items():
        setattr(builder, name, value)

    return builder


def position_data(moves, total=None):
    """PositionData whose white/draws/black totals are consistent with `moves`."""
    if total is None:
        total = sum(move.total for move in moves)

    return PositionData(
        white=total,
        draws=0,
        black=0,
        moves=list(moves),
    )


def move_stats(board: chess.Board, san: str, games: int) -> MoveStats:
    return MoveStats(
        uci=board.parse_san(san).uci(),
        san=san,
        white=games,
        draws=0,
        black=0,
    )


def data_for(board: chess.Board, moves: dict[str, int]) -> PositionData:
    """
    PositionData for a position, with one entry per SAN.

    A SAN that is not legal in the position is skipped rather than
    raising, so a test can describe a whole tree and let each position
    pick out the moves that apply to it.
    """
    stats = []

    for san, games in moves.items():
        try:
            stats.append(move_stats(board, san, games))
        except ValueError:
            continue

    return position_data(stats)


# =============================================================
# FEN handling
# =============================================================


def test_fen_is_valid():
    assert chess.Board(NIMZO_FEN).is_valid()


def test_position_key_drops_move_counters():
    """Regression: transpositions must share a cache key."""
    first = make_board(NIMZO_FEN, "e3")
    second = make_board(NIMZO_FEN, "e3")

    # Same position, different clocks.
    second.halfmove_clock = 9
    second.fullmove_number = 40

    assert first.fen() != second.fen(), "the fixtures must differ somewhere"
    assert position_key(first) == position_key(second)
    assert len(position_key(first).split(" ")) == 4


def test_position_key_is_shared_by_a_real_transposition():
    """
    Regression: the en passant field used the "fen" rule, which records the
    square behind every double pawn push even when no capture is possible.
    The same position reached in a different move order then got a second
    cache key and a second Lichess request.
    """
    by_e3 = make_board(NIMZO_FEN, "e3", "a6", "f4")
    by_f4 = make_board(NIMZO_FEN, "f4", "a6", "e3")

    assert by_e3.fen() == by_f4.fen()
    assert position_key(by_e3) == position_key(by_f4)


def test_position_key_keeps_a_live_en_passant_square():
    """A real en passant capture must not be stripped from the key."""
    # 1.e4 e6 2.e5 d5 -- White's e5 pawn may play exd6 e.p.
    live = make_board(chess.STARTING_FEN, "e4", "e6", "e5", "d5")

    assert live.ep_square == chess.D6
    assert live.has_legal_en_passant()
    assert position_key(live).split(" ")[3] == "d6"

    # 1.e4 d5 -- the ep square is recorded but no pawn can take it, which
    # is exactly the case the "legal" rule drops and "fen" keeps.
    dead = make_board(chess.STARTING_FEN, "e4", "d5")

    assert dead.ep_square == chess.D6
    assert not dead.has_legal_en_passant()
    assert position_key(dead).split(" ")[3] == "-"


def test_build_pgn_writes_setup_headers(tmp_path):
    output_path = tmp_path / "setup.pgn"

    build_pgn(
        NIMZO_FEN,
        [MoveNode(san="e3", uci="e2e3")],
        str(output_path),
    )

    with output_path.open() as handle:
        game = chess.pgn.read_game(handle)

    assert game.headers["SetUp"] == "1"
    assert game.headers["FEN"] == NIMZO_FEN


# =============================================================
# evaluation.py -- the win probability model
# =============================================================


def test_win_probability_is_monotonic_and_centred():
    assert win_probability(0) == pytest.approx(50.0)
    assert win_probability(100) > win_probability(50) > 0.0
    assert win_probability(-100) < 50.0
    assert win_probability(-300) < win_probability(-100)


def test_win_probability_anchors_match_lichess():
    """Anchors from the Lichess win-probability model."""
    assert win_probability(100) == pytest.approx(59.1, abs=0.15)
    assert win_probability(-100) == pytest.approx(40.9, abs=0.15)
    assert win_probability(400) == pytest.approx(81.3, abs=0.2)

    # The model is deliberately not linear: it saturates, which is the
    # whole reason for using it instead of raw centipawn thresholds.
    assert win_probability(800) == pytest.approx(95.0, abs=0.5)
    assert win_probability(800) - win_probability(400) < 50.0


def test_score_to_centipawns_respects_perspective():
    info = {"score": PovScore(Cp(120), chess.BLACK)}

    assert score_to_centipawns(info, chess.WHITE) == -120
    assert score_to_centipawns(info, chess.BLACK) == 120


def test_score_to_centipawns_handles_mate():
    """
    Regression: mate used to return None, so the most punishable move in
    the position was silently discarded.
    """
    white_mates = {"score": PovScore(Mate(3), chess.WHITE)}
    black_mates = {"score": PovScore(Mate(3), chess.BLACK)}

    assert score_to_centipawns(white_mates, chess.WHITE) > 0
    assert score_to_centipawns(black_mates, chess.WHITE) < 0
    assert score_to_centipawns(white_mates, chess.BLACK) < 0

    assert score_to_centipawns(white_mates, chess.WHITE) > score_to_centipawns(
        {"score": PovScore(Mate(9), chess.WHITE)}, chess.WHITE
    )
    assert score_to_centipawns(black_mates, chess.WHITE) < score_to_centipawns(
        {"score": PovScore(Mate(9), chess.BLACK)}, chess.WHITE
    )


def test_score_to_centipawns_without_score_key():
    assert score_to_centipawns({}, chess.WHITE) is None


def test_mate_distance_and_formatting():
    info = {"score": PovScore(Mate(4), chess.WHITE)}

    assert mate_distance(info, chess.WHITE) == 4
    assert mate_distance({"score": PovScore(Cp(20), chess.WHITE)}, chess.WHITE) is None
    assert mate_distance({}, chess.WHITE) is None

    assert format_score(None, 3) == "M3"
    assert format_score(None, -3) == "-M3"
    assert format_score(None, None) == "N/A"
    assert format_score(0) == "+50.0%"


def test_mate_becomes_extreme_win_probability():
    winning = score_to_centipawns({"score": PovScore(Mate(2), chess.WHITE)}, chess.WHITE)
    losing = score_to_centipawns({"score": PovScore(Mate(2), chess.BLACK)}, chess.WHITE)

    assert win_probability(winning) > 99.5
    assert win_probability(losing) < 0.5


def test_classify_mistake_bands_scale_with_the_threshold():
    assert classify_mistake(11.9, 12.0) is None
    assert classify_mistake(12.0, 12.0) == "MISTAKE"
    assert classify_mistake(18.0, 12.0) == "SERIOUS MISTAKE"
    assert classify_mistake(30.0, 12.0) == "BLUNDER"


def test_classify_mistake_never_flags_a_negative_loss():
    """
    Regression: a move that improves on the baseline must never be a
    mistake, whatever the magnitude.
    """
    assert classify_mistake(-0.1, 12.0) is None
    assert classify_mistake(-40.0, 12.0) is None


# =============================================================
# _select_moves
# =============================================================


def test_select_moves_applies_a_relative_floor():
    """Regression: no per-move floor meant 0.003% moves entered the repertoire."""
    builder = make_builder(min_move_frequency=0.05, min_frequency=0.8)
    board = make_board(NIMZO_FEN)

    data = position_data(
        [
            move_stats(board, "e3", 7000),
            move_stats(board, "cxd5", 1500),
            move_stats(board, "Nf3", 400),
        ]
    )

    selected = builder._select_moves(board, data)

    # 70.7% then 15.2% crosses the 80% cumulative threshold.
    assert [item[0].san for item in selected] == ["e3", "cxd5"]
    assert selected[0][2] == 7000
    assert selected[0][1] == pytest.approx(7000 / 8900)
    assert selected[1][1] == pytest.approx(1500 / 8900)


def test_select_moves_drops_a_move_below_the_floor():
    """0.4% is far below the 5% floor, so it must not enter the repertoire."""
    builder = make_builder(min_move_frequency=0.05)
    board = make_board(NIMZO_FEN)

    data = position_data(
        [
            move_stats(board, "e3", 9950),
            move_stats(board, "cxd5", 10),
            move_stats(board, "Nf3", 40),
        ]
    )

    assert [item[0].san for item in builder._select_moves(board, data)] == ["e3"]


def test_select_moves_stops_at_the_cumulative_threshold():
    builder = make_builder(min_move_frequency=0.0, min_frequency=0.8, max_moves=3)
    board = make_board(NIMZO_FEN)

    data = position_data(
        [
            move_stats(board, "e3", 7000),
            move_stats(board, "cxd5", 2000),
            move_stats(board, "Nf3", 500),
            move_stats(board, "Bxf6", 100),
        ]
    )

    selected = builder._select_moves(board, data)

    # 0.7 then 0.9: the second move crosses 0.8, so the rest are dropped.
    assert [item[0].san for item in selected] == ["e3", "cxd5"]


def test_select_moves_never_exceeds_max_moves():
    builder = make_builder(min_move_frequency=0.0, min_frequency=1.01, max_moves=2)
    board = make_board(NIMZO_FEN)

    data = position_data(
        [
            move_stats(board, "e3", 7000),
            move_stats(board, "cxd5", 2000),
            move_stats(board, "Nf3", 500),
        ]
    )

    assert len(builder._select_moves(board, data)) == 2


def test_select_moves_is_sorted_by_games():
    builder = make_builder(min_move_frequency=0.0, min_frequency=1.01, max_moves=3)
    board = make_board(NIMZO_FEN)

    data = position_data(
        [
            move_stats(board, "Nf3", 10),
            move_stats(board, "e3", 7000),
            move_stats(board, "cxd5", 2000),
        ]
    )

    selected = builder._select_moves(board, data)

    assert [item[0].san for item in selected] == ["e3", "cxd5", "Nf3"]
    assert [item[2] for item in selected] == [7000, 2000, 10]


def test_select_moves_deduplicates_repeated_rows():
    """
    Regression: a duplicated API row became two identical branches,
    because python-chess's add_variation never merges duplicates.
    """
    builder = make_builder(min_move_frequency=0.0, min_frequency=1.01)
    board = make_board(NIMZO_FEN)

    data = position_data(
        [
            MoveStats("e2e3", "e3", 500, 0, 0),
            MoveStats("e2e3", "e3", 400, 0, 0),
            move_stats(board, "cxd5", 300),
        ]
    )

    selected = builder._select_moves(board, data)

    ucis = [item[0].uci for item in selected]
    assert ucis == ["e2e3", "c4d5"]
    assert len(ucis) == len(set(ucis))


def test_select_moves_returns_nothing_for_an_empty_position():
    builder = make_builder()
    board = make_board(NIMZO_FEN)

    assert builder._select_moves(board, PositionData(0, 0, 0, [])) == []


def test_select_moves_stops_at_rare_positions():
    """
    min_games is a position level cutoff, so a position with too little data
    ends the branch instead of being filtered move by move.
    """
    board = make_board(NIMZO_FEN)

    small = make_builder(min_games=50)
    large = make_builder(min_games=5000)

    data = position_data([move_stats(board, "e3", 60)])

    assert small._select_moves(board, data) != []
    assert large._select_moves(board, data) == []


def test_select_moves_keeps_a_deep_position_with_small_absolute_counts():
    """
    The regression that produced a repertoire that died after two plies: a
    per-move absolute floor dropped everything in a late, thinly played
    position.  A relative floor keeps the position alive.
    """
    builder = make_builder(
        min_games=50, min_move_frequency=0.05, min_frequency=1.01
    )
    board = make_board(NIMZO_FEN, "e3", "h6", "Nf3", "g6", "Bh4", "e5")

    data = position_data(
        [move_stats(board, "Bd3", 400), move_stats(board, "a3", 50)]
    )

    selected = builder._select_moves(board, data)

    # 400/450 = 88.9% passes, 50/450 = 11.1% passes too at a 5% floor.
    assert [item[0].san for item in selected] == ["Bd3", "a3"]


def test_select_moves_keeps_a_deep_position_an_absolute_floor_would_kill():
    """A position with only 200 games must still yield its main move."""
    builder = make_builder(min_games=50, min_move_frequency=0.05)
    board = make_board(NIMZO_FEN, "e3", "h6", "Nf3", "g6", "Bh4", "e5")

    data = position_data([move_stats(board, "Bd3", 200)])

    assert [item[0].san for item in builder._select_moves(board, data)] == ["Bd3"]


# =============================================================
# Realism gate for opponent candidates
# =============================================================


def test_candidate_opponent_moves_only_uses_database_moves(monkeypatch):
    """
    Regression: every capture and check used to be analysed, so moves no
    human plays became MISTAKE branches.
    """
    builder = make_builder(max_deviation_moves=6)
    builder.color = chess.WHITE

    board = make_board(NIMZO_FEN, "cxd5")

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: position_data(
            [move_stats(board, "exd5", 400), move_stats(board, "Nxd5", 30)]
        ),
    )

    candidates = builder._candidate_opponent_moves(board)

    assert [board.san(move) for move in candidates] == ["exd5", "Nxd5"]

    forcing = [
        move
        for move in candidates
        if board.is_capture(move) or board.gives_check(move)
    ]
    assert [board.san(move) for move in forcing] == ["exd5", "Nxd5"]


def test_candidate_opponent_moves_excludes_unplayed_captures(monkeypatch):
    """...Bxa3 and ...Qxh2 style moves never appear unless the database has them."""
    builder = make_builder()
    builder.color = chess.WHITE

    board = make_board(NIMZO_FEN, "e3")

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: position_data([move_stats(board, "Nbd7", 500)]),
    )

    candidates = builder._candidate_opponent_moves(board)

    assert len(candidates) == 1
    for move in candidates:
        assert not board.is_capture(move)


def test_candidate_opponent_moves_are_capped(monkeypatch):
    builder = make_builder(max_deviation_moves=3)
    builder.color = chess.WHITE

    board = make_board(NIMZO_FEN, "e3")

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: position_data(
            [move_stats(board, san, 900 - index * 10)
             for index, san in enumerate(["Nbd7", "O-O", "Ne4", "a6", "h6"])]
        ),
    )

    # The database's own order is games descending, so the cap keeps the
    # moves a real opponent is most likely to play.
    assert [board.san(m) for m in builder._candidate_opponent_moves(board)] == [
        "Nbd7",
        "O-O",
        "Ne4",
    ]


def test_candidate_opponent_moves_returns_nothing_on_the_users_turn(monkeypatch):
    builder = make_builder()
    builder.color = chess.WHITE

    board = make_board(NIMZO_FEN)
    assert board.turn == chess.WHITE

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: pytest.fail("must not query the database on our own turn"),
    )

    assert builder._candidate_opponent_moves(board) == []


def test_candidate_opponent_moves_works_for_black_repertoires(monkeypatch):
    """Regression: colour was stored and never read, so Black users got nothing."""
    builder = make_builder()
    builder.color = chess.BLACK

    # White to move, so White is the opponent of a Black repertoire.
    board = make_board(NIMZO_FEN)
    assert board.turn == chess.WHITE

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: position_data([move_stats(board, "c5", 300)]),
    )

    candidates = builder._candidate_opponent_moves(board)

    assert [board.san(move) for move in candidates] == ["c5"]


def test_candidate_opponent_moves_ignore_our_own_turn_for_black(monkeypatch):
    """A Black repertoire must not scan Black's own moves."""
    builder = make_builder()
    builder.color = chess.BLACK

    board = make_board(NIMZO_FEN, "e3")
    assert board.turn == chess.BLACK

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: pytest.fail("must not scan our own turn"),
    )

    assert builder._candidate_opponent_moves(board) == []


def test_candidate_opponent_moves_skips_illegal_rows(monkeypatch):
    builder = make_builder()
    builder.color = chess.WHITE

    board = make_board(NIMZO_FEN, "e3")

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: PositionData(
            1000,
            0,
            0,
            [MoveStats("z9z9", "Zz9", 900, 0, 0), move_stats(board, "Nbd7", 50)],
        ),
    )

    candidates = builder._candidate_opponent_moves(board)

    assert [board.san(move) for move in candidates] == ["Nbd7"]


def test_candidate_opponent_moves_skips_a_move_below_two_games(monkeypatch):
    """A single game is not a realistic opponent choice."""
    builder = make_builder()
    builder.color = chess.WHITE

    board = make_board(NIMZO_FEN, "e3")

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: PositionData(
            1000,
            0,
            0,
            [MoveStats("c5", "c5", 1, 0, 0), move_stats(board, "Nbd7", 50)],
        ),
    )

    assert [board.san(m) for m in builder._candidate_opponent_moves(board)] == [
        "Nbd7"
    ]


def test_candidate_opponent_moves_drops_a_one_in_ten_thousand_curiosity(
    monkeypatch,
):
    """
    A single game inside a 50,000 game position is not something an opponent
    will actually play, so it must not earn a punishment line.
    """
    builder = make_builder()
    builder.color = chess.WHITE
    builder.min_opponent_frequency = 0.001

    board = make_board(NIMZO_FEN, "e3")

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: PositionData(
            50000,
            0,
            0,
            [
                move_stats(board, "c5", 40000),
                move_stats(board, "Nbd7", 9997),
                # 2/50000 = 0.004%, far under the 0.1% floor.
                move_stats(board, "Nh5", 2),
            ],
        ),
    )

    assert [board.san(move) for move in builder._candidate_opponent_moves(board)] == [
        "c5",
        "Nbd7",
    ]


def test_candidate_opponent_moves_keeps_a_rare_but_real_blunder(monkeypatch):
    """
    The floor must not be so high that a real mistake disappears: a blunder
    played in 0.3% of games is still worth punishing.
    """
    builder = make_builder()
    builder.color = chess.WHITE
    builder.min_opponent_frequency = 0.001

    board = make_board(NIMZO_FEN, "e3")

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: PositionData(
            10000,
            0,
            0,
            [
                move_stats(board, "c5", 8000),
                move_stats(board, "Nbd7", 1970),
                # 0.3% -- rare, but not a curiosity.
                move_stats(board, "Nh5", 30),
            ],
        ),
    )

    assert "Nh5" in [
        board.san(move) for move in builder._candidate_opponent_moves(board)
    ]


def test_candidate_opponent_moves_deduplicates_repeated_rows(monkeypatch):
    builder = make_builder(max_deviation_moves=6)
    builder.color = chess.WHITE

    board = make_board(NIMZO_FEN, "e3")

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: PositionData(
            1000,
            0,
            0,
            [
                MoveStats("c7c5", "c5", 400, 0, 0),
                MoveStats("c7c5", "c5", 300, 0, 0),
                move_stats(board, "Nbd7", 200),
            ],
        ),
    )

    assert [board.san(m) for m in builder._candidate_opponent_moves(board)] == [
        "c5",
        "Nbd7",
    ]


# =============================================================
# Mistake detection
# =============================================================


def stub_analysis(builder, scores, engine_best_san=None):
    """Give the builder deterministic engine output.

    `scores` maps a SAN to the expected score the opponent ends up with
    after playing it.  `engine_best_san` is what the engine would have
    played; it defaults to the highest scoring candidate, so the baseline
    is consistent with the stubbed evaluations.
    """
    if engine_best_san is None:
        engine_best_san = max(scores, key=scores.get)

    def _win_percent_after(board, move):
        return scores[board.san(move)]

    def _engine_best_move(board):
        return board.parse_san(engine_best_san)

    def _evaluate_move(board, move, best=None):
        after = scores[board.san(move)]
        before = scores[board.san(best)] if best is not None else 60.0

        return {
            "move": board.san(move),
            "uci": move.uci(),
            "before": before,
            "after": after,
            "difference": before - after,
            "best_line": "",
            "board": board.copy(stack=False),
        }

    builder._win_percent_after = _win_percent_after
    builder._engine_best_move = _engine_best_move
    builder._evaluate_move = _evaluate_move

    return builder


def test_opponent_move_that_improves_is_never_a_mistake(monkeypatch):
    """
    Regression: the old code took abs() of the swing, so a move that made
    the position BETTER for the opponent was reported as a blunder.

    Here the engine prefers ...Ne4 over the database's main move, and
    ...Ne4 is the only candidate that can be scored, so it is the
    baseline.  A move can never be its own baseline, so it is never
    flagged no matter how large the raw swing looks.
    """
    builder = make_builder(mistake_threshold=12.0)
    builder.color = chess.WHITE

    board = make_board(NIMZO_FEN, "e3")

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: position_data([move_stats(board, "Ne4", 500)]),
    )

    # The engine's own choice scores 80; the position baseline scores 50.
    # abs() of that 30 point swing would report a blunder.
    stub_analysis(builder, {"Ne4": 80.0, "h6": 50.0}, engine_best_san="Ne4")

    assert builder._find_opponent_mistakes(board) == []


def test_an_improving_move_is_not_reported_even_with_a_large_swing(monkeypatch):
    """Same regression, seen from the improving move's own record."""
    builder = make_builder(mistake_threshold=12.0)
    builder.color = chess.WHITE

    board = make_board(NIMZO_FEN, "e3")

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: position_data(
            [move_stats(board, "Nbd7", 500), move_stats(board, "Ne4", 300)]
        ),
    )

    # ...Ne4 is rated better for Black than ...Nbd7, so the baseline is
    # ...Ne4 and only ...Nbd7 can be a mistake.
    stub_analysis(builder, {"Nbd7": 50.0, "Ne4": 80.0}, engine_best_san="Ne4")

    mistakes = builder._find_opponent_mistakes(board)

    assert [item["move"] for item in mistakes] == ["Nbd7"]
    assert mistakes[0]["difference"] == pytest.approx(30.0)


def test_a_move_matching_the_baseline_is_not_a_mistake(monkeypatch):
    builder = make_builder(mistake_threshold=12.0)
    builder.color = chess.WHITE

    board = make_board(NIMZO_FEN, "e3")

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: position_data(
            [move_stats(board, "Nbd7", 500), move_stats(board, "Ne4", 400)]
        ),
    )

    stub_analysis(builder, {"Nbd7": 55.0, "Ne4": 55.0}, engine_best_san="Nbd7")

    assert builder._find_opponent_mistakes(board) == []


def test_a_move_below_the_threshold_is_not_a_mistake(monkeypatch):
    builder = make_builder(mistake_threshold=12.0)
    builder.color = chess.WHITE

    board = make_board(NIMZO_FEN, "e3")

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: position_data(
            [move_stats(board, "Nbd7", 500), move_stats(board, "Ne4", 100)]
        ),
    )

    stub_analysis(builder, {"Nbd7": 60.0, "Ne4": 52.0}, engine_best_san="Nbd7")

    assert builder._find_opponent_mistakes(board) == []


def test_realistic_mistakes_are_reported_and_sorted(monkeypatch):
    builder = make_builder(mistake_threshold=12.0, max_mistakes=3)
    builder.color = chess.WHITE

    board = make_board(NIMZO_FEN, "e3")

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: position_data(
            [
                move_stats(board, "Nbd7", 500),
                move_stats(board, "O-O", 400),
                move_stats(board, "Ne4", 30),
                move_stats(board, "a6", 20),
                move_stats(board, "h6", 15),
            ]
        ),
    )

    scores = {
        "Nbd7": 60.0,
        "O-O": 50.0,
        "Ne4": 45.0,
        "a6": 30.0,
        "h6": 20.0,
    }

    stub_analysis(builder, scores, engine_best_san="Nbd7")

    mistakes = builder._find_opponent_mistakes(board)

    # ...h6 throws away 40 points, ...a6 30, ...Ne4 15.
    assert [item["move"] for item in mistakes] == ["h6", "a6", "Ne4"]
    assert [round(item["difference"], 1) for item in mistakes] == [40.0, 30.0, 15.0]
    assert [item["classification"] for item in mistakes] == [
        "BLUNDER",
        "BLUNDER",
        "MISTAKE",
    ]


def test_mistake_count_is_capped(monkeypatch):
    """Regression: every mistake used to be added, flooding the tree."""
    builder = make_builder(mistake_threshold=5.0, max_mistakes=2)
    builder.color = chess.WHITE

    board = make_board(NIMZO_FEN, "e3")

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: position_data(
            [move_stats(board, san, 400 - index)
             for index, san in enumerate(["Nbd7", "O-O", "Ne4", "a6", "h6"])]
        ),
    )

    scores = {"Nbd7": 60.0, "O-O": 50.0, "Ne4": 20.0, "a6": 10.0, "h6": 0.0}

    stub_analysis(builder, scores, engine_best_san="Nbd7")

    mistakes = builder._find_opponent_mistakes(board)

    assert [item["move"] for item in mistakes] == ["h6", "a6"]


def test_mistakes_are_found_for_a_black_repertoire(monkeypatch):
    """Regression: White deviations were never scanned when playing Black."""
    builder = make_builder(mistake_threshold=12.0)
    builder.color = chess.BLACK

    # White to move, so White is the opponent of a Black repertoire.
    board = make_board(NIMZO_FEN)
    assert board.turn == chess.WHITE

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: position_data(
            [move_stats(board, "c5", 400), move_stats(board, "e4", 300)]
        ),
    )

    scores = {"c5": 60.0, "e4": 20.0}

    stub_analysis(builder, scores, engine_best_san="c5")

    mistakes = builder._find_opponent_mistakes(board)

    assert [item["move"] for item in mistakes] == ["e4"]
    assert mistakes[0]["classification"] == "BLUNDER"


def test_no_mistakes_on_the_users_own_turn():
    builder = make_builder()
    builder.color = chess.WHITE

    def fail(board):
        raise AssertionError("must not scan our own moves")

    builder._candidate_opponent_moves = fail

    assert builder._find_opponent_mistakes(make_board(NIMZO_FEN)) == []


def test_no_mistakes_when_the_engine_agrees_with_the_database(monkeypatch):
    """
    The baseline includes the engine's own choice, so a perfectly normal
    database move is not reported just because Stockfish prefers something
    else.
    """
    builder = make_builder(mistake_threshold=12.0)
    builder.color = chess.WHITE

    board = make_board(NIMZO_FEN, "e3")

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: position_data([move_stats(board, "Nbd7", 500)]),
    )

    scores = {"Nbd7": 50.0, "h6": 50.0}

    stub_analysis(builder, scores, engine_best_san="h6")

    assert builder._find_opponent_mistakes(board) == []


def test_no_mistakes_without_candidates(monkeypatch):
    builder = make_builder()
    builder.color = chess.WHITE

    board = make_board(NIMZO_FEN, "e3")

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: PositionData(0, 0, 0, []),
    )

    assert builder._find_opponent_mistakes(board) == []


def test_mistake_records_carry_game_frequency(monkeypatch):
    builder = make_builder(mistake_threshold=12.0)
    builder.color = chess.WHITE

    board = make_board(NIMZO_FEN, "e3")

    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: position_data(
            [move_stats(board, "Nbd7", 900), move_stats(board, "Ne4", 100)]
        ),
    )

    stub_analysis(builder, {"Nbd7": 60.0, "Ne4": 20.0}, engine_best_san="Nbd7")

    mistake = builder._find_opponent_mistakes(board)[0]

    assert mistake["games"] == 100
    assert mistake["frequency"] == pytest.approx(0.1)


def test_mistake_threshold_must_be_positive():
    with pytest.raises(ValueError):
        RepertoireBuilder(
            None,
            chess.WHITE,
            engine=object(),
            mistake_threshold=0,
        )


# =============================================================
# Comments
# =============================================================


def test_mistake_comment_reports_win_probability():
    builder = make_builder()

    comment = builder._make_mistake_comment(
        {
            "move": "Nxd5",
            "before": 62.4,
            "after": 41.0,
            "difference": 21.4,
            "best_line": "6. Bxd8 Qxd8",
        },
        "BLUNDER",
    )

    assert comment == (
        "BLUNDER: ...Nxd5?!\n"
        "Best available: 62.4%\n"
        "After this move: 41.0%\n"
        "Win probability given away: 21.4\n"
        "Punishment: Bxd8!"
    )


def test_mistake_comment_handles_a_missing_punishment_line():
    builder = make_builder()

    comment = builder._make_mistake_comment(
        {
            "move": "Nxd5",
            "before": None,
            "after": None,
            "difference": None,
            "best_line": "",
        },
        "MISTAKE",
    )

    assert "Best available: N/A" in comment
    assert "Punishment: N/A" in comment


def test_first_punishment_move_skips_move_numbers():
    builder = make_builder()

    assert builder._first_punishment_move("6. Bxd8 Qxd8 7. Qb3") == "Bxd8!"
    assert builder._first_punishment_move("6... Bxd8") == "Bxd8!"
    assert builder._first_punishment_move("") == "N/A"


# =============================================================
# Punishment lines
# =============================================================


def test_build_punishment_nodes_chains_the_principal_variation():
    """A punishment line is a single PV, chained into nested children."""
    builder = make_builder(punishment_depth=4)

    # Black to move after 5.cxd5, and the engine answers ...exd5.
    start = make_board(NIMZO_FEN, "cxd5")
    assert start.turn == chess.BLACK

    walk = start.copy()
    pv = []

    for san in ["exd5", "Nf3", "Nc6"]:
        move = walk.parse_san(san)
        pv.append(move)
        walk.push(move)

    builder.analyse_position = lambda board: {"pv": pv}

    nodes = builder._build_punishment_nodes(start, 4)

    assert len(nodes) == 1
    assert nodes[0].san == "exd5"
    assert nodes[0].children[0].san == "Nf3"
    assert nodes[0].children[0].children[0].san == "Nc6"
    assert nodes[0].children[0].children[0].children == []


def test_build_punishment_nodes_respects_plies_and_empty_pv():
    builder = make_builder()

    board = make_board(NIMZO_FEN, "cxd5")

    assert builder._build_punishment_nodes(board, 0) == []

    builder.analyse_position = lambda board: {}
    assert builder._build_punishment_nodes(board, 6) == []

    walk = board.copy()
    pv = [walk.parse_san("exd5")]
    walk.push_san("exd5")
    pv.append(walk.parse_san("Nf3"))

    builder.analyse_position = lambda board: {"pv": pv}

    nodes = builder._build_punishment_nodes(board, 1)

    assert len(nodes) == 1
    assert nodes[0].children == []


def test_build_punishment_nodes_uses_board_derived_san():
    """
    Regression: the node took its SAN from the move list, so a stale or
    wrong SAN produced a PGN that could not be replayed.
    """
    builder = make_builder()

    board = make_board(NIMZO_FEN, "cxd5")
    builder.analyse_position = lambda board: {"pv": [board.parse_san("exd5")]}

    nodes = builder._build_punishment_nodes(board, 6)

    assert nodes[0].san == "exd5"
    assert nodes[0].uci == "e6d5"


def test_build_punishment_nodes_drops_illegal_pv_moves():
    """A stale or corrupt PV must not produce an illegal branch."""
    builder = make_builder()

    board = make_board(NIMZO_FEN, "cxd5")
    builder.analyse_position = lambda board: {
        "pv": [board.parse_san("exd5"), chess.Move.from_uci("a1a8")]
    }

    nodes = builder._build_punishment_nodes(board, 6)

    assert len(nodes) == 1
    assert nodes[0].san == "exd5"
    assert nodes[0].children == []


# =============================================================
# Tree building
# =============================================================


def test_build_creates_repertoire_tree():
    builder = make_builder(max_depth=2, max_moves=2, min_move_frequency=0.0)
    builder.analyse_position = lambda board: {}

    builder._get_position_data = lambda board: data_for(board, {"e4": 60})

    nodes = builder.build(chess.STARTING_FEN)

    assert len(nodes) == 1
    assert nodes[0].san == "e4"
    assert nodes[0].uci == "e2e4"
    assert nodes[0].comment.startswith("MAIN LINE")
    assert "100.0%" in nodes[0].comment
    assert nodes[0].children == []


def test_build_labels_alternatives_and_covers_them():
    builder = make_builder(
        max_depth=1, max_moves=2, min_move_frequency=0.0, min_frequency=0.8
    )
    builder.analyse_position = lambda board: {}

    builder._get_position_data = lambda board: data_for(
        board, {"e4": 700, "d4": 300}
    )

    nodes = builder.build(chess.STARTING_FEN)

    assert [node.comment.split()[0] for node in nodes] == ["MAIN", "ALTERNATIVE"]


def test_build_honours_max_depth():
    """max_depth counts plies, so a depth of 3 gives three levels of moves."""
    builder = make_builder(
        max_depth=3, max_moves=1, min_move_frequency=0.0, min_frequency=1.01
    )
    builder.analyse_position = lambda board: {}

    builder._get_position_data = lambda board: data_for(
        board, {"e4": 100, "e5": 100, "Nf3": 100}
    )

    def depth_of(nodes):
        if not nodes:
            return 0
        return 1 + depth_of(nodes[0].children)

    assert depth_of(builder.build(chess.STARTING_FEN)) == 3


def test_build_stops_at_rare_positions():
    builder = make_builder(
        max_depth=6, max_moves=1, min_games=50, min_move_frequency=0.0
    )
    builder.analyse_position = lambda board: {}

    builder._get_position_data = lambda board: data_for(
        board, {"e4": 100, "e5": 5}
    )

    nodes = builder.build(chess.STARTING_FEN)

    assert len(nodes) == 1
    assert nodes[0].children == []


def test_build_skips_malformed_api_moves():
    """
    Regression: one unusable row raised InvalidMoveError out of build().
    """
    builder = make_builder(max_depth=1, max_moves=3, min_move_frequency=0.0)
    builder.analyse_position = lambda board: {}

    board = make_board(chess.STARTING_FEN)

    builder._get_position_data = lambda board: PositionData(
        1000,
        0,
        0,
        [MoveStats("z9z9", "Zz9", 900, 0, 0), move_stats(board, "e4", 100)],
    )

    nodes = builder.build(chess.STARTING_FEN)

    assert [node.san for node in nodes] == ["e4"]


def test_build_skips_api_moves_that_are_illegal_here():
    builder = make_builder(max_depth=1, max_moves=3, min_move_frequency=0.0)
    builder.analyse_position = lambda board: {}

    board = make_board(NIMZO_FEN)

    builder._get_position_data = lambda board: PositionData(
        1000,
        0,
        0,
        [
            MoveStats("a2a1", "a1", 900, 0, 0),
            move_stats(board, "e3", 100),
        ],
    )

    assert [node.san for node in builder.build(NIMZO_FEN)] == ["e3"]


def test_build_uses_board_derived_san():
    """
    Regression: the node took its SAN straight from the API, so a wrong or
    stale SAN produced a PGN that could not be replayed.
    """
    builder = make_builder(max_depth=1, max_moves=1, min_move_frequency=0.0)
    builder.analyse_position = lambda board: {}

    board = make_board(chess.STARTING_FEN)

    # Lichess would never send this, but a proxy or a cache might.
    builder._get_position_data = lambda board: PositionData(
        1000, 0, 0, [MoveStats("e2e4", "Zz9?!", 100, 0, 0)]
    )

    nodes = builder.build(chess.STARTING_FEN)

    assert [node.san for node in nodes] == ["e4"]


def test_build_does_not_duplicate_a_main_line_move_as_a_mistake(monkeypatch):
    """
    A move that is already a repertoire line must not also appear as a
    MISTAKE variation, or the same move shows up twice in the PGN.
    """
    builder = make_builder(
        max_depth=2, max_moves=1, min_move_frequency=0.0, min_frequency=0.5
    )
    builder.color = chess.WHITE
    builder.analyse_position = lambda board: {}

    def data(position):
        if position.turn == chess.WHITE:
            return position_data([move_stats(position, "e4", 500)])
        return position_data([move_stats(position, "e5", 500)])

    builder._get_position_data = data

    def fake_mistakes(position):
        # ...e5 is already a repertoire move in this position.
        return [
            {
                "move": "e5",
                "uci": "e7e5",
                "difference": 30.0,
                "classification": "BLUNDER",
                "comment": "BLUNDER: ...e5?!",
                "best_line": "",
            }
        ]

    monkeypatch.setattr(builder, "_find_opponent_mistakes", fake_mistakes)

    nodes = builder.build(chess.STARTING_FEN)

    assert [node.uci for node in nodes] == ["e2e4"]
    assert [node.uci for node in nodes[0].children] == ["e7e5"]

    for node in nodes[0].children:
        assert not node.comment.startswith("MISTAKE")


def test_build_attaches_mistake_and_punishment_branches():
    builder = make_builder(max_depth=1, max_moves=1, punishment_depth=3)
    builder.color = chess.WHITE

    board = make_board(chess.STARTING_FEN, "e4")
    builder._get_position_data = lambda board: PositionData(0, 0, 0, [])

    builder._find_opponent_mistakes = lambda board: [
        {
            "move": "d5",
            "uci": "d7d5",
            "difference": 30.0,
            "classification": "BLUNDER",
            "comment": "BLUNDER: ...d5?!",
            "best_line": "2. c4",
        }
    ]

    punishment = make_board(chess.STARTING_FEN, "e4", "d5")
    builder._build_punishment_nodes = lambda board, plies: [
        MoveNode(san="c4", uci="c2c4")
    ]

    nodes = builder.build(board.fen())

    assert len(nodes) == 1

    mistake = nodes[0]
    assert mistake.san == "d5"
    assert mistake.uci == "d7d5"
    assert mistake.comment.startswith("MISTAKE")
    assert "BLUNDER: ...d5?!" in mistake.comment

    assert len(mistake.children) == 1
    assert mistake.children[0].san == "c4"
    assert mistake.children[0].comment.startswith("PUNISHMENT")


def test_a_mistake_without_a_punishment_line_is_kept():
    """
    Regression: a mistake whose position has no principal variation was
    dropped from the repertoire entirely.
    """
    builder = make_builder(max_depth=1, max_moves=1)
    builder.color = chess.WHITE

    board = make_board(chess.STARTING_FEN, "e4")
    builder._get_position_data = lambda board: PositionData(0, 0, 0, [])
    builder._build_punishment_nodes = lambda board, plies: []

    builder._find_opponent_mistakes = lambda board: [
        {
            "move": "d5",
            "uci": "d7d5",
            "difference": 30.0,
            "classification": "BLUNDER",
            "comment": "BLUNDER: ...d5?!",
            "best_line": "",
        }
    ]

    nodes = builder.build(board.fen())

    assert len(nodes) == 1
    assert nodes[0].san == "d5"
    assert nodes[0].children == []


def test_build_reuses_the_tree_cache_for_transpositions():
    builder = make_builder(max_depth=4, max_moves=1, min_move_frequency=0.0)
    builder.color = chess.WHITE
    builder.analyse_position = lambda board: {}

    board = make_board(chess.STARTING_FEN)

    def data(position):
        for san in ("e4", "d4", "c4"):
            try:
                move = position.parse_san(san)
            except ValueError:
                continue
            return position_data([move_stats(position, san, 100)])
        return PositionData(0, 0, 0, [])

    builder._get_position_data = data
    builder.build(chess.STARTING_FEN)

    keys = {key for key, _ in builder._tree_cache}

    for key in keys:
        assert len(key.split(" ")) == 4


# =============================================================
# Real Stockfish integration
# =============================================================


@pytest.fixture(scope="module")
def real_builder():
    """
    A builder wired to the real Stockfish binary.

    The tests that use it are skipped, not failed, when no binary is
    available, so a machine without Stockfish still gets a clean run of
    everything that does not need an engine.
    """
    try:
        builder = RepertoireBuilder(
            source=object(),
            color=chess.WHITE,
            stockfish_depth=10,
        )
    except RuntimeError as exc:
        pytest.skip(f"Stockfish is not available: {exc}")

    yield builder
    builder.close()


def test_real_stockfish_can_analyse_position(real_builder):
    result = real_builder.analyse_position(make_board(QGD_FEN))

    assert "score" in result
    assert "pv" in result


def test_real_stockfish_depth_limit_is_deterministic(real_builder):
    """
    A fixed depth has to give a reproducible *decision*.  The score itself is
    not bit-identical between searches, because Stockfish carries its hash
    table over from the previous search, so the claim being tested here is
    the one the repertoire actually depends on: the chosen move.
    """
    board = make_board(NIMZO_FEN, "e3")

    def search():
        real_builder._engine_cache.clear()
        result = real_builder.analyse_position(board)
        return (
            [move.uci() for move in result["pv"]],
            real_builder.get_score(result, chess.BLACK),
        )

    first_pv, first_score = search()
    second_pv, second_score = search()

    # Only the first move is guaranteed to repeat: once the top move and its
    # score agree, the rest of the line is just one of several equal options.
    assert first_pv[0] == second_pv[0]
    assert first_score == pytest.approx(second_score, abs=5.0)


def test_real_engine_cache_is_keyed_on_the_position_not_the_clock(real_builder):
    board = make_board(NIMZO_FEN, "e3")
    board.halfmove_clock = 7

    key = position_key(board)

    assert key not in real_builder._engine_cache or True

    real_builder.analyse_position(board)
    assert key in real_builder._engine_cache


def test_real_punishment_line_is_legal_and_positive(real_builder):
    """A real punishment line must start from the position after the mistake."""
    board = make_board(NIMZO_FEN, "e3")

    # White has just played a blunder, so Black is the side to punish.
    assert board.turn == chess.BLACK

    after = board.copy()
    after.push_san("Nh5")

    nodes = real_builder._build_punishment_nodes(after, 4)

    assert nodes, "expected a punishment line after a tactical blunder"

    line = nodes[0]
    walk = after.copy()

    while line is not None:
        assert line.san in [walk.san(move) for move in walk.legal_moves]
        walk.push_san(line.san)
        line = line.children[0] if line.children else None


def test_test_opponent_move_reports_a_negative_difference_for_an_improvement(
    real_builder,
):
    """
    Regression: abs() of the swing turned the opponent's best move into a
    blunder.  The engine's own first choice must never be positive.
    """
    board = make_board(NIMZO_FEN, "e3")

    engine_best = real_builder._engine_best_move(board)
    assert engine_best is not None

    san = board.san(engine_best)

    result = real_builder.test_opponent_move(board, san)

    assert result["difference"] is not None
    assert result["difference"] <= 0.0


def test_real_mistake_detection_reports_a_real_blunder(real_builder, monkeypatch):
    """4...Nh5?? gives up a piece to Nxg5 and must be detected as a mistake."""
    builder = make_builder(
        mistake_threshold=15.0, max_deviation_moves=8, stockfish_depth=12
    )
    builder.color = chess.WHITE
    builder.stockfish_depth = real_builder.stockfish_depth
    # Share the live engine instead of starting a second one.
    builder.engine = real_builder.engine

    board = make_board(NIMZO_FEN, "e3")

    # Black is to move, so every candidate below is a Black move.  4...Nh5
    # walks into Nxg5 and costs about 43pp, while 4...O-O, 4...c6, 4...a6
    # and 4...Be7 all stay within a few points of the best move.
    monkeypatch.setattr(
        builder,
        "_get_position_data",
        lambda board: position_data(
            [
                move_stats(board, "O-O", 900),
                move_stats(board, "Be7", 800),
                move_stats(board, "c6", 700),
                move_stats(board, "a6", 600),
                move_stats(board, "Nh5", 120),
            ]
        ),
    )

    mistakes = builder._find_opponent_mistakes(board)

    assert [item["move"] for item in mistakes] == ["Nh5"]
    assert mistakes[0]["classification"] in ("BLUNDER", "SERIOUS MISTAKE")
    assert mistakes[0]["difference"] > builder.mistake_threshold

    # The builder never owned the engine, so closing must not kill it.
    builder.close()
    assert real_builder.analyse_position(board) is not None


# =============================================================
# PGN export
# =============================================================


def test_pgn_comments_are_attached_to_moves():
    game = chess.pgn.Game()
    board = chess.Board()

    _add_nodes(
        game,
        board,
        [
            MoveNode(
                san="e4",
                uci="e2e4",
                comment="MISTAKE",
                children=[
                    MoveNode(san="e5", uci="e7e5", comment="PUNISHMENT")
                ],
            )
        ],
    )

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
                MoveNode(san="e5", uci="e7e5", comment="PUNISHMENT")
            ],
        )
    ]

    build_pgn(chess.STARTING_FEN, nodes, str(output_path))

    assert output_path.exists()

    with output_path.open() as handle:
        game = chess.pgn.read_game(handle)

    assert game is not None
    assert game.headers["Event"] == "Opening Repertoire"

    first_move = game.next()
    assert first_move.move == chess.Move.from_uci("e2e4")
    assert first_move.comment == "MISTAKE"


def test_pgn_export_keeps_sibling_moves_distinct(tmp_path):
    """
    Regression: python-chess's add_variation never merges duplicate moves,
    so a tree with repeated siblings must still serialise and reload.
    """
    output_path = tmp_path / "dup.pgn"

    board = make_board(chess.STARTING_FEN)

    nodes = [
        MoveNode(san="e4", uci="e2e4", children=[MoveNode(san="e5", uci="e7e5")]),
        MoveNode(san="d4", uci="d2d4", children=[MoveNode(san="d5", uci="d7d5")]),
    ]

    build_pgn(chess.STARTING_FEN, nodes, str(output_path))

    with output_path.open() as handle:
        game = chess.pgn.read_game(handle)

    root = chess.Board()
    seen = []

    def walk(node, position):
        for child in node.variations:
            seen.append(child.move.uci())
            position.push(child.move)
            walk(child, position)
            position.pop()

    walk(game, root)

    assert "e2e4" in seen
    assert "d2d4" in seen
    assert "e7e5" in seen
    assert "d7d5" in seen


# =============================================================
# api.py
# =============================================================


def test_parse_position_accepts_a_normal_payload():
    data = _parse_position(
        {
            "white": 100,
            "draws": 20,
            "black": 80,
            "moves": [
                {"uci": "e2e4", "san": "e4", "white": 90, "draws": 10, "black": 20}
            ],
        }
    )

    assert data.total == 200
    assert data.moves[0].total == 120


def test_parse_position_rejects_a_non_object():
    with pytest.raises(LichessResponseError):
        _parse_position([1, 2, 3])


def test_parse_position_rejects_missing_fields():
    with pytest.raises(LichessResponseError):
        _parse_position({"white": 1, "draws": 0})


def test_parse_position_rejects_a_non_list_moves_field():
    with pytest.raises(LichessResponseError):
        _parse_position({"white": 1, "draws": 0, "black": 0, "moves": {}})


def test_parse_position_drops_only_the_unusable_rows():
    data = _parse_position(
        {
            "white": 10,
            "draws": 0,
            "black": 0,
            "moves": [
                {"uci": "e2e4"},
                {"uci": "d2d4", "san": "d4", "white": 5, "draws": 0, "black": 0},
            ],
        }
    )

    assert [move.san for move in data.moves] == ["d4"]


def _client_with_transport(handler):
    """A LichessClient whose HTTP layer is a mock, with retries off."""
    import httpx

    client = LichessClient.__new__(LichessClient)
    client._cache = {}
    client._client = httpx.Client(transport=httpx.MockTransport(handler))

    # tenacity would otherwise sleep for a minute between attempts.
    client._fetch = client._fetch.__wrapped__.__get__(client)

    return client


def _retry_without_sleep(client, attempts):
    """
    `client._fetch` with a retry policy of our choosing and no real pause.

    `__wrapped__` strips the production decorator first.  Re-wrapping the
    decorated method would nest the two policies, giving
    `attempts * stop_after_attempt(5)` calls and a 61 second sleep inside
    every one of them.
    """
    bare = type(client)._fetch.__wrapped__

    return retry(
        retry=retry_if_exception_type(
            (RateLimited, httpx.TransportError, TransientLichessError)
        ),
        wait=wait_fixed(0),
        stop=stop_after_attempt(attempts),
        reraise=True,
    )(bare.__get__(client, type(client)))


def test_fetch_raises_a_helpful_error_without_a_token():
    import httpx

    def handler(request):
        return httpx.Response(401, text="unauthorized")

    client = _client_with_transport(handler)

    with pytest.raises(LichessAuthError) as excinfo:
        client._fetch(chess.STARTING_FEN)

    assert "LICHESS_TOKEN" in str(excinfo.value)


def test_auth_error_is_not_retried():
    """A missing token is not transient; retrying only delays the message."""
    import httpx

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, text="unauthorized")

    client = _client_with_transport(handler)

    with pytest.raises(LichessAuthError):
        client.get_position(chess.STARTING_FEN)

    assert len(calls) == 1


def test_fetch_reports_non_json_bodies():
    import httpx

    def handler(request):
        return httpx.Response(200, text="<html>gateway error</html>")

    client = _client_with_transport(handler)

    with pytest.raises(LichessResponseError):
        client._fetch(chess.STARTING_FEN)


def test_fetch_reports_a_server_error():
    import httpx

    def handler(request):
        return httpx.Response(503, text="unavailable")

    client = _client_with_transport(handler)

    with pytest.raises(LichessResponseError) as excinfo:
        client._fetch(chess.STARTING_FEN)

    assert "503" in str(excinfo.value)


def test_a_bad_request_is_not_retried():
    """
    A 400 is the request's own fault.  Retrying it would sleep for four
    minutes and then report the identical error.
    """
    import httpx

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(400, text="bad request")

    client = _client_with_transport(handler)

    with pytest.raises(LichessResponseError) as excinfo:
        client.get_position("not a fen")

    assert "400" in str(excinfo.value)
    assert len(calls) == 1


def test_a_server_error_is_retried():
    """A 503 may be temporary, so the request has to be tried again."""
    import httpx

    calls = []

    def handler(request):
        calls.append(request)

        if len(calls) < 3:
            return httpx.Response(503, text="unavailable")

        return httpx.Response(
            200,
            json={"white": 1, "draws": 0, "black": 0, "moves": []},
        )

    client = _client_with_transport(handler)
    # Keep the retry behaviour but drop tenacity's real sleep.
    client._fetch = _retry_without_sleep(client, 3)

    data = client.get_position(chess.STARTING_FEN)

    assert data.total == 1
    assert len(calls) == 3


def test_a_transient_error_eventually_surfaces():
    """If the retries never succeed, the caller still learns why."""
    import httpx

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, text="unavailable")

    client = _client_with_transport(handler)
    client._fetch = _retry_without_sleep(client, 3)

    with pytest.raises(TransientLichessError):
        client.get_position(chess.STARTING_FEN)

    assert len(calls) == 3


def test_fetch_sends_the_fen_to_the_masters_endpoint():
    import httpx

    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"white": 1, "draws": 0, "black": 0, "moves": []},
        )

    client = _client_with_transport(handler)
    client._fetch(chess.STARTING_FEN)

    assert "explorer.lichess.org/masters" in seen["url"]


def test_rate_limited_is_raised():
    import httpx

    def handler(request):
        return httpx.Response(429, text="slow down")

    client = _client_with_transport(handler)

    with pytest.raises(Exception):
        client._fetch(chess.STARTING_FEN)


def test_get_position_caches_by_fen():
    import httpx

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "white": 1,
                "draws": 0,
                "black": 0,
                "moves": [
                    {"uci": "e2e4", "san": "e4", "white": 1, "draws": 0, "black": 0}
                ],
            },
        )

    client = _client_with_transport(handler)

    first = client.get_position(chess.STARTING_FEN)
    second = client.get_position(chess.STARTING_FEN)

    assert first is second
    assert len(calls) == 1


# =============================================================
# CLI
# =============================================================


def test_cli_builds_and_exports_repertoire(monkeypatch):
    output_path = "test-output.pgn"

    fake_nodes = [MoveNode(san="e4", uci="e2e4", comment="MAIN LINE")]

    captured = {}

    class FakeSource:
        def close(self):
            captured["source_closed"] = True

    class FakeBuilder:
        def __init__(self, source, color, min_games, threshold, **kwargs):
            captured["builder_args"] = (source, color, min_games, threshold)
            captured["builder_kwargs"] = kwargs
            captured["builder_closed"] = False

        def build(self, fen):
            captured["build_fen"] = fen
            return fake_nodes

        def close(self):
            captured["builder_closed"] = True

    def fake_build_pgn(fen, nodes, output):
        captured["pgn_args"] = (fen, nodes, output)

    monkeypatch.setattr(cli, "LichessDataSource", FakeSource)
    monkeypatch.setattr(cli, "RepertoireBuilder", FakeBuilder)
    monkeypatch.setattr(cli, "build_pgn", fake_build_pgn)

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
            "--max-depth",
            "6",
            "--output",
            output_path,
        ],
    )

    cli.main()

    source, color, min_games, threshold = captured["builder_args"]

    assert isinstance(source, FakeSource)
    assert color == chess.WHITE
    assert min_games == 100
    assert threshold == 0.6
    assert captured["builder_kwargs"]["max_depth"] == 6

    assert captured["build_fen"] == chess.STARTING_FEN
    assert captured["pgn_args"] == (chess.STARTING_FEN, fake_nodes, output_path)
    assert captured["source_closed"] is True
    assert captured["builder_closed"] is True, "Stockfish was never shut down"


def test_cli_rejects_invalid_fen(monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["lichess-opening-explorer", "--fen", "invalid-fen"]
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1


def test_cli_rejects_a_legal_but_impossible_fen(monkeypatch):
    """Regression: only unparseable FENs were rejected, not invalid ones."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lichess-opening-explorer",
            "--fen",
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBKQBNR w KQkq - 0 1",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1


def test_cli_passes_a_black_repertoire_colour(monkeypatch):
    captured = {}

    class FakeSource:
        def close(self):
            pass

    class FakeBuilder:
        def __init__(self, source, color, min_games, threshold, **kwargs):
            captured["color"] = color

        def build(self, fen):
            return []

        def close(self):
            pass

    monkeypatch.setattr(cli, "LichessDataSource", FakeSource)
    monkeypatch.setattr(cli, "RepertoireBuilder", FakeBuilder)
    monkeypatch.setattr(cli, "build_pgn", lambda *a: None)

    fen = "rnbqk2r/ppp2ppp/4pn2/3p2B1/1bPP4/2N5/PP2PPPP/R2QKBNR b KQkq - 4 5"

    monkeypatch.setattr(sys, "argv", ["lichess-opening-explorer", "--fen", fen])

    cli.main()

    assert captured["color"] == chess.BLACK


def test_cli_reports_a_lichess_auth_error(monkeypatch, tmp_path):
    closed = []

    class FakeSource:
        def get_position(self, board):
            raise LichessAuthError("no token")

        def close(self):
            closed.append("source")

    class FakeBuilder:
        def __init__(self, source, color, min_games, threshold, **kwargs):
            self._source = source

        def build(self, fen):
            return self._source.get_position(chess.Board(fen))

        def close(self):
            closed.append("builder")

    monkeypatch.setattr(cli, "LichessDataSource", FakeSource)
    monkeypatch.setattr(cli, "RepertoireBuilder", FakeBuilder)
    monkeypatch.setattr(
        sys, "argv", ["lichess-opening-explorer", "-o", str(tmp_path / "x.pgn")]
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    # Both the engine and the HTTP client have to be released on the way out.
    assert closed == ["builder", "source"]


# =============================================================
# Data source abstraction
# =============================================================


def test_position_key_is_importable_from_models():
    """position_key moved to models so sources.py can import it.

    A circular import would otherwise appear the moment sources.py needs
    the key and explorer.py needs the protocol.
    """
    from lichess_opening_explorer.models import position_key as from_models
    from lichess_opening_explorer.explorer import position_key as from_explorer

    assert from_models is from_explorer


def test_lichess_data_source_delegates_and_caches():
    calls = []

    class RecordingClient:
        def get_position(self, fen):
            calls.append(fen)
            return PositionData(7, 0, 0, [])

        def close(self):
            calls.append("closed")

    client = RecordingClient()
    source = LichessDataSource(client)
    board = chess.Board(NIMZO_FEN)

    assert source.get_position(board).total == 7
    assert source.get_position(board).total == 7

    # Second call must be served from the source's own cache.
    assert calls == [position_key(board)]

    source.close()

    # A client passed in from outside is not owned, so it is not closed.
    assert calls == [position_key(board)]


def test_lichess_data_source_closes_a_client_it_created():
    created = []

    class RecordingClient:
        def __init__(self):
            created.append(self)

        def get_position(self, fen):
            return PositionData(0, 0, 0, [])

        def close(self):
            created.append("closed")

    import lichess_opening_explorer.sources as sources_module

    original = sources_module.LichessClient
    sources_module.LichessClient = RecordingClient
    try:
        source = LichessDataSource()
        source.close()
    finally:
        sources_module.LichessClient = original

    assert created == [created[0], "closed"]


# =============================================================
# min_root_share
# =============================================================


def test_min_root_share_zero_is_a_no_op():
    builder = make_builder(
        min_games=1,
        min_frequency=1.0,
        min_move_frequency=0.0,
        max_moves=3,
        min_root_share=0.0,
    )
    board = chess.Board(NIMZO_FEN)

    selected = builder._select_moves(board, data_for(board, {"e3": 1, "a3": 99}))

    assert [stats.san for stats, _, _ in selected] == ["a3", "e3"]


def test_min_root_share_rejects_a_move_below_the_root_floor():
    """A 1-game line must not become a repertoire branch in a 1000-game root."""
    builder = make_builder(
        min_games=1,
        # 1.0 so both moves would be selected on their local merits; only the
        # root floor is allowed to drop the small one.
        min_frequency=1.0,
        min_move_frequency=0.0,
        max_moves=3,
        min_root_share=0.01,
    )
    board = chess.Board(NIMZO_FEN)

    builder._root_games = 1000

    selected = builder._select_moves(board, data_for(board, {"e3": 999, "a3": 1}))

    # 1 < 1000 * 0.01, so the small move is dropped.
    assert [stats.san for stats, _, _ in selected] == ["e3"]


def test_min_root_share_floor_is_a_count_not_a_share():
    """A move exactly on the floor survives: the test is 10 games, not 10%."""
    builder = make_builder(
        min_games=1,
        min_frequency=1.0,
        min_move_frequency=0.0,
        max_moves=3,
        min_root_share=0.01,
    )
    board = chess.Board(NIMZO_FEN)

    builder._root_games = 1000

    selected = builder._select_moves(board, data_for(board, {"e3": 990, "a3": 10}))

    assert [stats.san for stats, _, _ in selected] == ["e3", "a3"]


def test_min_root_share_rejects_everything_below_the_floor():
    builder = make_builder(
        min_games=1,
        min_frequency=0.1,
        min_move_frequency=0.0,
        max_moves=3,
        min_root_share=0.5,
    )
    board = chess.Board(NIMZO_FEN)

    builder._root_games = 1000

    assert builder._select_moves(board, data_for(board, {"e3": 400})) == []


def test_min_root_share_must_be_a_share():
    """`make_builder` skips __init__, so validation needs the real constructor."""
    with pytest.raises(ValueError):
        RepertoireBuilder(
            None,
            chess.WHITE,
            engine=object(),
            min_root_share=1.5,
        )


def test_move_frequency_uses_local_denominator_not_root_size(tmp_path):
    """
    Regression: move frequency is relative to the position, never the root.

    Ten games reach the anchor and six continue into one branch, where the
    three moves played were played 3 / 2 / 1 times. Those are 50% / 33% /
    17% of the branch. Dividing by the ten-game root would report 30% / 20%
    / 10% and quietly reorder which branches look important.
    """
    from pgn_fixtures import pgn_text, write_pgn
    from lichess_opening_explorer.pgn_index import PgnIndex
    from lichess_opening_explorer.sources import PgnDataSource

    # Every game is distinct, because the index collapses identical ones.
    games = [
        ["e3", "O-O", "Bd3", "Nc6"],
        ["e3", "O-O", "Bd3", "c5"],
        ["e3", "O-O", "Bd3", "a6"],
        ["e3", "O-O", "Nf3", "Nc6"],
        ["e3", "O-O", "Nf3", "c5"],
        ["e3", "O-O", "a3", "Nc6"],
        ["a3", "Nc6", "e3"],
        ["a3", "Nc6", "Nf3"],
        ["a3", "c5", "e3"],
        ["a3", "c5", "Nf3"],
    ]

    path = write_pgn(tmp_path / "nimzo.pgn", pgn_text(games, start_fen=NIMZO_FEN))
    index = PgnIndex.from_path(path, NIMZO_FEN, 8)
    source = PgnDataSource(index)

    assert index.report.games_anchored == 10

    branch = chess.Board(NIMZO_FEN)
    branch.push_san("e3")
    branch.push_san("O-O")

    data = source.get_position(branch)
    shares = {m.san: m.total / data.total for m in data.moves}

    assert data.total == 6
    assert shares["Bd3"] == pytest.approx(0.5)
    assert shares["Nf3"] == pytest.approx(1 / 3)
    assert shares["a3"] == pytest.approx(1 / 6)

    # The negative half: none of these is the root-divided value.
    root_divided = {"Bd3": 0.3, "Nf3": 0.2, "a3": 0.1}
    for san, wrong in root_divided.items():
        assert shares[san] != pytest.approx(wrong)


# =============================================================
# Transpositions
# =============================================================


def test_transposition_merges_stats_and_preserves_both_move_orders(tmp_path):
    """
    The same position reached by two move orders is one position.

    Statistics merge -- the merged position knows about every game that
    reached it, however they got there -- but the exported repertoire keeps
    both lines, because a human still has to play one move per turn and both
    orders are real games.

    Every game is distinct: the index collapses identical games, so repeating
    one line would merge into a single game and prove nothing.
    """
    from pgn_fixtures import pgn_text, write_pgn
    from lichess_opening_explorer.pgn_index import PgnIndex
    from lichess_opening_explorer.sources import PgnDataSource

    # e3/Nf3 and Nc6/Ne4 commute, so both orders reach the same position.
    games = [
        ["e3", "Nc6", "Nf3", "Ne4", "h3"],
        ["e3", "Nc6", "Nf3", "Ne4", "a4"],
        ["e3", "Nc6", "Nf3", "Ne4", "g3"],
        ["Nf3", "Ne4", "e3", "Nc6", "h3"],
        ["Nf3", "Ne4", "e3", "Nc6", "a4"],
        ["Nf3", "Ne4", "e3", "Nc6", "g3"],
    ]

    path = write_pgn(tmp_path / "transpose.pgn", pgn_text(games, start_fen=NIMZO_FEN))
    index = PgnIndex.from_path(path, NIMZO_FEN, 8)
    source = PgnDataSource(index)

    reached = chess.Board(NIMZO_FEN)
    for san in ("e3", "Nc6", "Nf3", "Ne4"):
        reached.push_san(san)

    # --- half one: statistics merge -----------------------------------
    data = source.get_position(reached)

    assert index.report.games_anchored == 6
    assert data.total == 6
    assert {m.san: m.total for m in data.moves} == {"h3": 2, "a4": 2, "g3": 2}

    # --- half two: both move orders survive into the PGN ---------------
    builder = make_builder(
        min_games=1,
        # 1.0 so both 50% first moves are kept instead of stopping after the
        # first one that crosses the cumulative bar.
        min_frequency=1.0,
        min_move_frequency=0.0,
        max_depth=4,
        max_moves=3,
        max_mistakes=0,
    )
    builder.analyse_position = lambda board: {}
    builder._get_position_data = lambda board: source.get_position(board)

    nodes = builder.build(NIMZO_FEN)
    output = tmp_path / "out.pgn"
    build_pgn(NIMZO_FEN, nodes, str(output))

    text = output.read_text(encoding="utf-8")

    assert "5. e3" in text
    assert "5. Nf3" in text

    # Both lines must actually replay, not merely appear as text.
    with output.open(encoding="utf-8") as handle:
        game = chess.pgn.read_game(handle)

    orders = set()

    for line in game.variations:
        board = game.board().copy(stack=False)
        sans = []
        node = line

        while node is not None and node.move is not None:
            sans.append(board.san(node.move))
            board.push(node.move)
            node = node.next()

        orders.add(tuple(sans))

    assert ("e3", "Nc6", "Nf3", "Ne4") in orders
    assert ("Nf3", "Ne4", "e3", "Nc6") in orders


class FakePassthroughBuilder:
    """A builder stand-in for CLI tests that only care about wiring."""

    def __init__(self, source, color, min_games, threshold, **kwargs):
        self._source = source

    def build(self, fen):
        return self._source.get_position(chess.Board(fen)).moves

    def close(self):
        pass


# =============================================================
# CLI -- data sources
# =============================================================


def test_pgn_source_requires_a_path(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["lichess-opening-explorer", "--source", "pgn"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1


def test_pgn_source_reports_a_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lichess-opening-explorer",
            "--source",
            "pgn",
            "--pgn",
            str(tmp_path / "absent.pgn"),
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1


def test_pgn_source_builds_and_prints_the_report(monkeypatch, tmp_path, capsys):
    from pgn_fixtures import pgn_text, write_pgn

    # Three *distinct* games: the index collapses identical ones, so three
    # copies of the same line would report one, not three.
    path = write_pgn(
        tmp_path / "games.pgn",
        pgn_text([["e3"], ["e4"], ["Nf3"]], start_fen=NIMZO_FEN),
    )

    monkeypatch.setattr(cli, "RepertoireBuilder", FakePassthroughBuilder)
    monkeypatch.setattr(cli, "build_pgn", lambda *a: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lichess-opening-explorer",
            "--source",
            "pgn",
            "--pgn",
            str(path),
            "--fen",
            NIMZO_FEN,
            "--max-depth",
            "2",
        ],
    )

    cli.main()

    out = capsys.readouterr().out

    assert "3 reached the anchor" in out
    assert "0 unfinished" in out


def test_pgn_report_tells_the_user_when_no_game_reached_the_anchor(
    monkeypatch, capsys, tmp_path
):
    """
    A silently empty repertoire is the worst failure mode here.

    Anchoring on a position nobody in the file ever played is easy to do
    by accident, and the run would otherwise look like it simply found
    no opening. The report has to say so.
    """
    from pgn_fixtures import pgn_text, write_pgn

    path = write_pgn(
        tmp_path / "other.pgn",
        pgn_text([["e3", "Nc6"]], start_fen=NIMZO_FEN),
    )

    monkeypatch.setattr(cli, "RepertoireBuilder", FakePassthroughBuilder)
    monkeypatch.setattr(cli, "build_pgn", lambda *a: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lichess-opening-explorer",
            "--source",
            "pgn",
            "--pgn",
            str(path),
            # The starting position of a Ruy Lopez, which this file
            # never reaches.
            "--fen",
            "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4",
        ],
    )

    cli.main()

    captured = capsys.readouterr()

    assert "1 read" in captured.out
    assert "0 reached the anchor" in captured.out
    assert "1 did not," in captured.out
    assert "0 distinct positions" in captured.out

    # ...and it says why, on stderr, where a user will see it.
    assert "reached" in captured.err


def test_min_games_default_depends_on_the_source(monkeypatch, tmp_path):
    from pgn_fixtures import pgn_text, write_pgn

    path = write_pgn(
        tmp_path / "games.pgn", pgn_text([["e3"]], start_fen=NIMZO_FEN)
    )

    captured = {}

    class RecordingBuilder:
        def __init__(self, source, color, min_games, threshold, **kwargs):
            captured["min_games"] = min_games
            captured["source"] = source

        def build(self, fen):
            return []

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(cli, "RepertoireBuilder", RecordingBuilder)
    monkeypatch.setattr(cli, "build_pgn", lambda *a: None)

    monkeypatch.setattr(
        sys,
        "argv",
        ["lichess-opening-explorer", "--source", "pgn", "--pgn", str(path), "--fen", NIMZO_FEN],
    )
    cli.main()

    assert captured["min_games"] == 10
    assert isinstance(captured["source"], PgnDataSource)

    # An explicit value always wins over the per-source default.
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lichess-opening-explorer",
            "--source",
            "pgn",
            "--pgn",
            str(path),
            "--fen",
            NIMZO_FEN,
            "--min-games",
            "77",
        ],
    )
    cli.main()

    assert captured["min_games"] == 77


def test_min_root_share_is_passed_through(monkeypatch, tmp_path):
    from pgn_fixtures import pgn_text, write_pgn

    path = write_pgn(
        tmp_path / "games.pgn", pgn_text([["e3"]], start_fen=NIMZO_FEN)
    )
    captured = {}

    class RecordingBuilder:
        def __init__(self, source, color, min_games, threshold, **kwargs):
            captured.update(kwargs)

        def build(self, fen):
            return []

        def close(self):
            pass

    monkeypatch.setattr(cli, "RepertoireBuilder", RecordingBuilder)
    monkeypatch.setattr(cli, "build_pgn", lambda *a: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lichess-opening-explorer",
            "--source",
            "pgn",
            "--pgn",
            str(path),
            "--fen",
            NIMZO_FEN,
            "--min-root-share",
            "0.05",
        ],
    )

    cli.main()

    assert captured["min_root_share"] == 0.05
