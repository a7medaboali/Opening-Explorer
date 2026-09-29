from __future__ import annotations

from pathlib import Path

import chess
import chess.pgn

from .models import MoveNode

STARTING_FEN = chess.STARTING_FEN


def build_pgn(fen: str, nodes: list[MoveNode], output_path: str) -> None:
    game = chess.pgn.Game()

    if fen != STARTING_FEN:
        game.setup(chess.Board(fen))

    game.headers["Event"] = "Opening Repertoire"

    board = chess.Board(fen)
    _add_nodes(game, board, nodes)

    path = Path(output_path)

    with path.open("w") as f:
        print(game, file=f)

    print(f"Written to {path}")


def _add_nodes(
    parent_node: chess.pgn.GameNode,
    board: chess.Board,
    nodes: list[MoveNode],
) -> None:
    for move_node in nodes:
        move = board.parse_san(move_node.san)

        child = parent_node.add_variation(move)

        if move_node.comment:
            child.comment = move_node.comment

        child_board = board.copy()
        child_board.push(move)

        _add_nodes(
            child,
            child_board,
            move_node.children,
        )