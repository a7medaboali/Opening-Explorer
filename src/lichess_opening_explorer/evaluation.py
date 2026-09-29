"""Evaluation helpers shared by the repertoire builder.

The repertoire is judged in *win probability*, not in raw centipawns.
A one-pawn swing means very different things at move 5 and at move 25,
and near equality a whole pawn is only about nine percentage points of
expected score.  Raw pawn thresholds therefore flag normal opening moves
and miss real ones.  Win percentage is comparable everywhere, which is
what a repertoire needs.
"""

from __future__ import annotations

import math

import chess.engine

# Coefficient of the Lichess win-probability model:
#   win% = 50 + 50 * (2 / (1 + exp(-k * cp)) - 1)
# k is positive: a positive centipawn score has to raise the win
# percentage, not lower it.
WIN_PROBABILITY_FACTOR = 0.00368208

# Mate is outside the centipawn scale.  Any mate is worth more than any
# positional evaluation, and a shorter mate is worth more than a longer
# one, so mate is folded into a large finite centipawn value.
MATE_SCORE = 10_000

BLUNDER_RATIO = 2.5
SERIOUS_MISTAKE_RATIO = 1.5


def win_probability(centipawns: float) -> float:
    """Return the expected score of `centipawns` as a percentage (0..100)."""
    return 50.0 + 50.0 * (
        2.0 / (1.0 + math.exp(-WIN_PROBABILITY_FACTOR * centipawns)) - 1.0
    )


def score_to_centipawns(
    info: chess.engine.InfoDict,
    pov: chess.Color,
) -> int | None:
    """Return a mate-safe centipawn evaluation seen from `pov`.

    Mate scores are converted to a large finite value instead of being
    discarded.  A move that walks into forced mate is the most punishable
    move there is, so it must rank at the very bottom rather than being
    reported as "no evaluation available".
    """
    score = info.get("score")

    if score is None:
        return None

    score = score.pov(pov)

    if score.is_mate():
        mate = score.mate()

        if mate is None:
            return None

        return (MATE_SCORE - mate) if mate > 0 else -(MATE_SCORE + mate)

    return score.score()


def mate_distance(
    info: chess.engine.InfoDict,
    pov: chess.Color,
) -> int | None:
    """Return the mate distance seen from `pov`, or None if not a mate.

    Positive means `pov` delivers mate, negative means `pov` is mated.
    """
    score = info.get("score")

    if score is None or not score.is_mate():
        return None

    return score.pov(pov).mate()


def format_score(centipawns: int | None, mate: int | None = None) -> str:
    """Human readable evaluation for PGN comments."""
    if mate is not None:
        return f"M{mate}" if mate > 0 else f"-M{abs(mate)}"

    if centipawns is None:
        return "N/A"

    return f"{win_probability(centipawns):+.1f}%"


def classify_mistake(
    win_loss: float,
    threshold: float,
) -> str | None:
    """Classify how much expected score the opponent threw away.

    `win_loss` is in percentage points and is expected to be positive for
    a move that is worse than the best move available in the position.
    """
    if win_loss < threshold:
        return None

    if win_loss >= BLUNDER_RATIO * threshold:
        return "BLUNDER"

    if win_loss >= SERIOUS_MISTAKE_RATIO * threshold:
        return "SERIOUS MISTAKE"

    return "MISTAKE"
