from __future__ import annotations

import argparse
import sys

import chess

from .api import LichessClient
from .explorer import RepertoireBuilder
from .pgn_export import build_pgn


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a chess opening repertoire from the Lichess Masters database."
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
        default=50,
        help="Minimum game count threshold for opponent positions (default: 50)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.8,
        help="Cumulative frequency threshold for our moves (default: 0.8)",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=10,
        help="Maximum depth of the repertoire tree (default: 10)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="repertoire.pgn",
        help="Output PGN file path (default: repertoire.pgn)",
    )
    args = parser.parse_args()

    try:
        board = chess.Board(args.fen)
    except ValueError:
        print(f"Invalid FEN: {args.fen}", file=sys.stderr)
        sys.exit(1)

    color = board.turn
    color_name = "White" if color == chess.WHITE else "Black"
    print(f"Building {color_name} repertoire from: {args.fen}")
    print(f"Min games: {args.min_games}, threshold: {args.threshold}")

    client = LichessClient()
    try:
        builder = RepertoireBuilder(
            client,
            color,
            args.min_games,
            args.threshold,
            max_depth=args.max_depth,
        )
        nodes = builder.build(args.fen)
        build_pgn(args.fen, nodes, args.output)
    finally:
        client.close()
        