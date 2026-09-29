import chess
import pytest
from pgn_fixtures import games_from_tree, pgn_text, write_pgn

from lichess_opening_explorer.models import position_key
from lichess_opening_explorer.pgn_index import PgnIndex, _fingerprint

NIMZO_FEN = (
    "rnbqk2r/ppp2ppp/4pn2/3p2B1/"
    "1bPP4/2N5/PP2PPPP/R2QKBNR w KQkq - 4 5"
)


def build(
    tmp_path,
    games,
    anchor_fen=NIMZO_FEN,
    max_depth=8,
    start_fen=NIMZO_FEN,
    result="1-0",
    filename="games.pgn",
):
    text = pgn_text(games, start_fen=start_fen, result=result)
    path = write_pgn(tmp_path / filename, text)
    return PgnIndex.from_path(path, anchor_fen, max_depth)


def totals(index, *sans, anchor_fen=NIMZO_FEN):
    board = chess.Board(anchor_fen)
    for san in sans:
        board.push_san(san)
    data = index.get_position(board)
    return data.total, {m.san: m.total for m in data.moves}


# --- parsing and frequencies -------------------------------------


def test_index_counts_the_moves_played_in_a_position(tmp_path):
    # Games are distinct below the anchor, because two games whose whole
    # mainline matches are one game to the index, not two.
    index = build(tmp_path, [["e3", "Nc6"], ["e3", "e5"], ["a3", "c5"]])

    assert totals(index) == (3, {"e3": 2, "a3": 1})


def test_results_are_attributed_to_the_moves_that_were_played(tmp_path):
    """`MoveStats` carries Lichess's meaning, so `performance()` stays honest."""
    index = build(
        tmp_path,
        [
            (["e3", "Nc6"], "1-0"),
            (["e3", "e5"], "0-1"),
            (["a3", "c5"], "1/2-1/2"),
        ],
    )

    board = chess.Board(NIMZO_FEN)
    data = index.get_position(board)
    by_san = {m.san: m for m in data.moves}

    assert (data.white, data.black, data.draws) == (1, 1, 1)
    assert (by_san["e3"].white, by_san["e3"].black, by_san["e3"].draws) == (1, 1, 0)
    assert (by_san["a3"].white, by_san["a3"].black, by_san["a3"].draws) == (0, 0, 1)
    assert by_san["e3"].performance() == pytest.approx(0.5)


def test_a_game_starting_from_its_own_fen_is_replayed_from_that_root(tmp_path):
    """A game carrying its own [FEN] must not be replayed from move one."""
    index = build(
        tmp_path,
        [["Nf3", "e5"], ["Nf3", "c5"]],
        start_fen=NIMZO_FEN,
    )

    total, moves = totals(index, "Nf3")

    assert total == 2
    assert moves == {"e5": 1, "c5": 1}


def test_move_frequency_is_relative_to_the_position_not_the_file(tmp_path):
    """A branch reached by 1 of 2 games reports 100% there, 50% at the anchor."""
    index = build(tmp_path, [["e3", "Nc6"], ["a3", "c5"]])

    branch_total, branch_moves = totals(index, "e3")

    assert branch_total == 1
    assert branch_moves == {"Nc6": 1}
    assert branch_moves["Nc6"] / branch_total == 1.0

    root_total, root_moves = totals(index)

    assert root_total == 2
    assert root_moves["e3"] / root_total == pytest.approx(1 / 2)


def test_moves_are_ordered_by_games_played_descending(tmp_path):
    """The builder keeps the first qualifying deviations, so order matters."""
    index = build(
        tmp_path,
        [
            ["e3", "Nc6"],
            ["e3", "e5"],
            ["e3", "c5"],
            ["a3", "Nc6"],
            ["a3", "c5"],
            ["Nf3", "e5"],
        ],
    )

    _, moves = totals(index)

    assert list(moves) == ["e3", "a3", "Nf3"]


def test_games_starting_at_the_anchor_are_all_anchored(tmp_path):
    index = build(tmp_path, [["e3", "O-O", "Bd3"], ["e3"]])

    root_total, _ = totals(index)

    assert root_total == 2
    assert index.report.games_anchored == 2
    assert index.report.games_without_anchor == 0
    assert index.report.games_read == 2


def test_games_from_a_different_opening_are_counted_as_missing(tmp_path):
    games = [["e4", "e5"], ["d4", "d5"]]

    index = build(tmp_path, games, start_fen=chess.STARTING_FEN, anchor_fen=NIMZO_FEN)

    assert index.report.games_read == 2
    assert index.report.games_anchored == 0
    assert index.report.games_without_anchor == 2
    assert totals(index) == (0, {})


def test_anchor_is_matched_at_a_different_move_number(tmp_path):
    """A hand-typed FEN rarely carries the games' fullmove number."""
    games = [["e4", "e5", "Nf3", "Nc6", "Bb5"], ["e4", "e5", "Nf3"]]

    reached = chess.Board(chess.STARTING_FEN)
    for san in ["e4", "e5", "Nf3"]:
        reached.push_san(san)

    # Same board, same side to move, same rights, but a fullmove number no
    # game will ever carry. Only the first four FEN fields are identity.
    fields = reached.fen().split()
    same_position_later_move = " ".join(fields[:5] + ["99"])

    index = build(
        tmp_path,
        games,
        start_fen=chess.STARTING_FEN,
        anchor_fen=same_position_later_move,
    )

    assert index.report.games_anchored == 2
    assert totals(index, anchor_fen=reached.fen()) == (2, {"Nc6": 1})


# --- depth and windowing -----------------------------------------


def test_index_stops_recording_at_max_depth(tmp_path):
    """
    The window is `max_depth` plies deep.

    Moves are recorded for those plies, so the position at depth 2 knows
    the move played from it. The position one ply past the last recorded
    ply still counts the games that arrived, and nothing beyond it does.
    """
    index = build(tmp_path, [["e3", "O-O", "Bd3", "c5", "Nf3", "a6"]], max_depth=3)

    assert totals(index, "e3", "O-O") == (1, {"Bd3": 1})
    assert totals(index, "e3", "O-O", "Bd3")[0] == 1
    assert totals(index, "e3", "O-O", "Bd3", "c5")[0] == 0


def test_covers_reports_whether_the_window_is_deep_enough(tmp_path):
    index = build(tmp_path, [["e3"]], max_depth=4)

    assert index.covers(4) is True
    assert index.covers(5) is False


# --- transpositions ----------------------------------------------


def test_two_move_orders_reaching_one_position_merge(tmp_path):
    """`e3 Ne4 Nf3 c5` and `Nf3 c5 e3 Ne4` are one position, reached two ways."""
    # The games diverge only after the shared position, so all five are
    # distinct games that still land on the same one.
    index = build(
        tmp_path,
        [
            ["e3", "Ne4", "Nf3", "c5", "h3", "Nc6"],
            ["e3", "Ne4", "Nf3", "c5", "a4", "Nc6"],
            ["e3", "Ne4", "Nf3", "c5", "g3", "Nc6"],
            ["Nf3", "c5", "e3", "Ne4", "h3", "Nc6"],
            ["Nf3", "c5", "e3", "Ne4", "a4", "Nc6"],
        ],
    )

    total, moves = totals(index, "e3", "Ne4", "Nf3", "c5")

    assert total == 5
    assert moves == {"h3": 2, "a4": 2, "g3": 1}
    assert totals(index, "Nf3", "c5", "e3", "Ne4") == (5, {"h3": 2, "a4": 2, "g3": 1})


# --- filtering and error handling --------------------------------


def test_duplicate_games_are_removed_but_divergent_ones_are_kept(tmp_path):
    index = build(tmp_path, [["e3", "O-O"], ["e3", "O-O"], ["e3", "Be7"]])

    root_total, root_moves = totals(index)

    assert root_total == 2
    assert root_moves == {"e3": 2}
    assert index.report.duplicates_removed == 1
    assert totals(index, "e3") == (2, {"O-O": 1, "Be7": 1})


def test_games_with_errors_are_skipped_and_the_file_continues(tmp_path):
    good = pgn_text([["e3"]], start_fen=NIMZO_FEN)
    # A junk movetext token is skipped silently by the parser, so the game
    # has to be broken in a header to be seen as broken at all.
    broken = (
        "[Event \"Broken\"]\n"
        "[Result \"1-0\"]\n"
        "[SetUp \"1\"]\n"
        "[FEN \"not a fen at all\"]\n\n"
        "5. e3 *\n\n"
    )
    path = write_pgn(tmp_path / "games.pgn", broken + good)

    index = PgnIndex.from_path(path, NIMZO_FEN, 8)

    assert index.report.games_malformed == 1
    assert totals(index) == (1, {"e3": 1})


def test_unfinished_games_are_excluded_and_counted(tmp_path):
    index = build(tmp_path, [(["e3"], "1-0"), (["a3"], "*"), (["Nf3"], "*")])

    root_total, _ = totals(index)

    assert root_total == 1
    assert index.report.games_unfinished == 2
    assert index.report.games_read == 1
    assert "2 unfinished" in index.report.summary()


def test_empty_file_yields_an_empty_index(tmp_path):
    path = write_pgn(tmp_path / "empty.pgn", "")

    index = PgnIndex.from_path(path, NIMZO_FEN, 8)

    assert index.report.games_read == 0
    assert totals(index) == (0, {})


def test_a_file_that_ends_mid_game_still_indexes_the_partial_game(tmp_path):
    """Real databases are routinely truncated. The partial game is usable."""
    text = pgn_text([["e3", "O-O"]], start_fen=NIMZO_FEN) + "[Event \"Cut\"]\n\n5. a3"
    path = write_pgn(tmp_path / "games.pgn", text)

    index = PgnIndex.from_path(path, NIMZO_FEN, 8)

    assert "e3" in totals(index)[1]
    assert index.report.games_read >= 1


def test_a_non_utf8_pgn_does_not_raise(tmp_path):
    """Old opening databases are frequently Latin-1."""
    path = tmp_path / "latin1.pgn"
    path.write_bytes(
        "[Event \"Turnier\"]\n[White \"Keres\"]\n[Result \"1-0\"]\n"
        "[SetUp \"1\"]\n"
        f"[FEN \"{NIMZO_FEN}\"]\n\n"
        "5. e3 *\n".encode("latin-1")
    )

    index = PgnIndex.from_path(path, NIMZO_FEN, 8)

    assert totals(index) == (1, {"e3": 1})


def write_png_variation(tmp_path):
    text = (
        "[Event \"Annotated\"]\n[Result \"1-0\"]\n[SetUp \"1\"]\n"
        f"[FEN \"{NIMZO_FEN}\"]\n\n"
        "5. e3 (5. a3 $1 Bxc3) 5... O-O *\n"
    )
    return write_pgn(tmp_path / "annotated.pgn", text)


def test_side_variations_are_not_counted_as_played(tmp_path):
    """A RAV is analysis, not evidence that anyone played the line."""
    path = write_png_variation(tmp_path)

    index = PgnIndex.from_path(path, NIMZO_FEN, 8)

    _, moves = totals(index)

    assert "e3" in moves
    assert "a3" not in moves


def test_a_position_revisited_in_one_game_is_not_counted_twice(tmp_path):
    """
    A shuffle returns to the anchor; the game must stay worth one game.

    `Nf3 Ng4 Ng1 Nf6 Nf3` is a legal five-ply cycle. The fourth ply returns
    to the anchor, so `Nf3` is played from the anchor twice inside a single
    game and the denominator must not grow with it.
    """
    index = build(tmp_path, [["Nf3", "Ng4", "Ng1", "Nf6", "Nf3"]])

    total, moves = totals(index)

    assert index.report.games_anchored == 1

    # One game means a denominator of 1, and the move played on the way back
    # is the same game rather than a second one, so the numerators still sum
    # to the denominator.
    assert total == 1
    assert moves == {"Nf3": 1}
    assert sum(moves.values()) == total


def test_a_game_ending_at_the_anchor_counts_but_adds_no_moves(tmp_path):
    """It joins the denominator and adds no numerator, so shares fall short."""
    index = build(tmp_path, [["e3", "O-O"], []])

    total, moves = totals(index)

    assert total == 2
    assert moves == {"e3": 1}
    assert sum(moves.values()) < total


def test_report_totals_are_internally_consistent(tmp_path):
    index = build(
        tmp_path,
        [
            ["e3", "Nc6"],
            ["e3", "e5"],
            ["e3", "c5"],
            ["a3", "Nc6"],
            ["a3", "c5"],
        ],
    )

    report = index.report

    assert report.games_read == 5
    assert report.games_read == report.games_anchored + report.games_without_anchor
    assert report.positions_indexed == len(index._positions)
    assert report.plies_recorded == 10


# --- the fast anchor filter and the key cache -------------------
#
# `PgnIndex` tests positions against a cheap fingerprint instead of building
# a FEN for every ply, because building a FEN was the bulk of the time a
# large file took to index.  These tests pin the shortcut to the behaviour it
# replaced: a fingerprint hit must always be confirmed against the real key,
# so the fast path can never change which games anchor or which positions
# merge.

import chess.pgn as _pgn


def _walk_all_positions(path):
    """Every position of every game in `path`, as boards."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        while True:
            game = _pgn.read_game(handle)

            if game is None:
                return

            board = game.board()
            yield board

            for move in game.mainline_moves():
                board.push(move)
                yield board


WALK_GAMES = [
    ["e3", "Nc6", "f4"],
    ["e3", "e5", "Nf3"],
    ["a3", "c5", "Nf3"],
    ["a3", "Nc6", "Nf3"],
    ["a3", "e5", "Nf3"],
    ["e3", "Nc6", "Nf3"],
]


def _index_and_path(tmp_path, games, max_depth=6, filename="walk.pgn"):
    text = pgn_text(games, start_fen=NIMZO_FEN, result="1-0")
    path = write_pgn(tmp_path / filename, text)
    return PgnIndex.from_path(path, NIMZO_FEN, max_depth), path


def test_the_fast_anchor_filter_agrees_with_the_position_key(tmp_path):
    index, path = _index_and_path(tmp_path, WALK_GAMES)

    seen_anchor = 0

    for board in _walk_all_positions(path):
        expected = position_key(board) == index.anchor_key
        assert index._is_anchor(board) is expected

        if expected:
            seen_anchor += 1

    assert seen_anchor > 0, "the filter must still recognise the real anchor"


def test_the_key_cache_returns_exactly_the_position_key(tmp_path):
    index, path = _index_and_path(tmp_path, WALK_GAMES)

    for board in _walk_all_positions(path):
        # Called twice so that the second call is served from the cache.
        assert index._key_of(board) == position_key(board)
        assert index._key_of(board) == position_key(board)


def test_a_fingerprint_hit_is_confirmed_against_the_real_key():
    # Two positions with identical piece placement, side to move, and
    # castling rights, where one has a *legal* en passant capture and the
    # other does not.  A fingerprint that ignored the en passant square would
    # call these one position, so the fingerprint has to carry it, and a
    # fingerprint hit still has to be confirmed against the real key.
    with_ep = chess.Board("4k1nr/8/8/8/3pP3/8/8/4K3 b - e3 0 1")
    without_ep = chess.Board("4k1nr/8/8/8/3pP3/8/8/4K3 b - - 0 1")

    assert with_ep.board_fen() == without_ep.board_fen()
    assert with_ep.turn == without_ep.turn
    assert with_ep.castling_rights == without_ep.castling_rights
    assert with_ep.has_legal_en_passant()
    assert not without_ep.has_legal_en_passant()

    # The cheap fingerprint and the real key agree that these differ.
    assert _fingerprint(with_ep) != _fingerprint(without_ep)
    assert position_key(with_ep) != position_key(without_ep)

    # And the anchor test takes the anchor, not its twin.
    index = PgnIndex(with_ep, max_depth=4)
    assert index._is_anchor(with_ep) is True
    assert index._is_anchor(without_ep) is False
    assert index._key_of(with_ep) == position_key(with_ep)
    assert index._key_of(without_ep) == position_key(without_ep)



