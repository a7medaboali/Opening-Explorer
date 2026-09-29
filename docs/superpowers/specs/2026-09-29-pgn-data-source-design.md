# Local PGN Opening Database — Design

- **Date:** 2026-09-29
- **Status:** Draft for review. Not implemented.
- **Author:** brainstorming session with the project owner

---

## 1. Motivation

The tool currently answers one question: *"what moves did Lichess Masters players
actually play in this position, and how often?"* That single question drives the whole
repertoire — which branches exist at all, and which opponent deviations are realistic
enough to be worth analysing.

That is a good default, but it is not the only good default. A user with 1,000 high-level
Nimzo-Indian games has a database that is far more relevant to their own repertoire than
the global Masters pool, and it contains lines the Masters database barely covers.

This feature adds a second source of the same statistic: a local PGN file.

The governing principle is a strict separation of concerns:

```
PGN database  ->  what is REALISTIC (which branches exist, how often)
Stockfish     ->  what is GOOD   (severity, punishment lines)
```

The PGN decides which branches exist. Stockfish never influences that. Stockfish only
grades opponent deviations that the PGN says were actually played, and writes the
punishment. This is what separates "realistic and bad" from "every legal capture".

---

## 2. Goals and non-goals

### Goals

1. Accept a local PGN file as an alternative to the Lichess Masters API.
2. Build a genuinely branching, position-keyed opening tree from real games.
3. Preserve the existing `MoveNode` / PGN export pipeline unchanged.
4. Keep the Lichess source fully working, unchanged in behaviour.
5. Make move frequencies **position-relative**, always.
6. Let branch depth vary naturally with how many games support it.
7. Work with no network access and no token.

### Non-goals

1. **Not** a separate repertoire builder. The existing `RepertoireBuilder` is reused.
2. **Not** an opening *book* or engine-line generator. It reports what was played.
3. **Not** a transposition-table storage engine for very large databases. See §10.
4. **Not** a `StockfishAnalyzer` extraction. Explicitly deferred (§17).
5. **Not** counting RAV / sideline variations as played moves (§7).
6. **Not** a web UI, database format, or incremental-update story.

---

## 3. Key architectural finding

Before designing anything new, the existing coupling was measured. `RepertoireBuilder`
has exactly **one** dependency on its data source:

```python
# explorer.py:629
data = self.client.get_position(fen=position_key(board))
```

Every downstream use of that data was traced. The real contract is much narrower than
`PositionData` appears:

| Consumer | Fields actually read |
|---|---|
| `_select_moves` | `data.total`, `data.moves[].total`, `data.moves[].uci` |
| `_candidate_opponent_moves` | `data.total`, `data.moves[].total`, `data.moves[].uci` |
| `_find_opponent_mistakes` | `data.total`, `data.moves[].total`, `data.moves[].uci` |

**Never read by the builder:** `PositionData.white/draws/black`, `MoveStats.san`,
`MoveStats.performance()`.

The builder derives SAN from the board (`explorer.py:792`) and ranks purely by `.total`.

**Consequence:** introducing a data source abstraction is a small, low-risk change.
Selection, pruning, mistake detection, punishment, tree building, and PGN export are all
reused as-is, already covered by 94 passing tests.

---

## 4. The `DataSource` abstraction

### 4.1 Shape

A one-method `typing.Protocol`, not an abstract base class. The project targets Python
3.13+, and structural typing means the existing fake client used throughout
`tests/test_explorer.py` satisfies the interface **without modification** — no test
churn, no inheritance required.

```python
# sources.py
class DataSource(Protocol):
    def get_position(self, board: chess.Board) -> PositionData:
        """Statistics for the position `board` is in.

        `PositionData.total` MUST be the number of games that reached this
        position, and each `MoveStats.total` MUST be the number of those games
        that played that move. Returning an empty PositionData for an unknown
        position is valid and means "no data here".
        """
```

A `Protocol` rather than an ABC is chosen deliberately: it is the smallest possible
contract, and it keeps the tests decoupled from the implementation.

### 4.2 Implementations

| Class | Backing | Notes |
|---|---|---|
| `LichessDataSource` | `LichessClient` + network | Converts `board` to `position_key`, delegates, caches |
| `PgnDataSource` | `PgnIndex` | Dict lookup, plus one guard method (below) |

`PgnDataSource` carries a single method beyond the protocol:

```python
def check_depth(self, required: int) -> None:
    """Raise ValueError if the index is shallower than the builder needs.

    The index is built for a specific `max_depth`. If the builder is later
    given a larger one, positions past the index window look like positions
    with no data, and the tree would silently truncate. That failure is
    invisible in the output, so it is turned into an explicit error.
    """
```

The CLI calls `check_depth(args.max_depth)` immediately after constructing the builder.

### 4.3 Cache ownership moves

`LichessClient` already caches by FEN (`api.py:98`) and `RepertoireBuilder` caches again
by `position_key` (`explorer.py:132`). With the abstraction in place the duplication is
pointless, so caching moves entirely into the data sources and `builder._position_cache`
is deleted. Both caches key on the same four-field FEN, so behaviour is unchanged.

### 4.4 Builder change

`RepertoireBuilder.__init__` takes `source: DataSource` instead of `client: LichessClient`.
`_get_position_data` becomes a thin delegation to `self.source.get_position(board)`.
The parameter is renamed to `source`; it is currently passed positionally from
`cli.py:140`.

---

## 5. The `PgnIndex`

The key insight: **the entire PGN is never indexed.**

The anchor position is supplied up front, so the only positions the builder can ever ask
about are those within `max_depth` of the anchor. Indexing anything else is wasted work
and wasted memory.

`PgnIndex` and `PgnDataSource` are deliberately **separate**:

- `PgnIndex` — expensive, offline, streaming, owns memory and the parse report.
- `PgnDataSource` — trivial, one dict lookup, satisfies `DataSource`.

Collapsing them would force the interface to carry indexing concerns it does not need.

### 5.1 Build algorithm

One streaming pass over the file. Games are never accumulated in a list.

```
anchor_key = position_key(anchor_board)
seen_fingerprints = set()

for each game yielded by the PGN reader:      # streaming, one game at a time
    result = game.headers.get("Result", "*")
    if game.errors:                            -> games_malformed += 1;  continue
    if result == "*":                          -> games_unfinished += 1; continue
    fingerprint = (position_key(game.board()), tuple of mainline UCI)
    if fingerprint in seen_fingerprints:       -> duplicates_removed += 1; continue
    seen_fingerprints.add(fingerprint)

    games_read += 1
    board = game.board()
    anchored = (anchor_key == position_key(board))   # fast path for standard start
    if anchored: games_anchored += 1
    plies_since_anchor = 0

    for move in game.mainline_moves():
        key = position_key(board)
        if not anchored:
            if key == anchor_key:
                anchored = True
                games_anchored += 1
            else:
                board.push(move); continue
        if plies_since_anchor < max_depth:
            record(key, move, result)
            plies_since_anchor += 1
        board.push(move)
```

Notes:

- The anchor comparison uses `position_key`, which **drops the halfmove and fullmove
  clocks**. A game that reaches the anchor at a different move number still matches an
  anchor FEN typed by hand. This reuse is not merely convenient, it is required.
- `record(key, move, result)` means: look up or create the `PositionData` for `key`, then
  the `MoveStats` for `move.uci()`, and increment the appropriate result bucket.
- The fast path (`anchored = True` before the loop) applies when the anchor is the
  standard starting position, which is the common case. It skips a per-ply FEN
  comparison on every game.
- Recording plies `0 .. max_depth-1` produces positions at depths `1 .. max_depth`. The
  position at depth `max_depth` is never expanded (the builder returns early at
  `depth >= max_depth`), but recording it costs almost nothing and keeps the index
  correct if the ceiling is later raised.

### 5.2 `PgnIndexReport`

The user needs to know what happened to their file. Counters surfaced by the CLI:

| Field | Meaning |
|---|---|
| `games_read` | Games accepted and indexed |
| `games_anchored` | Games that reached the anchor (**the denominator at the root**) |
| `games_without_anchor` | Parsed cleanly but never reached the anchor |
| `games_malformed` | `game.errors` non-empty; skipped |
| `games_unfinished` | Result is `*`; skipped (§7.4) |
| `duplicates_removed` | Identical move sequence already seen |
| `positions_indexed` | Distinct positions stored |
| `plies_recorded` | Total move occurrences recorded |

---

## 6. Statistical semantics

### 6.1 Denominators

**The denominator for move frequency is always the number of games that reached the
current position.** Not the number of games in the file, not the number of games at the
root.

At the anchor it is `games_anchored`. At every deeper position it is that position's own
`PositionData.total`. This falls out of the index construction automatically.

### 6.2 Do not renormalize

At the deepest recorded ply, `sum(move totals) < position total`, because games that
ended there — or that were truncated by the `max_depth` ceiling — contribute to the
denominator and to no numerator. This is correct. **Move frequencies must never be
normalised by the sum of the move totals.** Doing so would inflate the last ply of every
branch to 100%.

### 6.3 Result attribution

The PGN has results, and they map onto the existing model with no loss:

| Bucket | Condition |
|---|---|
| `MoveStats.white` | The move was played in a game White won |
| `MoveStats.black` | The move was played in a game Black won |
| `MoveStats.draws` | The move was played in a drawn game |

This is exactly Lichess' meaning and makes `MoveStats.performance()` correct. The builder
does not read these fields today, so this costs nothing — but it means the model carries
real information and a future feature (e.g. win-rate-weighted selection) works for both
sources.

### 6.4 Explicit threshold definitions

These definitions are binding. They are the contract every threshold in §8 implements.

| Term | Exact definition |
|---|---|
| `min_games` | The minimum number of games that reached the **current** position. It is a position-expansion gate, compared against `PositionData.total` of the position being expanded. |
| `min_move_frequency` | The minimum share of games that reached the **current** position **and** played that specific move. It is `MoveStats.total / PositionData.total` of the position the move is played from. |
| Denominator for move frequency | **Always** the number of games that reached the current position. |
| Root database size | **Never** used as the denominator for move frequency. |

**The root database size is never a denominator for move frequency.** It appears in
exactly one place: the optional `min_root_share` guard (§8.2), which exists to *reject*
statistically tiny lines and is disabled by default. Every other frequency in the tree is
computed strictly locally.

The single most important consequence, and the one most likely to be implemented wrongly:

> With 1,000 games at the root and 200 games in a branch, where the branch's moves were
> played 100 / 60 / 40 times, the frequencies are **50% / 30% / 20%**.
>
> They are **not** 10% / 6% / 4%. Dividing by the root size is a bug.

This is pinned by a named regression test — see §14.6.

### 6.5 Transpositions

Two different move orders reaching one position **merge** in the index. Their counts add,
because they are the same position reached two ways. This is the entire point of using a
position key.

**Both move orders are preserved in the exported PGN.** The builder's `_tree_cache`
(`explorer.py:134`) is keyed on `(position_key, depth)`, so both orders receive the same
continuation subtree and both appear in the output. The result is a legal PGN that keeps
the real move-order information from the source games. This matches existing Lichess
behaviour and is a deliberate decision, not an oversight.

Two properties are therefore required simultaneously, and both are pinned by a named
regression test (§14.6):

1. **Internal merge** — statistics for the position are combined across all move orders.
2. **Output preservation** — every distinct move order that reached it appears as its own
   line in the exported PGN.

Merging internally and collapsing in the output would satisfy the first and violate the
second, so the test must assert both halves together.

---

## 7. Edge cases

### 7.1 Malformed PGN

`game.errors` non-empty → counted in `games_malformed`, game skipped, file processing
continues. A single bad game must never abort a 1,000-game run.

`read_game` is additionally wrapped in `try/except` for hard structural failures so one
unparseable record cannot kill the run.

### 7.2 Duplicate games

Deduplicated on `(position_key(game.board()), tuple of mainline UCI)`. Only *identical*
games collapse. Two Nimzo games that share four moves and then diverge are **not**
duplicates and both count fully.

### 7.3 Non-standard starting positions

`game.board()` already honours `[FEN]` / `[SetUp "1"]`, and falls back to the standard
starting position. Each game is therefore replayed from its own correct root, and the
anchor comparison happens against that game's own board.

### 7.4 Unfinished games (`Result: *`)

Excluded from the index and counted as `games_unfinished`. Rationale: `MoveStats` has no
bucket for "played, outcome unknown", so including them would force a false attribution
into `draws` and silently corrupt `performance()`. GM game databases carry results, so
this costs nothing in practice. A flag to include them is deferred.

**The count is mandatory, not incidental.** `games_unfinished` is reported in
`PgnIndexReport` and printed by the CLI alongside the other counters, so a user whose
index is smaller than expected can see how many games were dropped for this reason rather
than wondering where they went. Exclusion without reporting would be a silent data loss.

### 7.5 RAV / side variations

**Mainline moves only.** A variation (`(`...`)`) is analysis, commentary, or an
alternative the annotator chose to show — it is not evidence that a player reached that
position in a game. Counting sidelines would inflate frequencies and create branches
nobody played.

`game.mainline_moves()` gives exactly this. Variations are parsed and discarded.

### 7.6 Games reaching the anchor more than once

A game can revisit a normalised position by repetition. The game is anchored at its
**first** occurrence and recorded from there for up to `max_depth` plies. It is never
counted twice.

### 7.7 Empty file, or no game reaches the anchor

Not an error. The index is empty, the anchor position has `total == 0`, and the builder
prunes immediately, producing an empty tree. The CLI reports the counters so the cause is
obvious rather than mysterious.

### 7.8 Games shorter than the anchor

Excluded naturally: the anchor is never reached, so `games_without_anchor` counts them.

---

## 8. Depth and branching rules

The tree must be **genuinely data-driven**. Branch depth is a consequence of game
support, not a fixed setting.

| Rule | Type | Flag | Lichess default | PGN default |
|---|---|---|---|---|
| Maximum depth | hard ceiling, plies | `--max-depth` | 10 | 10 |
| Position expansion gate | local absolute count | `--min-games` | 50 | 10 |
| Branch selection | local relative share | `--min-move-frequency` | 0.05 | 0.05 |
| Cumulative coverage | local relative share | `--threshold` | 0.8 | 0.8 |
| Branching cap | count per position | `--max-moves` | 3 | 3 |
| Root safety guard | root-relative share, off by default | `--min-root-share` | 0.0 | 0.0 |

**A position stops expanding when any of these is true:**

1. `depth >= max_depth` — hard ceiling.
2. `position.total < min_games` — too few games reached it.
3. No move clears `min_move_frequency`.
4. No move clears `min_root_share` (when enabled).
5. The index holds no move for the position.
6. `max_moves` qualifying moves have already been selected.

### 8.1 Why an absolute local floor plus a relative move filter is correct

The concern that a fixed absolute `min_games` "terminates the tree unnaturally early" is
valid in general but is already mitigated by the existing design, and the mitigation
should be preserved rather than replaced:

- `min_games` is compared against the **local** count at each position
  (`explorer.py:666`). It is a position-expansion gate, not a global filter.
- `min_move_frequency` is **relative** to the position's own games
  (`explorer.py:677`).

Given a 1,000-game database decaying 600 / 400 / 250 / 180, a local floor of 10 lets the
600-game branch run to the depth ceiling while killing a 25-game branch at ply 3. The
decay itself is the terminator. This is exactly the requested behaviour, and it is what
the code already does.

The genuinely new tool is `min_root_share`.

### 8.2 `min_root_share`

An optional, **root-relative** guard. A move must represent at least
`min_root_share × games_anchored` games to become a branch.

Purpose: a 3-game line at depth 8 inside a 1,000-game database is statistically
meaningless as a *repertoire* line even though it clears every local test. This guard
rejects it.

- Default `0.0` (disabled), so it changes nothing until explicitly requested.
- Implemented in the builder as a general option, not PGN-specific, so it works for
  either source.
- Compared as counts (`games < root_games * min_root_share`) rather than as a
  frequency, to avoid float edge cases.
- `root_games` is captured once in `build()` from the anchor position's `total`.

Note the deliberate asymmetry with opponent deviations: `min_root_share` guards
*repertoire* lines, where commonness matters. It is **not** applied to opponent
deviations, where a rare move is precisely the interesting case (§9).

---

## 9. Interaction with Stockfish mistake detection

**No new code is required.** This is the strongest argument for reusing the builder.

`_candidate_opponent_moves` (`explorer.py:411`) asks the data source for the moves the
position actually records, filters by `min_opponent_frequency`, and caps at
`max_deviation_moves`. `_find_opponent_mistakes` then grades exactly those moves with
Stockfish. Swapping the source swaps the candidate set from "Masters players" to "your
PGN players" with no other change.

The two frequency filters have **opposite intent** and both are preserved:

| Filter | Applied to | Intent | PGN default |
|---|---|---|---|
| `min_move_frequency` (0.05) | our repertoire moves | want **common** | strict |
| `min_opponent_frequency` (0.001) | opponent deviations | want **realistic and bad**; blunders are rare | loose |
| `min_root_share` (0.0) | our repertoire moves | reject statistically tiny lines | off |

### 9.1 Expected shallower mistake detection — documented, not a bug

In a 1,000-game PGN, deep positions may be supported by only a handful of games. The
`move_data.total < 2` guard (`explorer.py:446`) then rejects nearly every candidate.

**Mistake detection will be inherently shallower with a PGN source than with Lichess
Masters.** This is a consequence of data volume, not a design flaw, and the README must
say so plainly so users do not file it as a regression.

---

## 10. Performance

### 10.1 Measured expectations

| Games | PGN parse | Index entries (pre-merge) | Realistic after merge | Resident |
|---|---|---|---|---|
| 1,000 | < 1 s | ≤ 10,000 | few hundred – few thousand | < 10 MB |
| 10,000 | a few seconds | ≤ 100,000 | — | ~10–40 MB |
| 100,000+ | minutes | millions | — | needs on-disk |

Transposition merging collapses the index hard, because sibling branches reconverge. The
`games × max_depth` figure is a worst case that assumes no merging at all.

### 10.2 Inner-loop cost

`board.fen()` is called once per ply and dominates parsing. Acceptable at these scales.
A cheaper position key is a possible future optimisation and is explicitly **not** in
scope — correctness and reuse of the existing `position_key` matter more than speed here.

### 10.3 The real bottleneck is Stockfish

PGN parsing is not the limiting factor. `analyse_position` is already cached by
`position_key` (`explorer.py:196`), so transposed positions cost one engine call, not two.
A 10,000-game PGN run is bounded by engine depth and the number of positions in the tree,
exactly as a Lichess run is.

### 10.4 Scaling escape hatch

`PgnIndex` is a self-contained class with a dict-like `get_position`. If very large
databases ever need support, swapping the dict for a sqlite-backed index is an internal
change with no effect on `PgnDataSource` or the builder. Deliberately not built now.

### 10.5 A genuine side benefit

PGN mode needs **no network and no token**. The tool becomes fully usable offline, and
the 61-second retry wait (`api.py:112`) is irrelevant.

---

## 11. CLI design

```
--source {lichess,pgn}    default: lichess
--pgn PATH                required if and only if --source pgn
--min-root-share FLOAT    default: 0.0 (disabled)
```

### 11.1 Per-source defaults

`--min-games` becomes source-dependent, because a single default cannot serve two scales:

| | Lichess | PGN |
|---|---|---|
| `--min-games` | 50 | 10 |

A Lichess default of 50 against a 1,000-game PGN yields a 2-ply tree, and a user has no
way to guess why. This is a usability requirement, not a nicety.

Implemented as `default=None` on the parser, resolved after parsing, so an explicit
`--min-games` always wins.

### 11.2 Everything else is unchanged

All twelve existing flags work identically for PGN mode. That is the payoff of the
narrow one-method interface in §4.

### 11.3 CLI output

After indexing, print the report counters (§5.2) before building. A user whose tree came
out empty needs to distinguish "no game reached my FEN" from "my file was malformed" from
"everything was a duplicate".

```text
Indexed games.pgn: 1000 read, 812 reached the anchor, 41 did not,
188 malformed, 2 duplicates, 0 unfinished. 437 distinct positions.
```

---

## 12. Module layout

| File | Status | Responsibility |
|---|---|---|
| `sources.py` | **new** | `DataSource` Protocol, `LichessDataSource` |
| `pgn_index.py` | **new** | `PgnIndex.from_path()`, `PgnIndexReport`, streaming build |
| `explorer.py` | modified | take `source: DataSource`; add `min_root_share`; drop `_position_cache` |
| `cli.py` | modified | `--source`, `--pgn`, `--min-root-share`, per-source defaults, report output |
| `models.py` | docstrings only | document the `total` contract; **no new fields** |
| `api.py` | unchanged | `LichessClient` stays the transport |
| `pgn_export.py` | unchanged | `MoveNode` → PGN already works |
| `evaluation.py` | content unchanged | engine math already correct; **file is currently untracked and must be tracked (§15)** |
| `tests/test_pgn_index.py` | **new** | index unit tests |
| `tests/test_sources.py` | **new** | contract tests over both sources |
| `tests/test_explorer.py` | modified | integration; `min_root_share` tests |

Splitting `PgnIndex` from `PgnDataSource` keeps each file small and independently
testable: the index is about parsing and statistics, the source is about one lookup.

---

## 13. Data model

`models.py` gains **no new fields**. `PositionData` and `MoveStats` already express
exactly the right thing, provided the `total` contract in §4.1 is honoured. Only
docstrings change, to make the contract explicit and prevent a future source from
silently returning something else.

---

## 14. Testing strategy

PGN fixtures are **generated as strings inside the tests** via a small emit helper, not
shipped as binary files. Counts stay exact, diffs stay readable, and fixtures cannot rot.

### 14.1 Index tests (`test_pgn_index.py`)

| Test | Asserts |
|---|---|
| basic parsing | a simple PGN indexes to the expected moves |
| move frequency | counts per move are exact |
| relative frequency | share is against the position's own games |
| 1,000-game Nimzo | seeded distribution, exact counts at every position |
| **variable branch depth** | 600-game branch reaches the ceiling; 25-game branch stops early |
| **denominator** | move share uses games reaching the position, not file size |
| starting-FEN filtering | non-anchored games excluded from the root denominator |
| non-anchored games | counted in `games_without_anchor` |
| non-standard `[FEN]` | games replayed from their own root |
| transposition | two move orders merge counts; later one adds games |
| anchor at different move number | matched via `position_key` |
| duplicate games | identical sequences collapse, counted |
| near-duplicate games | shared prefix, divergent suffix: both kept |
| malformed PGN | bad game skipped, file continues |
| all-malformed PGN | empty index, no crash |
| empty PGN | empty index, no crash |
| variations present | RAV moves **not** counted |
| unfinished `*` games | excluded, counted |
| repetition in one game | anchored once, not double-counted |
| deep-anchor FEN | anchor matched late in the game |
| standard-start fast path | identical results to the slow path |

### 14.2 Contract tests (`test_sources.py`)

Run the same assertions against **both** `LichessDataSource` (with the existing fake
client) and `PgnDataSource`:

- `get_position` returns `PositionData` for a known position.
- Returns an empty `PositionData` for an unknown position.
- `PositionData.total` means "games reaching this position" — for both.
- `MoveStats.total` is never greater than `PositionData.total`.
- UCI strings are always parseable by `board.parse_uci`.

This is what stops the abstraction from rotting. A source that reports a different
meaning for `total` would make **every frequency silently wrong**; these tests are the
guard.

### 14.3 Builder tests (`test_explorer.py` additions)

- `min_root_share` rejects a tiny deep line when enabled.
- `min_root_share=0.0` is a no-op.
- `min_games` gate stops a thinly supported position.
- `max_depth` is never exceeded on any path.
- `max_moves` caps branches per position.
- Frequencies at the last recorded ply are **not** renormalised.

### 14.4 Integration and export

- PGN source → `list[MoveNode]` → `build_pgn` → **recursive SAN replay** using the
  existing `validate_pgn.py` approach. Every line legal.
- Transposed move orders both present in the exported PGN.
- Full CLI run on a generated PGN, asserting the report counters.

### 14.5 Stockfish

- `PgnDataSource` + real Stockfish: a genuinely played bad move is classified and
  punished.
- A legal move that **nobody played** is never generated as a deviation candidate —
  this is the realistic-vs-synthetic distinction, tested directly.

### 14.6 Named regression tests

Two behaviours are load-bearing enough to be pinned by name. If either ever breaks, the
test name says so directly in the failure output.

#### `test_move_frequency_uses_local_denominator_not_root_size`

The single most important statistical guarantee in the feature (§6.4).

Setup: 1,000 games reach the anchor. 200 of them continue with `...O-O`, and from that
position those 200 games play three moves 100 / 60 / 40 times.

| Assertion | Value |
|---|---|
| Root `PositionData.total` | 1000 |
| Branch `PositionData.total` | 200 |
| Branch move totals | 100, 60, 40 |
| **Correct frequencies** | **0.50, 0.30, 0.20** |
| Incorrect frequencies (root denominator) | 0.10, 0.06, 0.04 |

Asserts `100/200 == 0.5`, `60/200 == 0.3`, `40/200 == 0.2` through the real
`PgnDataSource` → `RepertoireBuilder` path, and additionally asserts that none of the
three equals the root-divided value. The negative assertion is what makes the test
meaningful: a test that only checks 0.5 would also pass if the code returned 0.5 by
accident.

#### `test_transposition_merges_stats_and_preserves_both_move_orders`

Setup: games reach one position by two distinct move orders, 30 games by the first and
20 by the second.

Asserts **both halves** of §6.5 together:

| Half | Assertion |
|---|---|
| Internal merge | `PositionData.total == 50`; each `MoveStats.total` is the sum across both orders |
| Output preservation | The exported PGN contains **two** distinct lines reaching that position, one per move order, and both are legal when replayed |

A single test asserting both, because merging correctly while collapsing the output — or
preserving the output while failing to merge — are each half-passing bugs that only this
combination catches.

---

## 15. Housekeeping (before implementation)

Three items identified during design. Cheap, and they should be fixed before new work is
layered on top.

1. **`evaluation.py` is untracked.** It is imported by `explorer.py` and is required for
   the package to work, but `git status` shows it as `??`. It must be tracked.
2. **README contradicts the code.** `README.md:84` says `BLUNDER: >= 3.0x the threshold`;
   `evaluation.py:28` sets `BLUNDER_RATIO = 2.5`. Fix the README to 2.5x.
3. **README example has wrong move numbers.** The anchor FEN is
   `... w KQkq - 4 5`, so White's first move is move **5**. The example shows `4. e3`,
   `4... O-O`, `5. Bxd8`; the generated PGN shows `5. e3`, `5... O-O`, `6. Bxd8`.
   Renumber throughout.

Optional, noted but not required: the README example's percentages were produced by a
mock run, not live Lichess data. Labelling it as illustrative sample output would be
more honest. Left to the owner's discretion.

The README will in any case be rewritten for §11's new flags, so items 2 and 3 fold into
that work naturally.

---

## 16. Implementation order

1. Housekeeping (§15): track `evaluation.py`, fix the two README errors.
2. `sources.py` — `DataSource` Protocol + `LichessDataSource`.
3. Rewire `RepertoireBuilder` to `DataSource`; delete the duplicate cache.
   **Full suite must stay green here** — this is a pure refactor.
4. `pgn_index.py` + `tests/test_pgn_index.py` (TDD).
5. `PgnDataSource` + `tests/test_sources.py` (TDD).
6. `min_root_share` in the builder + tests.
7. `cli.py` — `--source`, `--pgn`, per-source defaults, report output.
8. End-to-end: generated 1,000-game Nimzo PGN → CLI → PGN → recursive validation.
9. README: new flags, PGN source, per-source defaults, the shallower-mistake-detection
   caveat (§9.1).

Each step is independently testable. Step 3 is the risky one and is gated on the existing
94 tests staying green with no behavioural change.

---

## 17. Out of scope / future work

| Item | Why deferred |
|---|---|
| `StockfishAnalyzer` extraction from the 879-line `explorer.py` | Owner decision: keep as a separate refactor. Worth doing eventually — the file is large — but bundling it would confound this feature. |
| sqlite-backed index for very large PGNs | Not needed below ~100k games. Interface already permits it (§10.4). |
| Include `*` games behind a flag | No bucket exists for "played, unknown result" (§7.4). |
| Custom / cheaper position key | Profiling has not shown it necessary (§10.2). |
| Win-rate-weighted move selection | The model already carries the data (§6.3); no consumer yet. |
| Caching parsed indexes to disk | Re-indexing a 10k-game PGN takes seconds. |

---

## 18. Risks

| Risk | Mitigation |
|---|---|
| Data source reports a different `total` meaning | Contract tests over both sources (§14.2) |
| `min_games` default wrong for one source | Per-source defaults (§11.1) |
| Index shallower than builder's `max_depth` | `PgnDataSource.check_depth()` raises; CLI validates (§11) |
| Empty tree with no explanation | Report counters printed before building (§11.3) |
| Shallow mistake detection read as a regression | Documented in README (§9.1) |
| Index window too narrow after a future depth increase | `check_depth()` guard; index is cheap to rebuild |
| Repertoire polluted by unplayed lines | Mainline-only (§7.5) plus `min_root_share` (§8.2) |
