from __future__ import annotations

import os

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from .models import MoveStats, PositionData

MASTERS_URL = "https://explorer.lichess.org/masters"


class RateLimited(Exception):
    pass


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
        retry=retry_if_exception_type(RateLimited),
        wait=wait_fixed(61),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _fetch(self, fen: str) -> PositionData:
        resp = self._client.get(
            MASTERS_URL,
            params={"fen": fen},
        )

        if resp.status_code == 429:
            raise RateLimited(f"Rate limited fetching {fen}")

        resp.raise_for_status()

        body = resp.json()

        moves = [
            MoveStats(
                uci=m["uci"],
                san=m["san"],
                white=m["white"],
                draws=m["draws"],
                black=m["black"],
            )
            for m in body["moves"]
        ]

        return PositionData(
            white=body["white"],
            draws=body["draws"],
            black=body["black"],
            moves=moves,
        )

    def close(self) -> None:
        self._client.close()