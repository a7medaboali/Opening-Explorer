from __future__ import annotations

import os

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from .models import MoveStats, PositionData

MASTERS_URL = "https://explorer.lichess.org/masters"


class RateLimited(Exception):
    pass


class LichessAuthError(Exception):
    """The explorer refused the request because we are not authenticated."""


class LichessResponseError(Exception):
    """The explorer answered with something we cannot use."""


class TransientLichessError(LichessResponseError):
    """
    A failure that a later, identical request may survive.

    Keeping this separate matters: retrying a 5xx or a dropped connection is
    sensible, while retrying a 400 for a malformed FEN only turns an obvious
    mistake into a four minute wait before the same error appears.
    """


def _parse_position(body: object) -> PositionData:
    """Turn a Masters explorer payload into a PositionData."""
    if not isinstance(body, dict):
        raise LichessResponseError(
            f"expected a JSON object, got {type(body).__name__}"
        )

    try:
        white = int(body["white"])
        draws = int(body["draws"])
        black = int(body["black"])
        raw_moves = body["moves"]
    except KeyError as exc:
        raise LichessResponseError(
            f"missing field {exc} in Masters response"
        ) from exc
    except (TypeError, ValueError) as exc:
        raise LichessResponseError(
            "malformed game counts in Masters response"
        ) from exc

    if not isinstance(raw_moves, list):
        raise LichessResponseError(
            f"expected 'moves' to be a list, got {type(raw_moves).__name__}"
        )

    moves: list[MoveStats] = []

    for entry in raw_moves:
        try:
            moves.append(
                MoveStats(
                    uci=str(entry["uci"]),
                    san=str(entry["san"]),
                    white=int(entry["white"]),
                    draws=int(entry["draws"]),
                    black=int(entry["black"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            # A single unusable row must not lose the whole position.
            continue

    return PositionData(
        white=white,
        draws=draws,
        black=black,
        moves=moves,
    )


class LichessClient:
    def __init__(self) -> None:
        token = os.environ.get("LICHESS_TOKEN")

        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        self._client = httpx.Client(
            timeout=30,
            headers=headers,
        )
        self._cache: dict[str, PositionData] = {}

    def get_position(self, fen: str) -> PositionData:
        if fen in self._cache:
            return self._cache[fen]

        data = self._fetch(fen)
        self._cache[fen] = data
        return data

    @retry(
        retry=retry_if_exception_type(
            (RateLimited, httpx.TransportError, TransientLichessError)
        ),
        wait=wait_fixed(61),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _fetch(self, fen: str) -> PositionData:
        try:
            resp = self._client.get(
                MASTERS_URL,
                params={"fen": fen},
            )
        except httpx.TransportError:
            # Transient network failure: let tenacity retry.
            raise

        if resp.status_code == 429:
            raise RateLimited(f"Rate limited fetching {fen}")

        if resp.status_code in (401, 403):
            raise LichessAuthError(
                "Lichess refused the request. The Masters explorer needs a "
                "token: set LICHESS_TOKEN to a token created at "
                "https://lichess.org/account/oauth/token."
            )

        if resp.status_code >= 500:
            raise TransientLichessError(
                f"explorer.lichess.org returned {resp.status_code}"
            )

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # 4xx is the request's own fault, so it is not retried.
            raise LichessResponseError(
                f"explorer.lichess.org returned {resp.status_code}"
            ) from exc

        try:
            body = resp.json()
        except ValueError as exc:
            # A gateway that answers with an HTML error page usually stops
            # doing so on the next attempt.
            raise TransientLichessError(
                f"explorer.lichess.org returned a non-JSON body for {fen}"
            ) from exc

        return _parse_position(body)

    def close(self) -> None:
        self._client.close()
