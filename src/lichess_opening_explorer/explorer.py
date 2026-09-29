from __future__ import annotations

import chess
import chess.engine

from .api import LichessClient
from .models import MoveNode


class RepertoireBuilder:
    def __init__(
        self,
        client: LichessClient,
        color: chess.Color,
        min_games: int = 50,
        threshold: float = 0.10,
        max_depth: int = 10,
        max_moves: int = 3,
        stockfish_time: float = 0.3,
        mistake_threshold: float = 1.0,
        max_deviation_moves: int = 5,
        punishment_depth: int = 6,
    ) -> None:
        self.client = client
        self.color = color

        # Lichess settings
        self.min_games = min_games
        self.min_frequency = threshold

        # Opening tree settings
        self.max_depth = max_depth
        self.max_moves = max_moves

        # Stockfish settings
        self.stockfish_path = (
            r"C:\Stockfish\stockfish-windows-x86-64-universal.exe"
        )
        self.stockfish_time = stockfish_time

        # A Black move must worsen White's evaluation
        # by at least this amount to be considered a mistake.
        self.mistake_threshold = mistake_threshold

        # Maximum number of Black moves we test at each position.
        self.max_deviation_moves = max_deviation_moves

        # Number of plies to continue the punishment line.
        self.punishment_depth = punishment_depth

        # ---------------------------------------------------------
        # Caches
        # ---------------------------------------------------------

        self._position_cache: dict[str, object] = {}

        self._tree_cache: dict[
            tuple[str, int],
            list[MoveNode],
        ] = {}

        self._engine_cache: dict[
            str,
            chess.engine.InfoDict,
        ] = {}

        self._active: set[str] = set()

        # Prevent the same punishment from being repeatedly added
        # to transpositions.
        self._punishment_cache: dict[
            tuple[str, str],
            dict[str, object] | None,
        ] = {}

        # ---------------------------------------------------------
        # Stockfish
        # ---------------------------------------------------------

        self.engine: chess.engine.SimpleEngine | None = (
            chess.engine.SimpleEngine.popen_uci(
                self.stockfish_path
            )
        )

    # =============================================================
    # STOCKFISH
    # =============================================================

    def analyse_position(
        self,
        board: chess.Board,
    ) -> chess.engine.InfoDict:
        """Analyse a position with Stockfish."""

        fen = board.fen()

        if fen in self._engine_cache:
            return self._engine_cache[fen]

        if self.engine is None:
            raise RuntimeError(
                "Stockfish engine is closed."
            )

        result = self.engine.analyse(
            board,
            chess.engine.Limit(
                time=self.stockfish_time
            ),
        )

        self._engine_cache[fen] = result

        return result

    def get_score(
        self,
        info: chess.engine.InfoDict,
        pov: chess.Color = chess.WHITE,
    ) -> float | None:
        """Return Stockfish evaluation in pawns."""

        score = info["score"].pov(pov)

        if score.is_mate():
            return None

        value = score.score()

        if value is None:
            return None

        return value / 100.0

    def get_best_line(
        self,
        board: chess.Board,
        plies: int = 10,
    ) -> str:
        """Return Stockfish's principal variation."""

        info = self.analyse_position(board)

        pv = info.get("pv", [])

        return board.variation_san(
            pv[:plies]
        )

    # =============================================================
    # BLACK MISTAKE DETECTION
    # =============================================================

    def test_black_move(
        self,
        board: chess.Board,
        black_move: str,
    ) -> dict[str, object]:
        """
        Test a Black move.

        The evaluation is always measured from White's perspective.
        """

        if board.turn != chess.BLACK:
            raise ValueError(
                "test_black_move() expects Black to move."
            )

        before_info = self.analyse_position(
            board
        )

        before_score = self.get_score(
            before_info,
            chess.WHITE,
        )

        test_board = board.copy()

        try:
            move = test_board.parse_san(
                black_move
            )
        except ValueError:
            raise ValueError(
                f"Illegal Black move: {black_move}"
            )

        test_board.push(move)

        after_info = self.analyse_position(
            test_board
        )

        after_score = self.get_score(
            after_info,
            chess.WHITE,
        )

        difference: float | None = None

        if (
            before_score is not None
            and after_score is not None
        ):
            difference = (
                after_score
                - before_score
            )

        pv = after_info.get(
            "pv",
            [],
        )

        best_line = test_board.variation_san(
            pv[: self.punishment_depth]
        )

        return {
            "move": black_move,
            "before": before_score,
            "after": after_score,
            "difference": difference,
            "best_line": best_line,
            "board": test_board,
        }

    def _classify_mistake(
        self,
        difference: float,
    ) -> str | None:
        """Classify a Black mistake according to evaluation swing."""

        if difference < self.mistake_threshold:
            return None

        if difference >= 3.0:
            return "BLUNDER"

        if difference >= 1.5:
            return "SERIOUS MISTAKE"

        return "MISTAKE"

    def _make_mistake_comment(
        self,
        result: dict[str, object],
        classification: str,
    ) -> str:
        """Create the PGN comment for a detected mistake."""

        move = str(result["move"])

        before = result["before"]
        after = result["after"]
        difference = result["difference"]

        best_line = str(result["best_line"])

        before_text = (
            "N/A"
            if before is None
            else f"{float(before):+.2f}"
        )

        after_text = (
            "N/A"
            if after is None
            else f"{float(after):+.2f}"
        )

        difference_text = (
            "N/A"
            if difference is None
            else f"{float(difference):+.2f}"
        )

        # Get the first actual move from the punishment line.
        parts = best_line.split()
        punishment = "N/A"

        if parts:
            # Skip move numbers like "6." or "6..."
            for part in parts:
                if not part.endswith(".") and not part.endswith("..."):
                    punishment = part
                    break

        return (
            f"{classification}: ...{move}?!\n"
            f"Evaluation: {before_text} -> {after_text}\n"
            f"Swing: {difference_text}\n"
            f"Best punishment: {punishment.rstrip('!')}!"
        )

    # =============================================================
    # FIND BLACK DEVIATIONS
    # =============================================================

    def _candidate_black_moves(
        self,
        board: chess.Board,
    ) -> list[chess.Move]:
        """
        Return promising Black deviations.

        We use:
        1. Lichess Masters moves
        2. Forcing legal moves (checks and captures)

        This avoids scanning every legal move while still catching
        tactical deviations that may be rare in the database.
        """

        if board.turn != chess.BLACK:
            return []

        data = self._get_position_data(board)

        candidates: list[chess.Move] = []
        seen: set[chess.Move] = set()

        # 1. Lichess Masters moves
        for move_data in data.moves:
            if move_data.total < 2:
                continue

            try:
                move = board.parse_uci(
                    move_data.uci
                )
            except ValueError:
                continue

            if move not in seen:
                candidates.append(move)
                seen.add(move)

            if len(candidates) >= self.max_deviation_moves:
                break

        # 2. Add forcing moves: captures and checks.
        for move in board.legal_moves:
            if move in seen:
                continue

            if (
                board.is_capture(move)
                or board.gives_check(move)
            ):
                candidates.append(move)
                seen.add(move)

        return candidates

    def _find_black_mistakes(
        self,
        board: chess.Board,
    ) -> list[dict[str, object]]:
        """
        Test Black candidate moves and return serious mistakes.
        """

        if board.turn != chess.BLACK:
            return []

        candidates = self._candidate_black_moves(
            board
        )

        mistakes: list[dict[str, object]] = []

        for move in candidates:
            san = board.san(move)

            cache_key = (
                board.fen(),
                san,
            )

            if cache_key in self._punishment_cache:
                result = self._punishment_cache[
                    cache_key
                ]
            else:
                result = self.test_black_move(
                    board,
                    san,
                )

                self._punishment_cache[
                    cache_key
                ] = result

            if result is None:
                continue

            difference = result[
                "difference"
            ]

            if difference is None:
                continue

            mistake_magnitude = abs(float(difference))
            result["difference"] = mistake_magnitude

            classification = (
                self._classify_mistake(
                    mistake_magnitude
                )
            )

            if classification is None:
                continue

            result[
                "classification"
            ] = classification

            result[
                "comment"
            ] = self._make_mistake_comment(
                result,
                classification,
            )

            mistakes.append(result)

        # Biggest mistakes first.
        mistakes.sort(
            key=lambda result: float(
                result["difference"]
            ),
            reverse=True,
        )

        return mistakes

    # =============================================================
    # PUNISHMENT VARIATION
    # =============================================================

    def _build_punishment_nodes(
        self,
        board: chess.Board,
        plies: int,
    ) -> list[MoveNode]:
        """
        Build a Stockfish punishment line.

        This is a single principal variation rather than
        another Lichess repertoire branch.
        """

        if plies <= 0:
            return []

        info = self.analyse_position(
            board
        )

        pv = info.get(
            "pv",
            [],
        )

        if not pv:
            return []

        nodes: list[MoveNode] = []

        current_board = board.copy()

        for move in pv[:plies]:
            san = current_board.san(
                move
            )

            node = MoveNode(
                san=san,
                uci=move.uci(),
                children=[],
                comment="",
            )

            nodes.append(node)

            current_board.push(move)

        # Convert the linear list into a chain.
        for index in range(
            len(nodes) - 1
        ):
            nodes[index].children = [
                nodes[index + 1]
            ]

        return nodes[:1]

    # =============================================================
    # LICHESS
    # =============================================================

    def _get_position_data(
        self,
        board: chess.Board,
    ):
        """Get and cache Lichess Masters data."""

        fen = board.fen()

        if fen in self._position_cache:
            return self._position_cache[fen]

        data = self.client.get_position(
            fen=fen
        )

        self._position_cache[fen] = data

        return data

    def _select_moves(
        self,
        board: chess.Board,
        data,
    ) -> list[tuple[object, float, int]]:
        """
        Select the main Lichess moves.

        These are the actual repertoire branches.
        """

        moves = []

        total_games = (
            getattr(data, "white", 0)
            + getattr(data, "draws", 0)
            + getattr(data, "black", 0)
        )

        if total_games <= 0:
            return moves

        for move in getattr(
            data,
            "moves",
            [],
        ):
            games = (
                move.white
                + move.draws
                + move.black
            )

            frequency = (
                games / total_games
            )

            if games < self.min_games:
                continue

            if frequency < self.min_frequency:
                continue

            moves.append(
                (
                    move,
                    frequency,
                    games,
                )
            )

        moves.sort(
            key=lambda item: item[2],
            reverse=True,
        )

        return moves[: self.max_moves]

    # =============================================================
    # TREE BUILDING
    # =============================================================

    def _build_tree(
        self,
        board: chess.Board,
        depth: int,
    ) -> list[MoveNode]:
        """
        Build the main Lichess repertoire tree.

        At every Black position we additionally scan for
        serious mistakes and attach punishment variations.
        """

        if depth >= self.max_depth:
            return []

        fen = board.fen()

        cache_key = (
            fen,
            depth,
        )

        if cache_key in self._tree_cache:
            return self._tree_cache[
                cache_key
            ]

        if fen in self._active:
            return []

        self._active.add(fen)

        try:
            data = self._get_position_data(
                board
            )

            selected_moves = self._select_moves(
                board,
                data,
            )

            nodes: list[MoveNode] = []

            # -----------------------------------------------------
            # Main Lichess branches
            # -----------------------------------------------------

            for move_data, frequency, games in selected_moves:
                move = board.parse_san(
                    move_data.san
                )

                child_board = board.copy()

                child_board.push(move)

                children = self._build_tree(
                    child_board,
                    depth + 1,
                )

                node = MoveNode(
                    san=move_data.san,
                    uci=move_data.uci,
                    children=children,
                    comment=(
                        "MAIN LINE"
                        if not nodes
                        else "ALTERNATIVE"
                    ),
                )

                nodes.append(node)

            # -----------------------------------------------------
            # At Black's turn, scan deviations.
            # -----------------------------------------------------

            if board.turn == chess.BLACK:
                mistakes = self._find_black_mistakes(
                    board
                )

                for mistake in mistakes:
                    mistake_move = str(
                        mistake["move"]
                    )

                    try:
                        punishment_board = board.copy()

                        black_move = (
                            punishment_board.parse_san(
                                mistake_move
                            )
                        )

                        punishment_board.push(
                            black_move
                        )

                        punishment_nodes = (
                            self._build_punishment_nodes(
                                punishment_board,
                                self.punishment_depth,
                            )
                        )

                        # Add the punishment as an
                        # additional variation.
                        if punishment_nodes:
                            first = punishment_nodes[0]

                            first.comment = (
                                "PUNISHMENT\n"
                                + str(
                                    mistake["comment"]
                                )
                            )

                            punishment_root = MoveNode(
                                san=mistake_move,
                                uci=black_move.uci(),
                                children=[
                                    first
                                ],
                                comment=(
                                    "MISTAKE\n"
                                    + str(
                                        mistake[
                                            "comment"
                                        ]
                                    )
                                ),
                            )

                            # Only add if it is not already
                            # one of the main Lichess moves.
                            if not any(
                                existing.uci
                                == punishment_root.uci
                                for existing in nodes
                            ):
                                nodes.append(
                                    punishment_root
                                )

                    except ValueError:
                        continue

            self._tree_cache[
                cache_key
            ] = nodes

            return nodes

        finally:
            self._active.discard(fen)

    # =============================================================
    # PUBLIC API
    # =============================================================

    def build(
        self,
        fen: str,
    ) -> list[MoveNode]:
        """Build the complete repertoire from a FEN string."""

        board = chess.Board(fen)

        return self._build_tree(
            board,
            depth=0,
        )