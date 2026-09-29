"""PGN text generation for tests.

Fixtures are built as strings rather than committed as `.pgn` files so that
a test can state exactly which positions and results it needs, and so a
truncated or badly encoded file is a two-line change rather than a binary
one.
"""

from __future__ import annotations

from pathlib import Path

import chess
import chess.pgn


def pgn_text(
    games: list,
    start_fen: str = chess.STARTING_FEN,
    result: str = "1-0",
) -> str:
    """Render PGN text for a list of games.

    `games` is a list of SAN move lists. An entry may instead be a
    `(sans, result)` tuple when a test needs per-game results.
    """
    records = []

    for entry in games:
        if isinstance(entry, tuple):
            records.append(entry)
        else:
            records.append((entry, result))

    blocks = []

    for index, (sans, game_result) in enumerate(records):
        game = chess.pgn.Game()
        game.headers["Event"] = f"Test {index}"
        game.headers["Result"] = game_result

        board = chess.Board(start_fen)

        if start_fen != chess.STARTING_FEN:
            game.setup(board)

        node = game

        for san in sans:
            move = board.parse_san(san)
            node = node.add_variation(move)
            board.push(move)

        blocks.append(str(game))

    return "\n\n".join(blocks) + "\n"


def games_from_tree(
    anchor_fen: str,
    tree: dict,
) -> list[list[str]]:
    """Expand a nested move tree into a list of games.

        games_from_tree(fen, {"e3": {"O-O": {}, "Be7": {}}, "a3": {}})

    returns three games: `e3 O-O`, `e3 Be7`, and `a3`.

    A leaf is an empty dict or list. Repeating a move cannot express "this
    many games": the index collapses games whose whole mainline is
    identical, so five copies of one line are one game, not five. Vary a
    later move to make games distinct, and count frequencies in the
    assertions instead of in the fixture.
    """
    games: list[list[str]] = []

    def walk(prefix: list[str], node: dict) -> None:
        for san, child in node.items():
            line = prefix + [san]

            if isinstance(child, int):
                raise ValueError(
                    f"Cannot repeat {san!r} {child} times: identical games are "
                    "deduplicated by the index, so repeats would count once. "
                    "Give the leaf a distinct continuation instead."
                )

            if isinstance(child, (dict, list)):
                if not child:
                    games.append(list(line))
                else:
                    walk(line, child)
            else:
                games.append(list(line))

    walk([], tree)

    return games


def write_pgn(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path
