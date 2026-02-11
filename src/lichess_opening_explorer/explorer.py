from __future__ import annotations

import chess

from .api import LichessClient
from .models import MoveNode, MoveStats, PositionData


class RepertoireBuilder:
    def __init__(
        self,
        client: LichessClient,
        color: chess.Color,
        min_games: int,
        threshold: float = 0.8,
    ) -> None:
        self.client = client
        self.color = color
        self.min_games = min_games
        self.threshold = threshold

    def build(self, fen: str) -> list[MoveNode]:
        board = chess.Board(fen)
        our_turn = board.turn == self.color
        if our_turn:
            return self._our_moves(fen, board)
        else:
            return self._opponent_moves(fen, board)

    def _our_moves(self, fen: str, board: chess.Board) -> list[MoveNode]:
        data = self.client.get_position(fen)
        if not data.moves:
            return []

        sorted_moves = sorted(data.moves, key=lambda m: m.total, reverse=True)
        total = data.total
        if total == 0:
            return []

        nodes: list[MoveNode] = []
        cumulative = 0.0
        for ms in sorted_moves:
            freq = ms.total / total
            cumulative += freq
            node = MoveNode(san=ms.san, uci=ms.uci)
            child_board = board.copy()
            child_board.push_san(ms.san)
            child_fen = child_board.fen()
            print(f"  Our move: {ms.san} ({ms.total}/{total} = {freq:.1%})")
            node.children = self._opponent_moves(child_fen, child_board)
            nodes.append(node)
            if cumulative >= self.threshold:
                break

        return nodes

    def _opponent_moves(self, fen: str, board: chess.Board) -> list[MoveNode]:
        data = self.client.get_position(fen)
        if data.total < self.min_games:
            return []
        if not data.moves:
            return []

        sorted_moves = sorted(data.moves, key=lambda m: m.total, reverse=True)
        top_move = sorted_moves[0]
        top_perf = top_move.performance()

        selected: list[MoveStats] = [top_move]
        for ms in sorted_moves[1:4]:
            if ms.performance() > top_perf:
                selected.append(ms)

        nodes: list[MoveNode] = []
        for ms in selected:
            node = MoveNode(san=ms.san, uci=ms.uci)
            child_board = board.copy()
            child_board.push_san(ms.san)
            child_fen = child_board.fen()
            label = "top" if ms is top_move else "high-perf"
            print(f"  Opp move ({label}): {ms.san} (perf={ms.performance():.1%}, n={ms.total})")
            node.children = self._our_moves(child_fen, child_board)
            nodes.append(node)

        return nodes
