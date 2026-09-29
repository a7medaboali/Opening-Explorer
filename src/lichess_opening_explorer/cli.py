from __future__ import annotations

import argparse
import sys
from pathlib import Path

import chess

from .api import LichessAuthError, LichessResponseError
from .explorer import RepertoireBuilder
from .pgn_export import build_pgn
from .pgn_index import PgnIndex
from .sources import LichessDataSource, PgnDataSource

# A local file holds far fewer games than the online database, so a cutoff
# tuned for Lichess would reject almost everything in it.
DEFAULT_MIN_GAMES = {"lichess": 50, "pgn": 10}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a chess opening repertoire from the Lichess Masters database or a local PGN file."
    )
    parser.add_argument(
        "--source",
        choices=("lichess", "pgn"),
        default="lichess",
        help="Where the opening statistics come from (default: lichess)",
    )
    parser.add_argument(
        "--pgn",
        default=None,
        help="Path to a PGN file; required when --source pgn is used",
    )
    parser.add_argument(
        "--fen",
        default=chess.STARTING_FEN,
        help="Starting FEN position (default: standard starting position)",
    )
    parser.add_argument(
        "-n",
        "--min-games",
        type=int,
        default=None,
        help=(
            "Stop exploring a position played fewer than this many games "
            "(default: 50 from Lichess, 10 from a PGN file)"
        ),
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.8,
        help="Cumulative share of a position's games the repertoire moves should cover (default: 0.8)",
    )
    parser.add_argument(
        "--min-move-frequency",
        type=float,
        default=0.05,
        help="Minimum share of a position's games for a single repertoire move (default: 0.05)",
    )
    parser.add_argument(
        "--min-opponent-frequency",
        type=float,
        default=0.001,
        help=(
            "Minimum share of a position's games for an opponent deviation "
            "to be analysed. Kept low on purpose, because a real blunder is "
            "often rare (default: 0.001)"
        ),
    )
    parser.add_argument(
        "--min-root-share",
        type=float,
        default=0.0,
        help=(
            "Optional floor on a move's share of the games that reached the "
            "starting position, to reject lines that are locally frequent but "
            "meanless in the database as a whole (default: 0.0, disabled)"
        ),
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=10,
        help="Maximum depth of the repertoire tree in plies (default: 10)",
    )
    parser.add_argument(
        "--max-moves",
        type=int,
        default=3,
        help="Maximum repertoire moves per position (default: 3)",
    )
    parser.add_argument(
        "--max-mistakes",
        type=int,
        default=2,
        help="Maximum opponent mistakes per position (default: 2)",
    )
    parser.add_argument(
        "--max-deviations",
        type=int,
        default=6,
        help="Maximum opponent moves analysed per position (default: 6)",
    )
    parser.add_argument(
        "--mistake-threshold",
        type=float,
        default=12.0,
        help="Win percentage the opponent must give away before a move counts as a mistake (default: 12)",
    )
    parser.add_argument(
        "--punishment-depth",
        type=int,
        default=6,
        help="Plies kept in a punishment line (default: 6)",
    )
    parser.add_argument(
        "--engine-depth",
        type=int,
        default=14,
        help="Stockfish search depth; fixed depth keeps results reproducible (default: 14)",
    )
    parser.add_argument(
        "--stockfish",
        default=None,
        help="Path to the Stockfish executable (default: $LICHESS_STOCKFISH or auto-detect)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="repertoire.pgn",
        help="Output PGN file path (default: repertoire.pgn)",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        board = chess.Board(args.fen)
    except ValueError:
        print(f"Invalid FEN: {args.fen}", file=sys.stderr)
        sys.exit(1)

    if not board.is_valid():
        print(
            f"Invalid FEN: {args.fen}\n{board.status()!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    color = board.turn
    color_name = "White" if color == chess.WHITE else "Black"
    opponent_name = "Black" if color == chess.WHITE else "White"

    if args.source == "pgn":
        if args.pgn is None:
            print("--source pgn requires --pgn PATH", file=sys.stderr)
            sys.exit(1)

        pgn_path = Path(args.pgn)

        if not pgn_path.is_file():
            print(f"PGN file not found: {pgn_path}", file=sys.stderr)
            sys.exit(1)

        source_label = str(pgn_path)
    else:
        source_label = "the Lichess Masters database"

    min_games = (
        args.min_games
        if args.min_games is not None
        else DEFAULT_MIN_GAMES[args.source]
    )

    print(f"Building {color_name} repertoire from: {source_label}")
    print(f"Scanning {opponent_name} deviations and generating punishments.")
    print(
        f"Min games: {min_games}, "
        f"cumulative threshold: {args.threshold}, "
        f"min move frequency: {args.min_move_frequency}, "
        f"min opponent frequency: {args.min_opponent_frequency}"
    )

    if args.source == "pgn":
        index = PgnIndex.from_path(pgn_path, args.fen, args.max_depth)
        source = PgnDataSource(index)
        source.check_depth(args.max_depth)
    else:
        source = LichessDataSource()

    try:
        builder = RepertoireBuilder(
            source,
            color,
            min_games,
            args.threshold,
            max_depth=args.max_depth,
            max_moves=args.max_moves,
            stockfish_path=args.stockfish,
            stockfish_depth=args.engine_depth,
            mistake_threshold=args.mistake_threshold,
            max_deviation_moves=args.max_deviations,
            punishment_depth=args.punishment_depth,
            min_move_frequency=args.min_move_frequency,
            min_opponent_frequency=args.min_opponent_frequency,
            min_root_share=args.min_root_share,
            max_mistakes=args.max_mistakes,
        )

        try:
            nodes = builder.build(args.fen)
            build_pgn(args.fen, nodes, args.output)
        finally:
            builder.close()
    except (LichessAuthError, LichessResponseError) as exc:
        print(f"Lichess error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        source.close()

    if args.source == "pgn":
        report = index.report

        print(f"PGN report: {report.summary()}")

        # An empty repertoire because the anchor was never played is a silent,
        # convincing failure. Say it plainly.
        if report.games_anchored == 0:
            print(
                f"No game in {pgn_path} reached {args.fen}. "
                "Check --fen against the position you meant to anchor on.",
                file=sys.stderr,
            )
