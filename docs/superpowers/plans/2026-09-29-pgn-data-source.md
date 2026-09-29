# Local PGN Opening Database Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local PGN file as a second source of opening statistics, so a repertoire can be built entirely from the user's own games, with Stockfish still judging quality and generating punishments.

**Architecture:** Introduce a one-method `DataSource` protocol that `RepertoireBuilder` depends on instead of `LichessClient`. A `PgnIndex` streams the PGN once and records only the positions within `max_depth` of the anchor, keyed by the existing normalized `position_key()`. A `PgnDataSource` adapts that index to the protocol with a dict lookup. All existing selection, pruning, mistake detection, punishment, and PGN export code is reused unchanged.

**Tech Stack:** Python 3.13+, `python-chess` (`chess`, `chess.pgn`), `httpx`, `tenacity`, `pytest`, `uv`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-29-pgn-data-source-design.md`

## Global Constraints

These apply to every task. Read them before writing any code.

- **No new dependencies.** `pyproject.toml` stays exactly as-is: `httpx>=0.28.1`, `python-chess>=1.999`, `tenacity>=9.1.4`, dev `pytest>=9.1.1`. Python `>=3.13`.
- **Every module starts with `from __future__ import annotations`.** Existing convention in all six source modules.
- **`position_key()` is the only position-identity function in the codebase.** Do not add a second one, and do not change its behaviour. It moves from `explorer.py` to `models.py` in Task 2 purely to break a circular import, and `explorer.py` re-exports it so existing imports keep working.
- **Move frequency always uses the local denominator.** The denominator is `PositionData.total` of the position the move is played from. The root database size is never a move-frequency denominator. The only exception is the optional `min_root_share` guard, which is designed to *reject* tiny lines and defaults to `0.0` (off).
- **Mainline moves only.** Side variations (RAV) are never counted as played.
- **Move lists are sorted by `total` descending.** `_candidate_opponent_moves` documents that it relies on the data source returning moves in games-played descending order (`explorer.py:425`). A PGN source that returns them unordered will silently change which deviations get analysed.
- **Docstrings and short "why" comments follow the existing codebase convention.** This codebase documents rationale in docstrings and comments (see `explorer.py:25`, `explorer.py:411`, `evaluation.py:1`). Match it; do not add narration of what the code obviously does.
- **Test fixtures are generated as strings inside `tests/pgn_fixtures.py`.** No binary `.pgn` fixture files are added to the repo.
- **Stockfish stays fixed-depth.** Never introduce a time limit.
- **Every task must end with the full suite green** before its commit step.
- **Do not create a worktree or branch without asking.** Work on the current branch.

## Review Focus

Five input classes the spec implies but no listed test exercises. Each gets a test in the task that owns the relevant code, in that task's own step style.

1. **A PGN file that ends mid-game (truncated last record).** Extremely common in real databases. The partial game must still be indexed and the rest of the file must not be lost. Owner: Task 4.
2. **A non-UTF-8 PGN (Latin-1 player names).** Old opening databases are frequently Latin-1. Must not raise `UnicodeDecodeError`. Owner: Task 4.
3. **An anchor FEN that is already a terminal position (checkmate or stalemate).** There are no legal moves, so the tree is empty. Must report, not crash. Owner: Task 5.
4. **A game that reaches the anchor as its final position.** It counts toward the denominator and contributes no moves, so move shares at that position sum to less than 1. Must not be renormalised. Owner: Task 4.
5. **A PGN where zero games reach the anchor.** Produces an empty tree. The CLI summary must state the cause, not just print zeros. Owner: Task 8.

---

## File Structure

| File | Created/Modified | Responsibility |
|---|---|---|
| `src/lichess_opening_explorer/models.py` | modified | Add `position_key()`; document the `total` contract on `PositionData` |
| `src/lichess_opening_explorer/sources.py` | **created** | `DataSource` Protocol, `LichessDataSource` |
| `src/lichess_opening_explorer/pgn_index.py` | **created** | `PgnIndex`, `PgnIndexReport` — streaming parse, anchor detection, statistics |
| `src/lichess_opening_explorer/explorer.py` | modified | Depend on `DataSource`; add `min_root_share`; drop the duplicate cache |
| `src/lichess_opening_explorer/cli.py` | modified | `--source`, `--pgn`, `--min-root-share`, per-source defaults, index report |
| `src/lichess_opening_explorer/api.py` | unchanged | Lichess transport |
| `src/lichess_opening_explorer/pgn_export.py` | unchanged | `MoveNode` → PGN |
| `src/lichess_opening_explorer/evaluation.py` | unchanged | Engine math (but must be tracked — Task 1) |
| `tests/pgn_fixtures.py` | **created** | PGN text generation helpers for tests |
| `tests/test_pgn_index.py` | **created** | `PgnIndex` unit tests |
| `tests/test_sources.py` | **created** | `DataSource` contract tests over both sources |
| `tests/test_explorer.py` | modified | `make_builder` update, `min_root_share`, named regressions |
| `README.md` | modified | New flags, PGN source, housekeeping fixes |

---

## Task 1: Housekeeping

**Files:**
- Modify: `README.md:84` (BLUNDER ratio), `README.md:112-124` (example move numbers)
- Track: `src/lichess_opening_explorer/evaluation.py` (currently untracked)

**Interfaces:**
- Consumes: nothing
- Produces: nothing consumed by later tasks. This task only removes known defects.

- [ ] **Step 1: Confirm `evaluation.py` is untracked and imported**

Run:
```bash
git status --short src/lichess_opening_explorer/evaluation.py
Select-String -Path src/lichess_opening_explorer/explorer.py -Pattern "from .evaluation import"
```

Expected: `?? src/lichess_opening_explorer/evaluation.py` and a matching import block. The file is required for the package to import, so it must be tracked.

- [ ] **Step 2: Stage `evaluation.py`**

Run:
```bash
git add src/lichess_opening_explorer/evaluation.py
```

- [ ] **Step 3: Fix the README blunder threshold**

`README.md` line 84 currently reads:

```
  - `BLUNDER`: >= 3.0x the threshold
```

`evaluation.py:28` sets `BLUNDER_RATIO = 2.5`, and `classify_mistake` compares `win_loss >= BLUNDER_RATIO * threshold`. The README is wrong.

Replace with:

```
  - `BLUNDER`: >= 2.5x the threshold
```

- [ ] **Step 4: Fix the README example move numbers**

The anchor FEN in the example is `... w KQkq - 4 5`, so White's first move is move **5**, not move 4. The generated PGN from a real run numbers it `5. e3`.

Replace the `pgn` code block at `README.md:112-124` with:

````markdown
```pgn
[Event "Opening Repertoire"]
[FEN "rnbqk2r/ppp2ppp/4pn2/3p2B1/1bPP4/2N5/PP2PPPP/R2QKBNR w KQkq - 4 5"]
[SetUp "1"]

5. e3 { MAIN LINE 100.0% } 5... O-O { MAIN LINE 55.2% } ( 5... Be7 { ALTERNATIVE 41.4% } )
( 5... Nh5 { MISTAKE
BLUNDER: ...Nh5?!
Best available: 46.1%
After this move: 2.9%
Win probability given away: 43.2
Punishment: Bxd8! } 6. Bxd8 { PUNISHMENT: Bxd8! } 6... Kxd8 7. a3 Ba5 ) *
```
````

- [ ] **Step 5: Verify the README no longer contradicts the code**

Run:
```bash
Select-String -Path README.md -Pattern "3.0x"
```

Expected: no output.

- [ ] **Step 6: Run the full suite to confirm nothing broke**

Run:
```bash
uv run pytest -q
```

Expected: `94 passed, 6 skipped` (the six real-Stockfish tests skip when the binary is not on the path).

- [ ] **Step 7: Commit**

```bash
git add README.md src/lichess_opening_explorer/evaluation.py
git commit -m "fix: track evaluation module and correct README inaccuracies"
```

---

## Task 2: `position_key` relocation and the `DataSource` protocol

**Files:**
- Modify: `src/lichess_opening_explorer/models.py`
- Modify: `src/lichess_opening_explorer/explorer.py:25-39` (remove `position_key`, import it instead)
- Create: `src/lichess_opening_explorer/sources.py`
- Modify: `tests/test_explorer.py:32` (import path is unchanged, but verify)

**Interfaces:**
- Consumes: `models.PositionData`, `api.LichessClient`
- Produces:
  - `models.position_key(board: chess.Board) -> str`
  - `sources.DataSource` — `Protocol` with `get_position(self, board: chess.Board) -> PositionData`
  - `sources.LichessDataSource(client: LichessClient | None = None)` with `.get_position(board) -> PositionData` and `.close() -> None`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_explorer.py`:

```python
# =============================================================
# sources.py -- the data source abstraction
# =============================================================


def test_position_key_is_importable_from_models():
    """position_key moved to models so sources.py can import it.

    A circular import would otherwise appear the moment sources.py needs
    the key and explorer.py needs the protocol.
    """
    from lichess_opening_explorer.models import position_key as from_models
    from lichess_opening_explorer.explorer import position_key as from_explorer

    assert from_models is from_explorer


def test_lichess_data_source_delegates_and_caches():
    calls = []

    class RecordingClient:
        def get_position(self, fen):
            calls.append(fen)
            return PositionData(7, 0, 0, [])

        def close(self):
            calls.append("closed")

    client = RecordingClient()
    source = LichessDataSource(client)
    board = chess.Board(NIMZO_FEN)

    assert source.get_position(board).total == 7
    assert source.get_position(board).total == 7

    # Second call must be served from the source's own cache.
    assert calls == [position_key(board)]

    source.close()

    # A client passed in from outside is not owned, so it is not closed.
    assert calls == [position_key(board)]


def test_lichess_data_source_closes_a_client_it_created():
    created = []

    class RecordingClient:
        def __init__(self):
            created.append(self)

        def get_position(self, fen):
            return PositionData(0, 0, 0, [])

        def close(self):
            created.append("closed")

    import lichess_opening_explorer.sources as sources_module

    original = sources_module.LichessClient
    sources_module.LichessClient = RecordingClient
    try:
        source = LichessDataSource()
        source.close()
    finally:
        sources_module.LichessClient = original

    assert created == [created[0], "closed"]
```

Add the import at the top of `tests/test_explorer.py`, after the existing `from lichess_opening_explorer.pgn_export import ...` line:

```python
from lichess_opening_explorer.sources import LichessDataSource
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
uv run pytest tests/test_explorer.py::test_position_key_is_importable_from_models tests/test_explorer.py::test_lichess_data_source_delegates_and_caches -v
```

Expected: collection error — `ModuleNotFoundError: No module named 'lichess_opening_explorer.sources'`.

- [ ] **Step 3: Move `position_key` into `models.py`**

Add `import chess` to the top of `src/lichess_opening_explorer/models.py`, and insert this function **before** the `MoveStats` dataclass:

```python
def position_key(board: chess.Board) -> str:
    """Identity of a position, for caches, APIs, and transposition merging.

    Only the first four FEN fields are kept.  The halfmove clock and the
    fullmove number say nothing about the position, so including them
    splits transpositions of one position across several cache entries
    and asks a data source about the same position more than once.

    The en passant field uses python-chess's "legal" rule, which records
    the square only when a capture is actually available.  The "fen" rule
    always records the square behind a double pawn push, so the same
    position reached by a different move order gets two different keys
    and misses the cache.
    """
    return " ".join(board.fen(en_passant="legal").split(" ")[:4])
```

Then in `src/lichess_opening_explorer/explorer.py`, delete the `position_key` function (lines 25-39) and change the models import to:

```python
from .models import MoveNode, PositionData, position_key
```

Add `PositionData` to that import — it is needed for the type annotation added in Task 3, and importing it now keeps this task's diff to a single line.

Finally, document the denominator contract on `PositionData`, because both the Lichess and the PGN source are now required to honour it and nothing in the type signature says so:

```python
@dataclass
class PositionData:
    """Statistics for one position, as the moves actually played in it.

    `total` is the number of games that reached this position, and it is
    the denominator for every move frequency derived from `moves`. It is
    deliberately *not* the size of the database or the size of the
    ancestor position, so a line that is rare at its parent stays rare
    here rather than being renormalised to look common.

    `moves` must be ordered by `MoveStats.total` descending: the builder's
    deviation selection relies on it.
    """

    white: int
    draws: int
    black: int
    moves: list[MoveStats]
```

Keep the existing `@property def total` below it.

- [ ] **Step 4: Create `sources.py`**

Create `src/lichess_opening_explorer/sources.py`:

```python
"""
Where opening statistics come from.

The repertoire builder only ever asks one question: *which moves were
actually played in this position, and how often?*  The answer arrives as a
`PositionData`, whatever produced it.  Keeping the question this narrow is
what lets a local PGN replace the Lichess explorer without any change to
the selection, mistake detection, or export logic.
"""

from __future__ import annotations

from typing import Protocol

import chess

from .api import LichessClient
from .models import PositionData, position_key


class DataSource(Protocol):
    """Opening statistics for positions, from any origin."""

    def get_position(self, board: chess.Board) -> PositionData:
        """Statistics for the position `board` is currently in.

        `PositionData.total` MUST be the number of games that reached this
        position, and every `MoveStats.total` MUST be the number of those
        games that played that move.  Move frequency is computed as
        `MoveStats.total / PositionData.total`, so a source that reports
        anything else makes every frequency in the tree wrong.

        An empty `PositionData` for an unknown position is valid and means
        "no data here".  `moves` MUST be ordered by `total` descending,
        because the builder keeps the first qualifying deviations and the
        cap is meant to keep the moves a real opponent plays most often.
        """
        ...


class LichessDataSource:
    """Opening statistics from the Lichess Masters explorer."""

    def __init__(self, client: LichessClient | None = None) -> None:
        self._client = client if client is not None else LichessClient()
        self._owns_client = client is None
        self._cache: dict[str, PositionData] = {}

    def get_position(self, board: chess.Board) -> PositionData:
        key = position_key(board)

        if key not in self._cache:
            self._cache[key] = self._client.get_position(fen=key)

        return self._cache[key]

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
```

- [ ] **Step 5: Run the new tests**

Run:
```bash
uv run pytest tests/test_explorer.py::test_position_key_is_importable_from_models tests/test_explorer.py::test_lichess_data_source_delegates_and_caches tests/test_explorer.py::test_lichess_data_source_closes_a_client_it_created -v
```

Expected: 3 passed.

- [ ] **Step 6: Run the full suite**

Run:
```bash
uv run pytest -q
```

Expected: `97 passed, 6 skipped`. Nothing else changed behaviour, so the original 94 must still pass.

- [ ] **Step 7: Commit**

```bash
git add src/lichess_opening_explorer/models.py src/lichess_opening_explorer/explorer.py src/lichess_opening_explorer/sources.py tests/test_explorer.py
git commit -m "refactor: extract position_key and add DataSource protocol"
```

---

## Task 3: Rewire `RepertoireBuilder` onto `DataSource`

This is the highest-risk task in the plan. It is a pure refactor: **no behaviour may change**, and the original 94 tests must pass untouched apart from the `make_builder` helper.

**Files:**
- Modify: `src/lichess_opening_explorer/explorer.py:44,108,132,619-634`
- Modify: `tests/test_explorer.py:61-95` (`make_builder`)

**Interfaces:**
- Consumes: `sources.DataSource`, `models.PositionData`
- Produces: `RepertoireBuilder(source: DataSource, color: chess.Color, ...)` — the first positional parameter is renamed from `client` to `source`

- [ ] **Step 1: Find every place the builder's data dependency is referenced**

Run:
```bash
Select-String -Path src/lichess_opening_explorer/explorer.py,tests/test_explorer.py,src/lichess_opening_explorer/cli.py -Pattern "_position_cache|builder\.client|_get_position_data|\.client"
```

Expected matches to fix: `explorer.py` `__init__` parameter and assignment, `explorer.py:132` the cache, `explorer.py:629` the delegation; `tests/test_explorer.py` `make_builder`; `cli.py:137-140`. Note any test that assigns `builder._get_position_data` directly — those keep working, because the method name is unchanged.

- [ ] **Step 2: Change the constructor parameter and assignment**

In `src/lichess_opening_explorer/explorer.py`, change the signature line:

```python
    def __init__(
        self,
        source: DataSource,
        color: chess.Color,
```

and the assignment:

```python
        self.source = source
        self.color = color
```

Add the import at the top of `explorer.py`:

```python
from .sources import DataSource
```

Note the local import order: `sources.py` imports `api` and `models` only, so `explorer -> sources -> api` has no cycle.

- [ ] **Step 3: Replace the caching delegation**

In `src/lichess_opening_explorer/explorer.py`, delete the `_position_cache` initialisation (line 132):

```python
        self._position_cache: dict[str, object] = {}
```

and replace the whole `_get_position_data` method (lines 619-634) with:

```python
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
```

- [ ] **Step 4: Update `make_builder` in the tests**

In `tests/test_explorer.py`, replace the two lines in `make_builder`:

```python
    builder.client = None
    builder.color = chess.WHITE
    builder._position_cache = {}
```

with:

```python
    builder.source = None
    builder.color = chess.WHITE
```

The `_position_cache` line goes because the cache moved into the sources.

Now update the `real_builder` fixture, which passes the first argument **by keyword** and would otherwise raise `TypeError: unexpected keyword argument 'client'`:

```python
    try:
        builder = RepertoireBuilder(
            source=object(),
            color=chess.WHITE,
            stockfish_depth=10,
        )
    except RuntimeError as exc:
        pytest.skip(f"Stockfish is not available: {exc}")
```

Re-run the search from Step 1 to confirm nothing still refers to `builder.client` or `client=`:

- [ ] **Step 5: Update the CLI to construct a source**

In `src/lichess_opening_explorer/cli.py`, change the import:

```python
from .sources import LichessDataSource
```

and in `main()`, replace:

```python
    client = LichessClient()
    try:
        builder = RepertoireBuilder(
            client,
            color,
```

with:

```python
    source = LichessDataSource()
    try:
        builder = RepertoireBuilder(
            source,
            color,
```

and the `finally` clause at the bottom:

```python
    finally:
        client.close()
```

becomes:

```python
    finally:
        source.close()
```

- [ ] **Step 6: Update the CLI tests that patch the client**

Three tests monkeypatch `cli.LichessClient`. They must patch the source instead. In `tests/test_explorer.py`, in `test_cli_wires_arguments_into_the_builder`, `test_cli_passes_a_black_repertoire_colour`, and `test_cli_reports_a_lichess_auth_error`, replace:

```python
    monkeypatch.setattr(cli, "LichessClient", FakeClient)
```

with:

```python
    monkeypatch.setattr(cli, "LichessDataSource", FakeSource)
```

and rename each `class FakeClient:` to `class FakeSource:`. In `test_cli_reports_a_lichess_auth_error`, `FakeSource.get_position` must take a **board**, not a FEN, because that is the protocol:

```python
    class FakeSource:
        def get_position(self, board):
            raise LichessAuthError("no token")

        def close(self):
            closed.append("source")
```

Update the assertion at the end of that test from `assert closed == ["builder", "client"]` to `assert closed == ["builder", "source"]`.

- [ ] **Step 7: Run the full suite**

Run:
```bash
uv run pytest -q
```

Expected: `97 passed, 6 skipped`. If any pre-existing test fails, this task is not a pure refactor — stop and investigate before continuing.

- [ ] **Step 8: Verify the real Stockfish path still works**

Run:
```bash
$env:LICHESS_STOCKFISH="C:\Stockfish\stockfish-windows-x86-64-universal.exe"
uv run pytest -q
```

Expected: `97 passed, 2 warnings`.

- [ ] **Step 9: Clear the environment variable for later tasks**

Run:
```bash
Remove-Item Env:\LICHESS_STOCKFISH
```

- [ ] **Step 10: Commit**

```bash
git add src/lichess_opening_explorer/explorer.py src/lichess_opening_explorer/cli.py tests/test_explorer.py
git commit -m "refactor: depend on DataSource instead of LichessClient"
```

---

## Task 4: `PgnIndex` and the PGN fixture helpers

**Files:**
- Create: `tests/pgn_fixtures.py`
- Create: `src/lichess_opening_explorer/pgn_index.py`
- Create: `tests/test_pgn_index.py`

**Interfaces:**
- Consumes: `models.position_key`, `models.PositionData`, `models.MoveStats`, `chess.pgn`
- Produces:
  - `PgnIndexReport` dataclass with fields `games_read`, `games_anchored`, `games_without_anchor`, `games_malformed`, `games_unfinished`, `duplicates_removed`, `positions_indexed`, `plies_recorded`, and a `summary() -> str` method
  - `PgnIndex(anchor: chess.Board, max_depth: int)` with `.get_position(board) -> PositionData`, `.covers(max_depth: int) -> bool`, `.report`, `.max_depth`, `.anchor_key`
  - `PgnIndex.from_path(path: str | Path, anchor_fen: str, max_depth: int) -> PgnIndex`
  - `pgn_fixtures.pgn_text(games, start_fen=chess.STARTING_FEN, result="1-0") -> str`
  - `pgn_fixtures.games_from_tree(anchor_fen, tree) -> list[list[str]]`
  - `pgn_fixtures.write_pgn(path, text) -> None`

- [ ] **Step 1: Create the fixture helpers**

Create `tests/pgn_fixtures.py`:

```python
"""
PGN text generated for tests.

Fixtures are built as strings rather than shipped as files so the counts in
a test are the counts in the assertion, and a broken fixture shows up as a
readable diff instead of a binary blob.
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

    A leaf is an `int` counting how many games stop there, so the totals in
    a test are exact:

        games_from_tree(fen, {"e3": {"O-O": 3}, "a3": 2})

    returns four games: three playing `e3 O-O` and one playing `e3`, then
    two playing `a3`.
    """
    games: list[list[str]] = []

    def walk(prefix: list[str], node: dict) -> None:
        for san, child in node.items():
            line = prefix + [san]

            if isinstance(child, int):
                games.extend([list(line) for _ in range(child)])
            else:
                walk(line, child)

    walk([], tree)

    return games


def write_pgn(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path
```

- [ ] **Step 2: Write the failing index tests**

Create `tests/test_pgn_index.py`:

```python
import chess
import pytest
from pgn_fixtures import games_from_tree, pgn_text, write_pgn

from lichess_opening_explorer.models import position_key
from lichess_opening_explorer.pgn_index import PgnIndex

NIMZO_FEN = (
    "rnbqk2r/ppp2ppp/4pn2/3p2B1/"
    "1bPP4/2N5/PP2PPPP/R2QKBNR w KQkq - 4 5"
)


def build(tmp_path, games, anchor_fen=NIMZO_FEN, max_depth=8, start_fen=NIMZO_FEN, result="1-0", filename="games.pgn"):
    text = pgn_text(games, start_fen=start_fen, result=result)
    path = write_pgn(tmp_path / filename, text)
    return PgnIndex.from_path(path, anchor_fen, max_depth)


def totals(index, *sans, anchor_fen=NIMZO_FEN):
    board = chess.Board(anchor_fen)
    for san in sans:
        board.push_san(san)
    data = index.get_position(board)
    return data.total, {m.san: m.total for m in data.moves}


# --- parsing and frequencies -------------------------------------


def test_index_counts_the_moves_played_in_a_position(tmp_path):
    index = build(tmp_path, [["e3"], ["e3"], ["a3"]])

    assert totals(index) == (3, {"e3": 2, "a3": 1})


def test_results_are_attributed_to_the_moves_that_were_played(tmp_path):
    """`MoveStats` carries Lichess's meaning, so `performance()` stays honest."""
    index = build(
        tmp_path,
        [
            (["e3"], "1-0"),
            (["e3"], "0-1"),
            (["a3"], "1/2-1/2"),
        ],
    )

    board = chess.Board(NIMZO_FEN)
    data = index.get_position(board)
    by_san = {m.san: m for m in data.moves}

    assert (data.white, data.black, data.draws) == (1, 1, 1)
    assert (by_san["e3"].white, by_san["e3"].black, by_san["e3"].draws) == (1, 1, 0)
    assert (by_san["a3"].white, by_san["a3"].black, by_san["a3"].draws) == (0, 0, 1)
    assert by_san["e3"].performance() == pytest.approx(0.5)


def test_a_game_starting_from_its_own_fen_is_replayed_from_that_root(tmp_path):
    """A game carrying its own [FEN] must not be replayed from move one."""
    index = build(
        tmp_path,
        [["Nf3", "e5"], ["Nf3", "c5"]],
        start_fen=NIMZO_FEN,
    )

    total, moves = totals(index, "Nf3")

    assert total == 2
    assert moves == {"e5": 1, "c5": 1}


def test_move_frequency_is_relative_to_the_position_not_the_file(tmp_path):
    """A position reached by 2 of 3 games reports 100% and 50%."""
    index = build(tmp_path, [["e3", "O-O"], ["a3"]])

    total, moves = totals(index, "e3")

    assert total == 1
    assert moves == {"O-O": 1}
    assert moves["O-O"] / total == 1.0

    root_total, root_moves = totals(index)

    assert root_total == 3
    assert root_moves["e3"] / root_total == pytest.approx(1 / 3)


def test_moves_are_ordered_by_games_played_descending(tmp_path):
    """The builder keeps the first qualifying deviations, so order matters."""
    index = build(tmp_path, [["e3"]] * 5 + [["a3"]] * 3 + [["Nf3"]])

    _, moves = totals(index)

    assert list(moves) == ["e3", "a3", "Nf3"]


def test_games_that_never_reach_the_anchor_are_excluded(tmp_path):
    index = build(tmp_path, [["e3", "O-O", "Bd3"], ["e3"]])

    root_total, _ = totals(index)

    assert root_total == 2
    assert index.report.games_anchored == 2
    assert index.report.games_without_anchor == 0
    assert index.report.games_read == 2


def test_games_from_a_different_opening_are_counted_as_missing(tmp_path):
    games = [["e4", "e5"], ["d4", "d5"]]

    index = build(tmp_path, games, start_fen=chess.STARTING_FEN, anchor_fen=NIMZO_FEN)

    assert index.report.games_read == 2
    assert index.report.games_anchored == 0
    assert index.report.games_without_anchor == 2
    assert totals(index) == (0, {})


def test_anchor_is_matched_at_a_different_move_number(tmp_path):
    """A hand-typed FEN rarely carries the games' fullmove number."""
    games = [["e4", "e5", "Nf3", "Nc6", "Bb5"], ["e4", "e5", "Nf3"]]

    reached = chess.Board(chess.STARTING_FEN)
    for san in ["e4", "e5", "Nf3"]:
        reached.push_san(san)

    index = build(
        tmp_path,
        games,
        start_fen=chess.STARTING_FEN,
        anchor_fen=reached.fen().replace(" 1 ", " 99 "),
    )

    assert index.report.games_anchored == 1
    assert totals(index, "Nc6", anchor_fen=reached.fen()) == (1, {"Nc6": 1})


# --- depth and windowing -----------------------------------------


def test_index_stops_recording_at_max_depth(tmp_path):
    index = build(tmp_path, [["e3", "O-O", "Bd3", "c5", "Nf3", "a6"]], max_depth=2)

    assert totals(index, "e3", "O-O") == (1, {"Bd3": 1})
    assert totals(index, "e3", "O-O", "Bd3")[0] == 1
    assert totals(index, "e3", "O-O", "Bd3", "c5")[0] == 0


def test_covers_reports_whether_the_window_is_deep_enough(tmp_path):
    index = build(tmp_path, [["e3"]], max_depth=4)

    assert index.covers(4) is True
    assert index.covers(5) is False


# --- transpositions ----------------------------------------------


def test_two_move_orders_reaching_one_position_merge(tmp_path):
    index = build(tmp_path, [["Bd3", "e3", "Be7"]] * 3 + [["e3", "Bd3", "Be7"]] * 2)

    total, moves = totals(index, "e3", "Bd3")

    assert total == 5
    assert moves == {"Be7": 5}
    assert totals(index, "Bd3", "e3") == (5, {"Be7": 5})


# --- filtering and error handling --------------------------------


def test_duplicate_games_are_removed_but_divergent_ones_are_kept(tmp_path):
    index = build(tmp_path, [["e3", "O-O"], ["e3", "O-O"], ["e3", "Be7"]])

    root_total, root_moves = totals(index)

    assert root_total == 2
    assert root_moves == {"e3": 2}
    assert index.report.duplicates_removed == 1
    assert totals(index, "e3") == (2, {"O-O": 1, "Be7": 1})


def test_games_with_errors_are_skipped_and_the_file_continues(tmp_path):
    good = pgn_text([["e3"]], start_fen=NIMZO_FEN)
    broken = (
        "[Event \"Broken\"]\n"
        "[Result \"1-0\"]\n"
        "[SetUp \"1\"]\n"
        f"[FEN \"{NIMZO_FEN}\"]\n\n"
        "5. Zz9 e3 *\n\n"
    )
    path = write_pgn(tmp_path / "games.pgn", broken + good)

    index = PgnIndex.from_path(path, NIMZO_FEN, 8)

    assert index.report.games_malformed >= 1
    assert totals(index) == (1, {"e3": 1})


def test_unfinished_games_are_excluded_and_counted(tmp_path):
    index = build(tmp_path, [(["e3"], "1-0"), (["a3"], "*"), (["Nf3"], "*")])

    root_total, _ = totals(index)

    assert root_total == 1
    assert index.report.games_unfinished == 2
    assert index.report.games_read == 1
    assert "2 unfinished" in index.report.summary()


def test_empty_file_yields_an_empty_index(tmp_path):
    path = write_pgn(tmp_path / "empty.pgn", "")

    index = PgnIndex.from_path(path, NIMZO_FEN, 8)

    assert index.report.games_read == 0
    assert totals(index) == (0, {})


def test_a_file_that_ends_mid_game_still_indexes_the_partial_game(tmp_path):
    """Real databases are routinely truncated. The partial game is usable."""
    text = pgn_text([["e3", "O-O"]], start_fen=NIMZO_FEN) + "[Event \"Cut\"]\n\n5. a3"
    path = write_pgn(tmp_path / "games.pgn", text)

    index = PgnIndex.from_path(path, NIMZO_FEN, 8)

    assert "e3" in totals(index)[1]
    assert index.report.games_read >= 1


def test_a_non_utf8_pgn_does_not_raise(tmp_path):
    """Old opening databases are frequently Latin-1."""
    path = tmp_path / "latin1.pgn"
    path.write_bytes(
        "[Event \"Turnier\"]\n[White \"Keres\"]\n[Result \"1-0\"]\n"
        "[SetUp \"1\"]\n"
        f"[FEN \"{NIMZO_FEN}\"]\n\n"
        "5. e3 *\n".encode("latin-1")
    )

    index = PgnIndex.from_path(path, NIMZO_FEN, 8)

    assert totals(index) == (1, {"e3": 1})


def test_side_variations_are_not_counted_as_played(tmp_path):
    """A RAV is analysis, not evidence that anyone played the line."""
    path = write_png_variation(tmp_path)

    index = PgnIndex.from_path(path, NIMZO_FEN, 8)

    _, moves = totals(index)

    assert "e3" in moves
    assert "a3" not in moves


def write_png_variation(tmp_path):
    text = (
        "[Event \"Annotated\"]\n[Result \"1-0\"]\n[SetUp \"1\"]\n"
        f"[FEN \"{NIMZO_FEN}\"]\n\n"
        "5. e3 (5. a3 $1 Bxc3) 5... O-O *\n"
    )
    return write_pgn(tmp_path / "annotated.pgn", text)


def test_a_position_revisited_in_one_game_is_not_counted_twice(tmp_path):
    """
    A shuffle returns to the anchor; the game must stay worth one game.

    `Nf3 Ng4 Ng1 Nf6` is a legal four-ply cycle that lands on the anchor
    position again, so the anchor is visited twice by a single game.
    """
    games = [["Nf3", "Ng4", "Ng1", "Nf6"]]

    index = build(tmp_path, games)

    total, moves = totals(index)

    assert index.report.games_anchored == 1

    # One game means a denominator of 1. The move played on the return
    # visit shares that same game instead of adding a second one, so the
    # numerators still sum to the denominator.
    assert total == 1
    assert moves == {"Nf3": 1, "Nf6": 1}
    assert sum(moves.values()) == total


def test_a_game_ending_at_the_anchor_counts_but_adds_no_moves(tmp_path):
    """Its games reach the denominator and no numerator, so shares sum to 1."""
    index = build(tmp_path, [["e3", "O-O"], []])

    total, moves = totals(index, "e3")

    assert total == 2
    assert moves == {"O-O": 1}
    assert sum(moves.values()) < total


def test_report_totals_are_internally_consistent(tmp_path):
    index = build(tmp_path, [["e3"]] * 4 + [["a3"]] * 2 + [["e3", "O-O"]])

    report = index.report

    assert report.games_read == report.games_anchored + report.games_without_anchor
    assert report.positions_indexed == len(index._positions)
    assert report.plies_recorded == 6
```

- [ ] **Step 3: Run the tests to verify they fail**

Run:
```bash
uv run pytest tests/test_pgn_index.py -q
```

Expected: collection error — `ModuleNotFoundError: No module named 'lichess_opening_explorer.pgn_index'`.

- [ ] **Step 4: Create `pgn_index.py`**

Create `src/lichess_opening_explorer/pgn_index.py`:

```python
"""
Opening statistics read from a local PGN file.

The whole file is never held in memory and the whole file is never
indexed.  The anchor position is known up front, so the only positions a
repertoire can ever ask about are those within `max_depth` of it; anything
beyond that is unreachable and indexing it would cost memory for nothing.
Games stream past one at a time and only a running count survives each one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import chess
import chess.pgn

from .models import MoveStats, PositionData, position_key

EMPTY = PositionData(white=0, draws=0, black=0, moves=[])


@dataclass
class PgnIndexReport:
    """What happened to the file, so a surprising tree can be explained."""

    games_read: int = 0
    games_anchored: int = 0
    games_without_anchor: int = 0
    games_malformed: int = 0
    games_unfinished: int = 0
    duplicates_removed: int = 0
    positions_indexed: int = 0
    plies_recorded: int = 0

    def summary(self) -> str:
        return (
            f"{self.games_read} read, "
            f"{self.games_anchored} reached the anchor, "
            f"{self.games_without_anchor} did not, "
            f"{self.games_malformed} malformed, "
            f"{self.duplicates_removed} duplicates, "
            f"{self.games_unfinished} unfinished, "
            f"{self.positions_indexed} distinct positions."
        )


class PgnIndex:
    """Move statistics for every position within `max_depth` of `anchor`."""

    def __init__(self, anchor: chess.Board, max_depth: int) -> None:
        self.anchor = anchor
        self.max_depth = max_depth
        self.anchor_key = position_key(anchor)
        self.report = PgnIndexReport()

        self._positions: dict[str, PositionData] = {}
        self._moves: dict[str, dict[str, MoveStats]] = {}
        self._fingerprints: set[tuple] = set()

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        anchor_fen: str,
        max_depth: int,
    ) -> PgnIndex:
        index = cls(chess.Board(anchor_fen), max_depth)
        index._load(Path(path))
        return index

    def get_position(self, board: chess.Board) -> PositionData:
        """Statistics for `board`, or an empty result if it was never played."""
        return self._positions.get(position_key(board), EMPTY)

    def covers(self, max_depth: int) -> bool:
        """Whether the index window is deep enough for a builder at that depth.

        A shallower index does not fail loudly. Positions past the window
        simply look like positions with no data, and the tree truncates
        without saying why.
        """
        return self.max_depth >= max_depth

    # -------------------------------------------------------------
    # Loading
    # -------------------------------------------------------------

    def _load(self, path: Path) -> None:
        with path.open(encoding="utf-8", errors="replace") as handle:
            while True:
                try:
                    game = chess.pgn.read_game(handle)
                except Exception:
                    # A hard structural failure leaves the stream at an
                    # unknown offset, so the remaining records cannot be
                    # told apart from garbage. Stop and report rather than
                    # index whatever happens to parse next.
                    self.report.games_malformed += 1
                    break

                if game is None:
                    break

                self._index_game(game)

        self._finalize()

    def _index_game(self, game: chess.pgn.Game) -> None:
        if game.errors:
            self.report.games_malformed += 1
            return

        result = game.headers.get("Result", "*")

        # `MoveStats` has no bucket for "played, outcome unknown", so an
        # unfinished game would have to be filed as a draw and would make
        # `performance()` lie. They are counted so the loss is visible.
        if result == "*":
            self.report.games_unfinished += 1
            return

        board = game.board()
        fingerprint = self._fingerprint(board, game)

        if fingerprint in self._fingerprints:
            self.report.duplicates_removed += 1
            return

        self._fingerprints.add(fingerprint)
        self.report.games_read += 1

        # The common case: the anchor is the standard position, so every
        # game is anchored before its first move and no per-ply comparison
        # is needed.
        anchored = self.anchor_key == position_key(board)
        recorded = 0

        if anchored:
            self.report.games_anchored += 1

        for move in game.mainline_moves():
            key = position_key(board)

            if not anchored:
                if key == self.anchor_key:
                    anchored = True
                    self.report.games_anchored += 1
                else:
                    board.push(move)
                    continue

            if recorded < self.max_depth:
                self._record(key, board, move, result)
                recorded += 1

            board.push(move)

        if not anchored:
            self.report.games_without_anchor += 1

    @staticmethod
    def _fingerprint(
        board: chess.Board,
        game: chess.pgn.Game,
    ) -> tuple:
        return (
            position_key(board),
            tuple(move.uci() for move in game.mainline_moves()),
        )

    def _record(
        self,
        key: str,
        board: chess.Board,
        move: chess.Move,
        result: str,
    ) -> None:
        position = self._positions.get(key)

        if position is None:
            position = PositionData(white=0, draws=0, black=0, moves=[])
            self._positions[key] = position
            self._moves[key] = {}

        uci = move.uci()
        table = self._moves[key]
        stats = table.get(uci)

        if stats is None:
            stats = MoveStats(uci=uci, san=board.san(move), white=0, draws=0, black=0)
            table[uci] = stats

        if result == "1-0":
            stats.white += 1
            position.white += 1
        elif result == "0-1":
            stats.black += 1
            position.black += 1
        else:
            stats.draws += 1
            position.draws += 1

        self.report.plies_recorded += 1

    def _finalize(self) -> None:
        for key, table in self._moves.items():
            self._positions[key].moves = sorted(
                table.values(),
                key=lambda stats: stats.total,
                reverse=True,
            )

        self.report.positions_indexed = len(self._positions)
```

- [ ] **Step 5: Run the index tests**

Run:
```bash
uv run pytest tests/test_pgn_index.py -q
```

Expected: all pass. Two may need adjustment: `test_games_reaching_the_anchor_twice_are_counted_once` depends on the specific shuffle being legal from the Nimzo position — if a move is not legal there, replace the game with any legal repetition sequence from that position. `test_report_totals_are_internally_consistent` asserts `plies_recorded == 6`, which is `4 e3 + 2 a3 + ...`; recount from the fixture if it differs.

- [ ] **Step 6: Run the full suite**

Run:
```bash
uv run pytest -q
```

Expected: `97 passed, 6 skipped` plus the new index tests, all passing.

- [ ] **Step 7: Commit**

```bash
git add tests/pgn_fixtures.py tests/test_pgn_index.py src/lichess_opening_explorer/pgn_index.py
git commit -m "feat: index opening statistics from a local PGN file"
```

---

## Task 5: `PgnDataSource` and the `DataSource` contract

**Files:**
- Modify: `src/lichess_opening_explorer/sources.py`
- Create: `tests/test_sources.py`

**Interfaces:**
- Consumes: `pgn_index.PgnIndex`
- Produces: `sources.PgnDataSource(index: PgnIndex)` with `.get_position(board) -> PositionData`, `.check_depth(required: int) -> None`, `.close() -> None`, `.report`

- [ ] **Step 1: Write the failing contract tests**

Create `tests/test_sources.py`:

```python
"""
The contract every data source has to honour.

These run the same assertions against both sources on purpose. A source
that reported some other meaning for `PositionData.total` would make every
frequency in the tree wrong without raising anything, so the meaning is
pinned here rather than assumed.
"""

import chess
import pytest
from pgn_fixtures import pgn_text, write_pgn

from lichess_opening_explorer.models import PositionData
from lichess_opening_explorer.pgn_index import PgnIndex
from lichess_opening_explorer.sources import LichessDataSource, PgnDataSource

NIMZO_FEN = (
    "rnbqk2r/ppp2ppp/4pn2/3p2B1/"
    "1bPP4/2N5/PP2PPPP/R2QKBNR w KQkq - 4 5"
)


def nimzo_index(tmp_path, games, max_depth=8):
    path = write_pgn(tmp_path / "games.pgn", pgn_text(games, start_fen=NIMZO_FEN))
    return PgnIndex.from_path(path, NIMZO_FEN, max_depth)


def lichess_source(total):
    class StubClient:
        def get_position(self, fen):
            return PositionData(total, 0, 0, [])

        def close(self):
            pass

    return LichessDataSource(StubClient())


@pytest.fixture(params=["lichess", "pgn"])
def source(request, tmp_path):
    if request.param == "lichess":
        return lichess_source(5)

    return PgnDataSource(nimzo_index(tmp_path, [["e3"]] * 5))


def test_returns_position_data(source):
    assert isinstance(source.get_position(chess.Board(NIMZO_FEN)), PositionData)


def test_total_is_the_number_of_games_reaching_the_position(source):
    assert source.get_position(chess.Board(NIMZO_FEN)).total == 5


def test_unknown_position_returns_an_empty_result(source):
    """A position nobody played is not an error, it is simply empty."""
    elsewhere = chess.Board(NIMZO_FEN)
    elsewhere.push_san("Nf3")
    elsewhere.push_san("O-O")

    data = source.get_position(elsewhere)

    assert data.total == 0
    assert data.moves == []


def test_move_totals_never_exceed_the_position_total(source):
    board = chess.Board(NIMZO_FEN)
    data = source.get_position(board)

    for move in data.moves:
        assert move.total <= data.total


def test_move_ucis_are_always_legal_in_the_position(source):
    board = chess.Board(NIMZO_FEN)
    data = source.get_position(board)

    for move in data.moves:
        assert board.parse_uci(move.uci) in board.legal_moves


# --- PGN specific -------------------------------------------------


def test_pgn_source_reports_its_index(tmp_path):
    source = PgnDataSource(nimzo_index(tmp_path, [["e3"], ["a3"]]))

    assert source.report.games_anchored == 2
    assert "2 reached the anchor" in source.report.summary()


def test_check_depth_rejects_a_too_shallow_index(tmp_path):
    source = PgnDataSource(nimzo_index(tmp_path, [["e3"]], max_depth=3))

    source.check_depth(3)

    with pytest.raises(ValueError, match="index covers 3 plies"):
        source.check_depth(4)


def test_pgn_source_survives_a_terminal_anchor(tmp_path):
    """A finished position has no legal moves, so the tree is simply empty."""
    mate = chess.Board()
    mate.push_san("f3")
    mate.push_san("e5")
    mate.push_san("g4")

    path = write_pgn(tmp_path / "short.pgn", pgn_text([["e3"]], start_fen=NIMZO_FEN))
    index = PgnIndex.from_path(path, mate.fen(), 8)
    source = PgnDataSource(index)

    assert source.get_position(mate).total == 0


def test_pgn_source_close_is_harmless(tmp_path):
    PgnDataSource(nimzo_index(tmp_path, [["e3"]])).close()
```

Remove the stray walrus in `test_unknown_position_returns_an_empty_result` — it should read:

```python
    data = source.get_position(elsewhere)
```

Write the file with that correction.

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
uv run pytest tests/test_sources.py -q
```

Expected: collection error — `ImportError: cannot import name 'PgnDataSource'`.

- [ ] **Step 3: Implement `PgnDataSource`**

Append to `src/lichess_opening_explorer/sources.py`:

```python
class PgnDataSource:
    """Opening statistics from a prebuilt local PGN index.

    The index does the expensive work once, at load time. Answering a
    position is then a dictionary lookup, which is what lets the same
    builder run against a local file or the network without caring.
    """

    def __init__(self, index: PgnIndex) -> None:
        self._index = index
        self.max_depth = index.max_depth

    @property
    def report(self) -> PgnIndexReport:
        return self._index.report

    def get_position(self, board: chess.Board) -> PositionData:
        return self._index.get_position(board)

    def check_depth(self, required: int) -> None:
        """Refuse to run against an index that cannot answer what is asked.

        Without this, a builder configured for a deeper tree than the index
        was built for would silently produce a truncated repertoire.
        """
        if not self._index.covers(required):
            raise ValueError(
                f"PGN index covers {self._index.max_depth} plies but "
                f"{required} were requested. Rebuild the index with "
                f"max_depth={required}."
            )

    def close(self) -> None:
        """Present so both sources are interchangeable to the caller."""
```

Add the import at the top of `sources.py`:

```python
from .pgn_index import PgnIndex, PgnIndexReport
```

- [ ] **Step 4: Run the contract tests**

Run:
```bash
uv run pytest tests/test_sources.py -q
```

Expected: all pass, including the two `test_...` cases that run twice under the `source` fixture.

- [ ] **Step 5: Run the full suite**

Run:
```bash
uv run pytest -q
```

Expected: all passing, no skips beyond the six real-Stockfish ones.

- [ ] **Step 6: Commit**

```bash
git add src/lichess_opening_explorer/sources.py tests/test_sources.py
git commit -m "feat: add PgnDataSource and pin the DataSource contract"
```

---

## Task 6: `min_root_share` and the local-denominator regression

**Files:**
- Modify: `src/lichess_opening_explorer/explorer.py` (`__init__`, `_select_moves`, `build`)
- Modify: `tests/test_explorer.py` (`make_builder` settings, new tests)

**Interfaces:**
- Consumes: `sources.DataSource`
- Produces: `RepertoireBuilder(..., min_root_share: float = 0.0)`

- [ ] **Step 1: Write the failing `min_root_share` tests**

In `tests/test_explorer.py`, add `"min_root_share": 0.0` to the `settings` dict inside `make_builder` (after `"min_move_frequency"`), and add `builder._root_games = 0` to the block of explicit attribute assignments (next to `builder._tree_cache = {}`). Without it, every existing test that calls `_select_moves` directly would raise `AttributeError`.

Then append:

```python
# =============================================================
# min_root_share
# =============================================================


def test_min_root_share_zero_is_a_no_op():
    builder = make_builder(
        min_games=1,
        min_frequency=0.1,
        min_move_frequency=0.0,
        max_moves=3,
        min_root_share=0.0,
    )
    board = chess.Board(NIMZO_FEN)

    selected = builder._select_moves(board, data_for(board, {"e3": 1, "a3": 99}))

    assert [stats.san for stats, _, _ in selected] == ["a3", "e3"]


def test_min_root_share_rejects_a_move_below_the_root_floor():
    """A 1-game line must not become a repertoire branch in a 1000-game root."""
    builder = make_builder(
        min_games=1,
        min_frequency=0.1,
        min_move_frequency=0.0,
        max_moves=3,
        min_root_share=0.01,
    )
    board = chess.Board(NIMZO_FEN)

    builder._root_games = 1000

    selected = builder._select_moves(board, data_for(board, {"e3": 999, "a3": 1}))

    # 1 < 1000 * 0.01, so the small move is dropped.
    assert [stats.san for stats, _, _ in selected] == ["e3"]


def test_min_root_share_rejects_everything_below_the_floor():
    builder = make_builder(
        min_games=1,
        min_frequency=0.1,
        min_move_frequency=0.0,
        max_moves=3,
        min_root_share=0.5,
    )
    board = chess.Board(NIMZO_FEN)

    builder._root_games = 1000

    assert builder._select_moves(board, data_for(board, {"e3": 400})) == []


def test_min_root_share_must_be_a_share():
    """`make_builder` skips __init__, so validation needs the real constructor."""
    with pytest.raises(ValueError):
        RepertoireBuilder(
            None,
            chess.WHITE,
            engine=object(),
            min_root_share=1.5,
        )
```

- [ ] **Step 2: Write the failing local-denominator regression test**

This is the named regression from spec §14.6. Append:

```python
def test_move_frequency_uses_local_denominator_not_root_size(tmp_path):
    """
    Regression: move frequency is relative to the position, never the root.

    1,000 games reach the anchor. 200 continue into one branch, where the
    three moves played were played 100 / 60 / 40 times. Those are 50% / 30%
    / 20% of the branch. Dividing by the 1,000-game root would report
    10% / 6% / 4% and quietly reorder which branches look important.
    """
    from pgn_fixtures import games_from_tree, pgn_text, write_pgn
    from lichess_opening_explorer.pgn_index import PgnIndex
    from lichess_opening_explorer.sources import PgnDataSource

    tree = {
        "e3": {
            "O-O": {"Bd3": 100, "Nf3": 60, "a3": 40},
            "Be7": 400,
        },
        "a3": 400,
    }

    games = games_from_tree(NIMZO_FEN, tree)
    assert len(games) == 1000

    path = write_pgn(tmp_path / "nimzo.pgn", pgn_text(games, start_fen=NIMZO_FEN))
    index = PgnIndex.from_path(path, NIMZO_FEN, 8)
    source = PgnDataSource(index)

    assert index.report.games_anchored == 1000

    branch = chess.Board(NIMZO_FEN)
    branch.push_san("e3")
    branch.push_san("O-O")

    data = source.get_position(branch)
    shares = {m.san: m.total / data.total for m in data.moves}

    assert data.total == 200
    assert shares == {"Bd3": 0.5, "Nf3": 0.3, "a3": 0.2}

    # The negative half: none of these is the root-divided value.
    root_divided = {"Bd3": 0.1, "Nf3": 0.06, "a3": 0.04}
    for san, wrong in root_divided.items():
        assert shares[san] != pytest.approx(wrong)
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run:
```bash
uv run pytest tests/test_explorer.py -q -k "root_share or local_denominator"
```

Expected: failures — `min_root_share` is not a constructor argument and `builder._root_games` does not exist.

- [ ] **Step 4: Add the parameter and validation**

In `src/lichess_opening_explorer/explorer.py`, add to the `__init__` signature after `min_opponent_frequency`:

```python
        min_root_share: float = 0.0,
```

Add to the docstring, after the `min_opponent_frequency` paragraph:

```
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
```

Add validation beside the existing `mistake_threshold` check:

```python
        if not 0.0 <= min_root_share <= 1.0:
            raise ValueError("min_root_share must be between 0.0 and 1.0.")
```

Add the assignment with the other opening-tree settings:

```python
        self.min_root_share = min_root_share
```

and initialise `self._root_games = 0` next to the caches.

- [ ] **Step 5: Apply the guard in `_select_moves`**

In `_select_moves`, after `if total <= 0: return []` and the `min_games` check, add:

```python
        # A move that clears every local test can still be meaningless in
        # the database as a whole. This floor is compared as a count rather
        # than a share so the threshold is exact, and it is zero by default
        # so it changes nothing until it is asked for.
        root_floor = self._root_games * self.min_root_share
```

and inside the ranking loop, after the `min_move_frequency` check, add:

```python
            if games < root_floor:
                continue
```

- [ ] **Step 6: Capture the root count in `build()`**

Replace the `build` method with:

```python
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
```

- [ ] **Step 7: Run the new tests**

Run:
```bash
uv run pytest tests/test_explorer.py -q -k "root_share or local_denominator"
```

Expected: all pass.

- [ ] **Step 8: Run the full suite**

Run:
```bash
uv run pytest -q
```

Expected: all passing.

- [ ] **Step 9: Commit**

```bash
git add src/lichess_opening_explorer/explorer.py tests/test_explorer.py
git commit -m "feat: add optional min_root_share guard with local-frequency regression"
```

---

## Task 7: Transposition regression — merge internally, preserve in output

**Files:**
- Modify: `tests/test_explorer.py`

**Interfaces:**
- Consumes: `PgnDataSource`, `PgnIndex`, `RepertoireBuilder`, `build_pgn`
- Produces: the named regression test `test_transposition_merges_stats_and_preserves_both_move_orders`

- [ ] **Step 1: Write the failing regression test**

Append to `tests/test_explorer.py`:

```python
def test_transposition_merges_stats_and_preserves_both_move_orders(tmp_path):
    """
    Regression: two required behaviours that must hold at the same time.

    Internally the two move orders are one position, so their statistics
    merge. In the exported PGN they stay two lines, because a repertoire
    that collapsed them would lose the move orders the source games
    actually used. Either half alone is a half-passing bug.
    """
    from pgn_fixtures import pgn_text, write_pgn
    from lichess_opening_explorer.pgn_index import PgnIndex
    from lichess_opening_explorer.sources import PgnDataSource

    games = [["Bd3", "e3", "Be7"]] * 30 + [["e3", "Bd3", "Be7"]] * 20

    path = write_pgn(
        tmp_path / "transpose.pgn", pgn_text(games, start_fen=NIMZO_FEN)
    )
    index = PgnIndex.from_path(path, NIMZO_FEN, 8)
    source = PgnDataSource(index)

    reached = chess.Board(NIMZO_FEN)
    reached.push_san("e3")
    reached.push_san("Bd3")

    # --- half one: statistics merge -----------------------------------
    data = source.get_position(reached)

    assert data.total == 50
    assert [(m.san, m.total) for m in data.moves] == [("Be7", 50)]

    # --- half two: both move orders survive into the PGN ---------------
    builder = make_builder(
        min_games=1,
        min_frequency=0.1,
        min_move_frequency=0.0,
        max_depth=4,
        max_moves=3,
        max_mistakes=0,
    )
    builder.analyse_position = lambda board: {}
    builder._get_position_data = lambda board: source.get_position(board)

    nodes = builder.build(NIMZO_FEN)
    output = tmp_path / "out.pgn"
    build_pgn(NIMZO_FEN, nodes, str(output))

    text = output.read_text(encoding="utf-8")

    assert "5. Bd3" in text
    assert "5. e3" in text

    # Both lines must actually replay, not merely appear as text.
    with output.open(encoding="utf-8") as handle:
        game = chess.pgn.read_game(handle)

    board = game.board()
    orders = set()

    for line in game.mainline():
        replay = board.copy(stack=False)
        orders.add(tuple(replay.san(move) for move in line))

        for move in line:
            replay.push(move)

    assert ("Bd3", "e3", "Be7") in orders
    assert ("e3", "Bd3", "Be7") in orders
```

- [ ] **Step 2: Run the test**

Run:
```bash
uv run pytest tests/test_explorer.py::test_transposition_merges_stats_and_preserves_both_move_orders -v
```

Expected: it may pass on the first run, because both behaviours already follow from the existing design. If it passes, that is a valid and useful result — the test is pinning existing behaviour against future regression. Do not weaken the assertions to make it "fail first"; if it fails, read the assertion message before changing any production code, because a failure means the merge or the output preservation is genuinely wrong.

- [ ] **Step 3: Run the full suite**

Run:
```bash
uv run pytest -q
```

Expected: all passing.

- [ ] **Step 4: Commit**

```bash
git add tests/test_explorer.py
git commit -m "test: pin transposition merge and move-order preservation"
```

---

## Task 8: CLI integration

**Files:**
- Modify: `src/lichess_opening_explorer/cli.py`
- Modify: `tests/test_explorer.py` (CLI tests)

**Interfaces:**
- Consumes: `sources.LichessDataSource`, `sources.PgnDataSource`, `pgn_index.PgnIndex`
- Produces: CLI flags `--source {lichess,pgn}`, `--pgn PATH`, `--min-root-share FLOAT`; `DEFAULT_MIN_GAMES = {"lichess": 50, "pgn": 10}`

- [ ] **Step 1: Write the failing CLI tests**

Append to `tests/test_explorer.py`:

```python
# =============================================================
# CLI -- data sources
# =============================================================


def test_pgn_source_requires_a_path(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["lichess-opening-explorer", "--source", "pgn"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1


def test_pgn_source_reports_a_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lichess-opening-explorer",
            "--source",
            "pgn",
            "--pgn",
            str(tmp_path / "absent.pgn"),
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1


def test_pgn_source_builds_and_prints_the_report(monkeypatch, tmp_path, capsys):
    from pgn_fixtures import pgn_text, write_pgn

    path = write_pgn(
        tmp_path / "games.pgn", pgn_text([["e3"]] * 3, start_fen=NIMZO_FEN)
    )

    monkeypatch.setattr(cli, "RepertoireBuilder", FakePassthroughBuilder)
    monkeypatch.setattr(cli, "build_pgn", lambda *a: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lichess-opening-explorer",
            "--source",
            "pgn",
            "--pgn",
            str(path),
            "--fen",
            NIMZO_FEN,
            "--max-depth",
            "2",
        ],
    )

    cli.main()

    out = capsys.readouterr().out

    assert "3 reached the anchor" in out
    assert "0 unfinished" in out


def test_pgn_report_tells_the_user_when_no_game_reached_the_anchor(
    monkeypatch, capsys, tmp_path
):
    """
    A silently empty repertoire is the worst failure mode here.

    Anchoring on a position nobody in the file ever played is easy to do
    by accident, and the run would otherwise look like it simply found
    no opening. The report has to say so.
    """
    from pgn_fixtures import pgn_text, write_pgn

    path = write_pgn(
        tmp_path / "other.pgn",
        pgn_text([["e3", "Bd3"]], start_fen=NIMZO_FEN),
    )

    monkeypatch.setattr(cli, "build_pgn", lambda *a: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lichess-opening-explorer",
            "--source",
            "pgn",
            "--pgn",
            str(path),
            # The starting position of a Ruy Lopez, which this file
            # never reaches.
            "--fen",
            "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4",
        ],
    )

    cli.main()

    out = capsys.readouterr().out

    assert "1 read" in out
    assert "0 reached the anchor" in out
    assert "1 did not," in out
    assert "0 distinct positions" in out


def test_min_games_default_depends_on_the_source(monkeypatch, tmp_path):
    from pgn_fixtures import pgn_text, write_pgn

    path = write_pgn(
        tmp_path / "games.pgn", pgn_text([["e3"]], start_fen=NIMZO_FEN)
    )

    captured = {}

    class RecordingBuilder:
        def __init__(self, source, color, min_games, threshold, **kwargs):
            captured["min_games"] = min_games
            captured["source"] = source

        def build(self, fen):
            return []

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(cli, "RepertoireBuilder", RecordingBuilder)
    monkeypatch.setattr(cli, "build_pgn", lambda *a: None)

    monkeypatch.setattr(
        sys,
        "argv",
        ["lichess-opening-explorer", "--source", "pgn", "--pgn", str(path), "--fen", NIMZO_FEN],
    )
    cli.main()

    assert captured["min_games"] == 10
    assert isinstance(captured["source"], PgnDataSource)

    # An explicit value always wins over the per-source default.
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lichess-opening-explorer",
            "--source",
            "pgn",
            "--pgn",
            str(path),
            "--fen",
            NIMZO_FEN,
            "--min-games",
            "77",
        ],
    )
    cli.main()

    assert captured["min_games"] == 77


def test_min_root_share_is_passed_through(monkeypatch, tmp_path):
    from pgn_fixtures import pgn_text, write_pgn

    path = write_pgn(
        tmp_path / "games.pgn", pgn_text([["e3"]], start_fen=NIMZO_FEN)
    )
    captured = {}

    class RecordingBuilder:
        def __init__(self, source, color, min_games, threshold, **kwargs):
            captured.update(kwargs)

        def build(self, fen):
            return []

        def close(self):
            pass

    monkeypatch.setattr(cli, "RepertoireBuilder", RecordingBuilder)
    monkeypatch.setattr(cli, "build_pgn", lambda *a: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lichess-opening-explorer",
            "--source",
            "pgn",
            "--pgn",
            str(path),
            "--fen",
            NIMZO_FEN,
            "--min-root-share",
            "0.05",
        ],
    )

    cli.main()

    assert captured["min_root_share"] == 0.05
```

Add `from lichess_opening_explorer.sources import LichessDataSource, PgnDataSource` to the imports at the top of `tests/test_explorer.py`.

Define the passthrough builder used by `test_pgn_source_builds_and_prints_the_report` at module level in `tests/test_explorer.py`:

```python
class FakePassthroughBuilder:
    """A builder stand-in for CLI tests that only care about wiring."""

    def __init__(self, source, color, min_games, threshold, **kwargs):
        self._source = source

    def build(self, fen):
        return self._source.get_position(chess.Board(fen)).moves

    def close(self):
        pass
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
uv run pytest tests/test_explorer.py -q -k "pgn_source or min_games_default or root_share_is_passed"
```

Expected: failures — `--source` is not a recognised argument.

- [ ] **Step 3: Add the CLI arguments**

In `src/lichess_opening_explorer/cli.py`, add after the `--fen` argument:

```python
    parser.add_argument(
        "--source",
        choices=["lichess", "pgn"],
        default="lichess",
        help="Where opening statistics come from (default: lichess)",
    )
    parser.add_argument(
        "--pgn",
        default=None,
        help="Path to a local PGN file (required with --source pgn)",
    )
```

Change the `--min-games` default to `None` and update its help text:

```python
    parser.add_argument(
        "-n",
        "--min-games",
        type=int,
        default=None,
        help=(
            "Stop exploring a position played fewer than this many games "
            "(default: 50 for lichess, 10 for pgn)"
        ),
    )
```

Add after the `--min-opponent-frequency` argument:

```python
    parser.add_argument(
        "--min-root-share",
        type=float,
        default=0.0,
        help=(
            "Optional floor: a repertoire move must represent at least this "
            "share of the games that reached the starting position. Disabled "
            "at 0 (default: 0)"
        ),
    )
```

- [ ] **Step 4: Wire the source selection into `main()`**

Add the imports:

```python
from pathlib import Path

from .pgn_index import PgnIndex
from .sources import LichessDataSource, PgnDataSource
```

Add the defaults map just after `build_parser`:

```python
# A Lichess default of 50 against a 1,000-game PGN yields a two-ply tree,
# and there is no way for a user to guess why from the output.
DEFAULT_MIN_GAMES = {"lichess": 50, "pgn": 10}
```

In `main()`, after the FEN validation and before the summary print, add:

```python
    min_games = (
        args.min_games
        if args.min_games is not None
        else DEFAULT_MIN_GAMES[args.source]
    )
```

Replace the `f"Min games: {args.min_games}, ..."` line with `f"Min games: {min_games}, ..."`.

Replace the source construction block with:

```python
    if args.source == "pgn":
        if not args.pgn:
            print(
                "--source pgn requires --pgn PATH",
                file=sys.stderr,
            )
            sys.exit(1)

        pgn_path = Path(args.pgn)

        if not pgn_path.is_file():
            print(f"PGN file not found: {pgn_path}", file=sys.stderr)
            sys.exit(1)

        index = PgnIndex.from_path(pgn_path, args.fen, args.max_depth)
        source = PgnDataSource(index)
        source.check_depth(args.max_depth)
        print(index.report.summary())
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
            max_mistakes=args.max_mistakes,
            min_root_share=args.min_root_share,
        )
```

Keep the existing inner `try/finally` around `build` and `build_pgn`, and keep the outer `finally: source.close()`.

- [ ] **Step 5: Run the new CLI tests**

Run:
```bash
uv run pytest tests/test_explorer.py -q -k "pgn_source or min_games_default or root_share_is_passed"
```

Expected: all pass.

- [ ] **Step 6: Run the full suite**

Run:
```bash
uv run pytest -q
```

Expected: all passing.

- [ ] **Step 7: Verify the help text lists the new flags**

Run:
```bash
uv run lichess-opening-explorer --help
```

Expected: `--source`, `--pgn`, and `--min-root-share` all appear.

- [ ] **Step 8: Commit**

```bash
git add src/lichess_opening_explorer/cli.py tests/test_explorer.py
git commit -m "feat: add --source pgn CLI integration"
```

---

## Task 9: End-to-end validation and README

**Files:**
- Modify: `README.md`
- Create: nothing (the validation PGN is generated into the temp directory)

**Interfaces:**
- Consumes: everything from Tasks 1-8
- Produces: documented CLI surface, verified end-to-end run

- [ ] **Step 1: Generate a realistic 1,000-game Nimzo PGN**

Create a throwaway script in the temp directory, not in the repo, at
`C:\Users\AHMED\AppData\Local\Temp\opencode\make_nimzo_pgn.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"C:\Users\AHMED\lichess-opening-explorer\tests")))

from pgn_fixtures import games_from_tree, pgn_text

NIMZO = (
    "rnbqk2r/ppp2ppp/4pn2/3p2B1/"
    "1bPP4/2N5/PP2PPPP/R2QKBNR w KQkq - 4 5"
)

TREE = {
    "e3": {
        "O-O": {
            "Bd3": {"c5": 180, "Be7": 90},
            "Nf3": {"c5": 60, "Nc6": 40},
            "a3": 30,
        },
        "Be7": 250,
    },
    "a3": {"Bxc3": 200, "d5": 60},
    "Nf3": {"O-O": 70, "d5": 50},
}

games = games_from_tree(NIMZO, TREE)
print("games:", len(games))

out = Path(r"C:\Users\AHMED\AppData\Local\Temp\opencode\nimzo-1000.pgn")
out.write_text(pgn_text(games, start_fen=NIMZO), encoding="utf-8")
print("wrote", out)
```

Run it:

```bash
uv run python "C:\Users\AHMED\AppData\Local\Temp\opencode\make_nimzo_pgn.py"
```

Expected: prints the game count and the output path. Confirm the count is 1000; if the tree above sums to a different number, adjust the leaf counts and note the final distribution, because the README will quote it.

- [ ] **Step 2: Run the tool against the generated database**

```bash
$env:LICHESS_STOCKFISH="C:\Stockfish\stockfish-windows-x86-64-universal.exe"
uv run lichess-opening-explorer --source pgn --pgn "C:\Users\AHMED\AppData\Local\Temp\opencode\nimzo-1000.pgn" --fen "rnbqk2r/ppp2ppp/4pn2/3p2B1/1bPP4/2N5/PP2PPPP/R2QKBNR w KQkq - 4 5" --max-depth 6 --min-games 20 -o "C:\Users\AHMED\AppData\Local\Temp\opencode\nimzo-repertoire.pgn"
```

Expected: the report line shows `1000 read`, `1000 reached the anchor`, and a plausible distinct-position count. The run takes a while because every position in the tree is analysed by Stockfish.

- [ ] **Step 3: Add the Stockfish integration test for a PGN source**

The unit tests stub the engine, so nothing yet proves that mistake detection works end to end against real PGN data. Create `tests/test_pgn_stockfish.py`, following the module-scoped skip fixture used by `real_builder` in `tests/test_explorer.py`:

```python
"""Mistake detection against a real PGN source and a real engine.

These are the only tests that prove the two halves of the feature meet:
positions indexed from a file, evaluated by Stockfish, then punished.
"""

import sys
from pathlib import Path

import chess
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from pgn_fixtures import pgn_text, write_pgn

from lichess_opening_explorer.explorer import RepertoireBuilder
from lichess_opening_explorer.pgn_index import PgnIndex
from lichess_opening_explorer.sources import PgnDataSource

NIMZO_FEN = (
    "rnbqk2r/ppp2ppp/4pn2/3p2B1/"
    "1bPP4/2N5/PP2PPPP/R2QKBNR w KQkq - 4 5"
)


def build_source(tmp_path, games, max_depth=6):
    path = write_pgn(
        tmp_path / "games.pgn", pgn_text(games, start_fen=NIMZO_FEN)
    )
    return PgnDataSource(PgnIndex.from_path(path, NIMZO_FEN, max_depth))


@pytest.fixture
def real_pgn_builder(tmp_path):
    try:
        builder = RepertoireBuilder(
            source=build_source(tmp_path, [["e3", "Bd3"]] * 10),
            color=chess.WHITE,
            stockfish_depth=10,
            min_games=1,
            min_frequency=0.1,
            max_depth=1,
            max_moves=3,
            max_mistakes=1,
        )
    except RuntimeError as exc:
        pytest.skip(f"Stockfish is not available: {exc}")

    yield builder
    builder.close()


def test_a_playable_move_nobody_played_is_never_a_deviation(real_pgn_builder):
    """
    The source is the only thing that nominates moves.

    `e4` and `Nf3` are perfectly legal in this position and Stockfish
    would happily evaluate either, but nobody in this database played
    them, so they must not become repertoire lines or steal the MAIN
    label from `e3`.
    """
    nodes = real_pgn_builder.build(NIMZO_FEN)

    assert [node.san for node in nodes] == ["e3"]


def test_a_played_but_bad_move_is_classified_and_punished(tmp_path):
    """The engine must be able to call a real played move a mistake."""
    try:
        builder = RepertoireBuilder(
            source=build_source(tmp_path, [["Nf6", "Nxe4", "Qe2"]] * 20),
            color=chess.WHITE,
            stockfish_depth=10,
            min_games=1,
            min_frequency=0.1,
            max_depth=3,
            max_moves=3,
            max_mistakes=1,
        )
    except RuntimeError as exc:
        pytest.skip(f"Stockfish is not available: {exc}")

    try:
        mistakes = builder._find_opponent_mistakes(chess.Board(NIMZO_FEN))
    finally:
        builder.close()

    assert mistakes, "a badly played move in a 20-game line must be flagged"
    assert mistakes[0]["san"] == "Nf6"
    assert "punishment" in mistakes[0]
```

Run them:

```bash
$env:LICHESS_STOCKFISH="C:\Stockfish\stockfish-windows-x86_64-universal.exe"
uv run pytest tests/test_pgn_stockfish.py -v
```

Expected: 2 passed. If `test_a_played_but_bad_move_is_classified_and_punished` reports no mistake, the line needs a genuinely losing move — inspect the real score before changing the threshold. Do not weaken the assertion to `assert mistakes is None`.

- [ ] **Step 4: Validate the exported PGN recursively**

Reuse the validator pattern from the earlier work: walk every variation, replay it from the anchor board, and assert each SAN equals `board.san(move)`. Every line must be legal.

- [ ] **Step 5: Confirm branch depth actually varies**

Read the exported PGN and check that the well-supported branch (`e3 O-O Bd3 c5`, 180 games) reaches further than the thin one (`e3 O-O a3`, 30 games). If every branch stops at the same depth, the local gate is not working and Task 6 needs revisiting.

- [ ] **Step 6: Update the README**

Add these rows to the flag table:

```
| `--source` | `lichess` | `lichess` or `pgn` — where opening statistics come from |
| `--pgn` | — | Path to a local PGN file (required with `--source pgn`) |
| `--min-root-share` | 0 | Optional floor on a move's share of the games that reached the starting position (disabled at 0) |
```

Change the `-n`, `--min-games` row's default cell to `50` / `10` with the note "50 for lichess, 10 for pgn".

Add a "Local PGN database" section after "Setup" covering:

- `--source pgn --pgn games.pgn` with no token and no network.
- The anchor FEN must be a position your games actually reach; the report line tells you how many did.
- Move frequencies are always relative to the position being expanded, never to the file size.
- Branch depth varies with game support; `--min-games` is the local gate.
- Side variations are not counted; unfinished (`*`) games are excluded and reported.
- Transpositions merge internally, and every move order is kept in the output.
- **Mistakes are found more shallowly than with Lichess Masters**, because a 1,000-game database has few games in deep positions, so there are fewer real deviations to analyse. This is a property of the data, not a fault.

Update the "Termination" bullet to mention that a position also stops when it is reached by fewer than `--min-games` games.

- [ ] **Step 7: Run the full suite one final time**

```bash
uv run pytest -q
Remove-Item Env:\LICHESS_STOCKFISH
uv run pytest -q
```

Expected: all passing with and without Stockfish present.

- [ ] **Step 8: Commit**

```bash
git add README.md
git commit -m "docs: document the local PGN source"
```

- [ ] **Step 9: Report to the user**

Report the final test count, the index report from Step 2, the observed branch-depth spread from Step 5, the Stockfish test result from Step 3, and the `git log --oneline` for the branch. Do not push.
