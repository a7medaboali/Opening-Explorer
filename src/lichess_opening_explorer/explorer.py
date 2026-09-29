from __future__ import annotations

import os
from pathlib import Path

import chess
import chess.engine

from .api import LichessClient
from .evaluation import (
    classify_mistake,
    format_score,
    mate_distance,
    score_to_centipawns,
    win_probability,
)
from .models import MoveNode, PositionData, position_key
from .sources import DataSource

MAIN_LINE = "MAIN LINE"
ALTERNATIVE = "ALTERNATIVE"
MISTAKE = "MISTAKE"
PUNISHMENT = "PUNISHMENT"


class RepertoireBuilder:
    def __init__(
        self,
        source: DataSource,
        color: chess.Color,
        min_games: int = 50,
        threshold: float = 0.10,
        max_depth: int = 10,
        max_moves: int = 3,
        stockfish_path: str | None = None,
        stockfish_depth: int = 14,
        mistake_threshold: float = 12.0,
        max_deviation_moves: int = 6,
        punishment_depth: int = 6,
        min_move_frequency: float = 0.05,
        min_opponent_frequency: float = 0.001,
        min_root_share: float = 0.0,
        max_mistakes: int = 2,
        engine: chess.engine.SimpleEngine | None = None,
    ) -> None:
        """
        Build an opening repertoire for `color`.

        `color` is the side the human plays.  Every other side to move in
        the tree is the opponent, and only the opponent gets deviation
        and punishment branches.

        Lichess settings
            min_games           stop exploring a position played fewer
                                than this many games.  This is a position
                                level cutoff, not a per-move filter, so it
                                scales with how deep the tree goes.
            threshold           cumulative share of the position's games
                                the repertoire moves should cover.
            min_move_frequency  a single repertoire move must cover at
                                least this share of the position.
            min_opponent_frequency
                                a deviation must cover at least this share
                                of the position's games to be treated as
                                something the opponent will really play.
                                The default is deliberately low: a blunder
                                is often rare, and the point is to punish
                                it.  The filter exists to drop one- and
                                two-game curiosities in a position with
                                tens of thousands of games, not to
                                suppress genuine mistakes.
            min_root_share      optional root-relative floor: a repertoire
                                move must represent at least this share of
                                the games that reached the starting
                                position. Disabled at 0.0. It exists to
                                reject a line that is statistically
                                meaningless in the database as a whole —
                                three games at depth eight out of a
                                thousand — even when it clears every local
                                test. It is deliberately not applied to
                                opponent deviations, where a rare move is
                                the interesting case.

        Opening tree settings
            max_depth           maximum plies of the repertoire tree.
            max_moves           maximum repertoire moves per position.
            max_mistakes        maximum opponent mistakes per position.

        Stockfish settings
            stockfish_depth     fixed search depth.  A depth limit makes
                                the analysis reproducible; a time limit
                                makes the repertoire depend on machine
                                speed and load.
            mistake_threshold   expected score, in win percentage points,
                                the opponent must throw away before a move
                                counts as a mistake.
            max_deviation_moves maximum opponent moves analysed.
            punishment_depth    plies kept in a punishment line.
        """

        if mistake_threshold <= 0:
            raise ValueError("mistake_threshold must be greater than zero.")

        if not 0.0 <= min_root_share <= 1.0:
            raise ValueError("min_root_share must be between 0.0 and 1.0.")

        self.source = source
        self.color = color

        # Lichess settings
        self.min_games = min_games
        self.min_frequency = threshold
        self.min_move_frequency = min_move_frequency
        self.min_opponent_frequency = min_opponent_frequency
        self.min_root_share = min_root_share

        # Opening tree settings
        self.max_depth = max_depth
        self.max_moves = max_moves
        self.max_mistakes = max_mistakes

        # Stockfish settings
        self.stockfish_depth = stockfish_depth
        self.mistake_threshold = mistake_threshold
        self.max_deviation_moves = max_deviation_moves
        self.punishment_depth = punishment_depth

        # ---------------------------------------------------------
        # Caches
        # ---------------------------------------------------------

        self._tree_cache: dict[tuple[str, int], list[MoveNode]] = {}
        self._root_games = 0

        self._engine_cache: dict[str, chess.engine.InfoDict] = {}

        self._active: set[str] = set()

        # ---------------------------------------------------------
        # Stockfish
        # ---------------------------------------------------------

        self._owns_engine = engine is None
        self.engine: chess.engine.SimpleEngine | None = (
            engine if engine is not None else self._open_engine(stockfish_path)
        )

    @staticmethod
    def _open_engine(
        stockfish_path: str | None,
    ) -> chess.engine.SimpleEngine:
        """Start Stockfish from an explicit path, the environment, or PATH."""
        candidates = [
            stockfish_path,
            os.environ.get("LICHESS_STOCKFISH"),
            os.environ.get("STOCKFISH_PATH"),
        ]

        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return chess.engine.SimpleEngine.popen_uci(candidate)

        # No auto-detection helper exists in python-chess, so fall back to
        # whatever is on PATH.
        try:
            return chess.engine.SimpleEngine.popen_uci("stockfish")
        except Exception as exc:
            raise RuntimeError(
                "Could not start Stockfish. Pass --stockfish PATH or set the "
                "LICHESS_STOCKFISH environment variable."
            ) from exc

    def close(self) -> None:
        """Shut down the engine started by this builder."""
        if self.engine is not None and self._owns_engine:
            self.engine.quit()

        self.engine = None

    def __enter__(self) -> RepertoireBuilder:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # =============================================================
    # STOCKFISH
    # =============================================================

    def analyse_position(
        self,
        board: chess.Board,
    ) -> chess.engine.InfoDict:
        """Analyse a position with Stockfish at a fixed depth."""
        key = position_key(board)

        if key in self._engine_cache:
            return self._engine_cache[key]

        if self.engine is None:
            raise RuntimeError("Stockfish engine is closed.")

        result = self.engine.analyse(
            board,
            chess.engine.Limit(depth=self.stockfish_depth),
        )

        self._engine_cache[key] = result

        return result

    def get_score(
        self,
        info: chess.engine.InfoDict,
        pov: chess.Color = chess.WHITE,
    ) -> float | None:
        """Return the evaluation seen from `pov`, in win percentage."""
        centipawns = score_to_centipawns(info, pov)

        if centipawns is None:
            return None

        return win_probability(centipawns)

    def get_best_line(
        self,
        board: chess.Board,
        plies: int = 10,
    ) -> str:
        """Return Stockfish's principal variation."""
        info = self.analyse_position(board)

        return board.variation_san(info.get("pv", [])[:plies])

    def _win_percent_after(
        self,
        board: chess.Board,
        move: chess.Move,
    ) -> float | None:
        """Expected score for the side that plays `move`, after playing it."""
        child = board.copy(stack=False)
        child.push(move)

        return self.get_score(
            self.analyse_position(child),
            board.turn,
        )

    def _engine_best_move(
        self,
        board: chess.Board,
    ) -> chess.Move | None:
        info = self.analyse_position(board)
        pv = info.get("pv", [])

        return pv[0] if pv else None

    # =============================================================
    # OPPONENT MISTAKES
    # =============================================================

    def test_opponent_move(
        self,
        board: chess.Board,
        opponent_move: str,
    ) -> dict[str, object]:
        """Evaluate a single opponent move.

        `difference` is how much expected score the opponent gave up
        against the engine's evaluation of the position as it stands.  It
        is positive when the move is worse and negative when the move is
        better than that baseline, so a negative difference is never a
        mistake.
        """
        if board.turn == self.color:
            raise ValueError(
                "test_opponent_move() expects the opponent to move."
            )

        try:
            move = board.parse_san(opponent_move)
        except ValueError:
            raise ValueError(
                f"Illegal opponent move: {opponent_move}"
            )

        return self._evaluate_move(board, move)

    def _evaluate_move(
        self,
        board: chess.Board,
        move: chess.Move,
        best: chess.Move | None = None,
    ) -> dict[str, object]:
        """
        Evaluate one opponent move.

        The baseline is `best` if given, otherwise the position itself.
        Both evaluations are read from the opponent's point of view, so
        `difference` is positive exactly when `move` is worse than the
        baseline and negative when it is better.
        """
        if best is None:
            baseline_board = board
        else:
            baseline_board = board.copy(stack=False)
            baseline_board.push(best)

        baseline_info = self.analyse_position(baseline_board)

        baseline_cp = score_to_centipawns(baseline_info, board.turn)
        baseline_mate = mate_distance(baseline_info, board.turn)

        after_board = board.copy(stack=False)
        after_board.push(move)

        after_info = self.analyse_position(after_board)

        after_cp = score_to_centipawns(after_info, board.turn)
        after_mate = mate_distance(after_info, board.turn)

        difference: float | None = None

        if baseline_cp is not None and after_cp is not None:
            difference = (
                win_probability(baseline_cp)
                - win_probability(after_cp)
            )

        pv = after_info.get("pv", [])

        best_line = after_board.variation_san(
            pv[: self.punishment_depth]
        )

        return {
            "move": board.san(move),
            "uci": move.uci(),
            "before": (
                None if baseline_cp is None else win_probability(baseline_cp)
            ),
            "after": None if after_cp is None else win_probability(after_cp),
            "difference": difference,
            "mate_before": baseline_mate,
            "mate_after": after_mate,
            "best_line": best_line,
            "board": after_board,
        }

    def _classify_mistake(
        self,
        difference: float,
    ) -> str | None:
        """Classify an opponent mistake according to the score it throws away."""
        return classify_mistake(difference, self.mistake_threshold)

    def _make_mistake_comment(
        self,
        result: dict[str, object],
        classification: str,
    ) -> str:
        """Create the PGN comment for a detected mistake."""
        move = str(result["move"])
        difference = result["difference"]

        before = result["before"]
        after = result["after"]

        before_text = (
            "N/A"
            if before is None
            else f"{float(before):.1f}%"
        )

        after_text = (
            "N/A"
            if after is None
            else f"{float(after):.1f}%"
        )

        difference_text = (
            "N/A"
            if difference is None
            else f"{float(difference):.1f}"
        )

        punishment = self._first_punishment_move(str(result["best_line"]))

        return (
            f"{classification}: ...{move}?!\n"
            f"Best available: {before_text}\n"
            f"After this move: {after_text}\n"
            f"Win probability given away: {difference_text}\n"
            f"Punishment: {punishment}"
        )

    @staticmethod
    def _first_punishment_move(best_line: str) -> str:
        """First move of a SAN variation, ignoring the move numbers."""
        for part in best_line.split():
            if not part.endswith(".") and not part.endswith("..."):
                return f"{part}!"

        return "N/A"

    # =============================================================
    # FIND OPPONENT DEVIATIONS
    # =============================================================

    def _candidate_opponent_moves(
        self,
        board: chess.Board,
    ) -> list[chess.Move]:
        """
        Return the opponent moves worth analysing.

        Only moves the Lichess Masters database actually records for this
        position are candidates.  Synthetic candidates such as "every
        capture and check" are not included: a capture that nobody plays
        is a theoretical curiosity, not a mistake an opponent will commit
        in a game, and analysing them buries the deviations that matter.

        The candidates keep the database's own order, which is games
        played descending, so the cap keeps the moves a real opponent is
        most likely to choose.

        A candidate also has to clear `min_opponent_frequency`.  A move
        that one player out of fifty thousand tried is a curiosity, and
        building a punishment line for it buries the deviations that
        actually matter.  The bar is low, because a real blunder is
        usually rare too.
        """
        if board.turn == self.color:
            return []

        data = self._get_position_data(board)

        if data.total <= 0:
            return []

        candidates: list[chess.Move] = []
        seen: set[chess.Move] = set()

        for move_data in data.moves:
            if move_data.total < 2:
                continue

            if move_data.total / data.total < self.min_opponent_frequency:
                continue

            move = self._resolve_move(board, move_data.uci)

            if move is None or move in seen:
                continue

            seen.add(move)
            candidates.append(move)

            if len(candidates) >= self.max_deviation_moves:
                break

        return candidates

    def _find_opponent_mistakes(
        self,
        board: chess.Board,
    ) -> list[dict[str, object]]:
        """
        Test candidate opponent moves and return the serious mistakes.

        A move is measured against the best of the realistic candidates
        and the engine's own first choice, each evaluated through exactly
        the same path, so the comparison carries no side-to-move
        asymmetry.  A move that matches or beats that baseline is never a
        mistake, and the baseline can never be the move being judged.
        """
        if board.turn == self.color:
            return []

        candidates = self._candidate_opponent_moves(board)

        if not candidates:
            return []

        data = self._get_position_data(board)

        games_by_move: dict[chess.Move, int] = {}

        for move_data in data.moves:
            try:
                move = board.parse_uci(move_data.uci)
            except ValueError:
                continue

            games_by_move.setdefault(move, move_data.total)

        # The baseline has to include the engine's own preference, or a
        # perfectly normal database move would look like a mistake merely
        # because the engine would have played something better.
        probes = list(candidates)

        engine_best = self._engine_best_move(board)

        if engine_best is not None and engine_best not in probes:
            probes.append(engine_best)

        evaluations: dict[chess.Move, float] = {}

        for move in probes:
            value = self._win_percent_after(board, move)

            if value is not None:
                evaluations[move] = value

        if not evaluations:
            return []

        baseline_move = max(evaluations, key=evaluations.get)
        baseline = evaluations[baseline_move]

        mistakes: list[dict[str, object]] = []

        for move in candidates:
            after = evaluations.get(move)

            if after is None:
                continue

            loss = baseline - after

            # A negative loss means the move matched or beat the best
            # option known here. That is the engine improving on itself,
            # not a mistake, and it must never be reported as one.
            classification = self._classify_mistake(loss)

            if classification is None:
                continue

            result = self._evaluate_move(
                board,
                move,
                best=baseline_move,
            )

            games = games_by_move.get(move, 0)

            result["difference"] = loss
            result["games"] = games
            result["frequency"] = 0.0 if data.total <= 0 else games / data.total
            result["baseline"] = baseline
            result["classification"] = classification
            result["comment"] = self._make_mistake_comment(
                result,
                classification,
            )

            mistakes.append(result)

        mistakes.sort(
            key=lambda item: float(item["difference"]),
            reverse=True,
        )

        return mistakes[: self.max_mistakes]

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

        This is a single principal variation rather than another Lichess
        repertoire branch, so it deliberately ignores `max_depth`.
        """
        if plies <= 0:
            return []

        info = self.analyse_position(board)

        pv = info.get("pv", [])

        if not pv:
            return []

        nodes: list[MoveNode] = []

        current_board = board.copy()

        for move in pv[:plies]:
            if move not in current_board.legal_moves:
                break

            nodes.append(
                MoveNode(
                    san=current_board.san(move),
                    uci=move.uci(),
                )
            )

            current_board.push(move)

        # Convert the linear list into a chain.
        for index in range(len(nodes) - 1):
            nodes[index].children = [nodes[index + 1]]

        return nodes[:1]

    # =============================================================
    # LICHESS
    # =============================================================

    def _get_position_data(
        self,
        board: chess.Board,
    ) -> PositionData:
        """Get opening statistics for `board` from the configured source.

        Caching belongs to the source: `LichessDataSource` memoises network
        responses and `PgnDataSource` is a dict lookup over a prebuilt index.
        Keeping a second cache here would only duplicate that work.
        """
        return self.source.get_position(board)

    def _select_moves(
        self,
        board: chess.Board,
        data,
    ) -> list[tuple[object, float, int]]:
        """
        Select the main Lichess moves.

        `min_games` is a position level cutoff: a position played fewer
        than that many games ends the branch instead of being filtered
        move by move.  Filtering move by move with a fixed absolute number
        truncates the tree at a fixed depth regardless of how large the
        database is there, which produces a repertoire that stops a few
        plies in.

        A move is therefore kept when it covers at least
        `min_move_frequency` of the games played in this position, and the
        most played qualifying moves are taken until they cover
        `min_frequency` of the position, at most `max_moves` in total.

        Moves are resolved against the board here, while there are still
        more candidates to fall back on.  Selecting a move first and
        parsing it afterwards means one unusable row at the top of the
        ranking consumes a selection slot and is then dropped, which can
        leave the position with no repertoire line at all.
        """
        total = getattr(data, "total", 0)

        if total <= 0:
            return []

        if total < self.min_games:
            return []

        # A move can clear every local test and still be meaningless in the
        # database as a whole. This floor is compared as a count rather than
        # a share so the threshold is exact, and it is zero by default so it
        # changes nothing until it is asked for.
        root_floor = self._root_games * self.min_root_share

        ranked: list[tuple[chess.Move, object, float, int]] = []
        seen: set[chess.Move] = set()

        for move_data in getattr(data, "moves", []):
            games = move_data.total

            frequency = games / total

            if frequency < self.min_move_frequency:
                continue

            if games < root_floor:
                continue

            move = self._resolve_move(board, move_data.uci)

            if move is None or move in seen:
                continue

            seen.add(move)

            ranked.append((move, move_data, frequency, games))

        ranked.sort(key=lambda item: item[3], reverse=True)

        selected: list[tuple[object, float, int]] = []

        cumulative_frequency = 0.0

        for move, move_data, frequency, games in ranked:
            selected.append((move_data, frequency, games))

            cumulative_frequency += frequency

            if (
                cumulative_frequency >= self.min_frequency
                or len(selected) >= self.max_moves
            ):
                break

        return selected

    @staticmethod
    def _resolve_move(
        board: chess.Board,
        uci: str,
    ) -> chess.Move | None:
        """Parse a UCI move and return it only if it is legal here."""
        try:
            move = board.parse_uci(uci)
        except ValueError:
            return None

        return move if move in board.legal_moves else None

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

        At every opponent position we additionally scan for realistic
        mistakes and attach punishment variations.
        """
        if depth >= self.max_depth:
            return []

        key = position_key(board)

        cache_key = (key, depth)

        if cache_key in self._tree_cache:
            return self._tree_cache[cache_key]

        if key in self._active:
            return []

        self._active.add(key)

        try:
            data = self._get_position_data(board)

            selected_moves = self._select_moves(board, data)

            nodes: list[MoveNode] = []
            seen_moves: set[str] = set()

            # -----------------------------------------------------
            # Main repertoire branches
            # -----------------------------------------------------

            for move_data, frequency, games in selected_moves:
                move = self._resolve_move(board, move_data.uci)

                # `move.uci` is a method, so it has to be called before it
                # can be compared with the UCI strings in `seen_moves`.
                if move is None:
                    continue

                uci = move.uci()

                if uci in seen_moves:
                    continue

                seen_moves.add(uci)

                child_board = board.copy()
                child_board.push(move)

                children = self._build_tree(
                    child_board,
                    depth + 1,
                )

                nodes.append(
                    MoveNode(
                        # The SAN is taken from the board rather than the
                        # API.  The UCI has already been checked for
                        # legality, so the board always produces SAN that
                        # matches the move and can be replayed.
                        san=board.san(move),
                        uci=uci,
                        children=children,
                        comment=(
                            f"{MAIN_LINE if not nodes else ALTERNATIVE} "
                            f"{frequency * 100:.1f}%"
                        ),
                    )
                )

            # -----------------------------------------------------
            # At the opponent's turn, scan realistic deviations.
            # -----------------------------------------------------

            if board.turn != self.color:
                mistakes = self._find_opponent_mistakes(board)

                for mistake in mistakes:
                    node = self._build_mistake_node(
                        board,
                        mistake,
                        seen_moves,
                    )

                    if node is not None:
                        nodes.append(node)
                        seen_moves.add(node.uci)

            self._tree_cache[cache_key] = nodes

            return nodes

        finally:
            self._active.discard(key)

    def _build_mistake_node(
        self,
        board: chess.Board,
        mistake: dict[str, object],
        seen_moves: set[str],
    ) -> MoveNode | None:
        """Build the mistake branch, with its punishment line."""
        uci = str(mistake.get("uci", ""))

        if not uci or uci in seen_moves:
            return None

        try:
            black_move = board.parse_uci(uci)
        except ValueError:
            return None

        punishment_board = board.copy()
        punishment_board.push(black_move)

        punishment_nodes = self._build_punishment_nodes(
            punishment_board,
            self.punishment_depth,
        )

        # A mistake with no principal variation is still a mistake, so the
        # branch is kept even when there is no line to attach.
        if punishment_nodes:
            first = punishment_nodes[0]
            first.comment = f"{PUNISHMENT}: {self._first_punishment_move(str(mistake['best_line']))}"
            children = [first]
        else:
            children = []

        return MoveNode(
            san=board.san(black_move),
            uci=uci,
            children=children,
            comment=f"{MISTAKE}\n{mistake['comment']}",
        )

    # =============================================================
    # PUBLIC API
    # =============================================================

    def build(
        self,
        fen: str,
    ) -> list[MoveNode]:
        """Build the complete repertoire from a FEN string."""
        board = chess.Board(fen)

        # The root count is what `min_root_share` is measured against, and
        # it is the denominator at the root only. Every other frequency in
        # the tree is relative to its own position.
        self._root_games = self._get_position_data(board).total

        return self._build_tree(board, depth=0)
