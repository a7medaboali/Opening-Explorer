# Lichess Opening Explorer

CLI tool that queries the [Lichess Masters database](https://lichess.org/api#tag/Opening-Explorer) to generate a chess opening repertoire as a PGN file with variations.

## Install

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Usage

```
uv run lichess-opening-explorer [OPTIONS]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--source` | `lichess` | `lichess` or `pgn` — where opening statistics come from |
| `--pgn` | — | Path to a local PGN file (required with `--source pgn`) |
| `--fen` | standard starting position | Starting FEN position |
| `-n`, `--min-games` | 50 for `lichess`, 10 for `pgn` | Min games in a position before stopping exploration |
| `--threshold` | 0.8 | Cumulative frequency cutoff for "our" moves |
| `--min-move-frequency` | 0.05 | Min share of a position for a single repertoire move |
| `--min-opponent-frequency` | 0.001 | Min share for an opponent deviation to be analysed |
| `--min-root-share` | 0 (disabled) | Optional floor on a move's share of the games that reached the starting position |
| `--max-depth` | 10 | Maximum repertoire depth in plies |
| `--max-moves` | 3 | Maximum repertoire moves per position |
| `--max-mistakes` | 2 | Maximum opponent mistakes per position |
| `--max-deviations` | 6 | Maximum opponent moves analysed per position |
| `--mistake-threshold` | 12.0 | Win percentage the opponent must throw away |
| `--punishment-depth` | 6 | Plies kept in a punishment line |
| `--engine-depth` | 14 | Fixed Stockfish search depth |
| `--stockfish` | auto | Path to the Stockfish executable |
| `-o`, `--output` | `repertoire.pgn` | Output PGN file path |

The repertoire color is inferred from the side to move in the starting FEN.

## Local PGN database

`--source pgn` builds the repertoire from your own games. No token and no
network access are involved, which makes it useful for analysing a study
file, a tournament export, or a set of games you want to check yourself
against.

```bash
uv run lichess-opening-explorer --source pgn --pgn games.pgn \
    --fen "rnbqk2r/ppp2ppp/4pn2/3p2B1/1bPP4/2N5/PP2PPPP/R2QKBNR w KQkq - 4 5" \
    -o nimzo.pgn
```

```text
PGN report: 1000 read, 1000 reached the anchor, 0 did not, 0 malformed,
0 duplicates, 0 unfinished, 914 distinct positions.
```

- **The anchor has to be a position your games actually reach.** Games that
  never pass through `--fen` are counted as "did not", and a run where
  nothing reached the anchor also says so on stderr, because a silently
  empty repertoire is otherwise indistinguishable from "no opening found".
- **Frequencies are always relative to the position being expanded**, never
  to the size of the file. A move played in 6 of the 30 games that reached a
  position is 20% there, however many games the file holds.
- **`--min-games` is the local gate.** Branch depth follows game support, so
  a well-supported line goes deeper than a thin one.
- **Side variations are ignored.** Only each game's main line is counted.
  Unfinished (`Result: "*"`) games are excluded and reported, as are
  duplicates: games with an identical starting position and identical
  main line are one game, not several.
- **Transpositions merge internally, and every move order is kept in the
  output.** A position reached by two different move orders is one position
  with combined statistics, but a human still has to play one move per turn,
  so both orders are written as separate lines.
- **`--min-root-share` is for lines that are locally real but globally
  meaningless** — three games at depth eight out of a thousand. It is
  disabled at 0 and is deliberately not applied to opponent deviations, where
  a rare move is the interesting case.
- **Mistakes are found more shallowly than with Lichess Masters.** A
  thousand-game database has few games in deep positions, so there are fewer
  real deviations to analyse. That is a property of the data, not a fault.

## Setup

Stockfish is required. The tool looks for it in this order:

1. the `--stockfish` path,
2. the `LICHESS_STOCKFISH` environment variable,
3. the `STOCKFISH_PATH` environment variable,
4. `stockfish` on your `PATH`.

```bash
export LICHESS_STOCKFISH=/path/to/stockfish
```

The Lichess Masters explorer requires an OAuth token, which the tool reads
from the `LICHESS_TOKEN` environment variable. Without it every request
returns `401`, and the tool says so instead of failing silently.

## Algorithm

- **Main repertoire moves**: at every position, the moves the Lichess Masters
  database actually records, in games-played order, until the cumulative
  `--threshold` share is covered. A single move below
  `--min-move-frequency` is skipped.
- **Position cutoff**: exploration of a position stops below `--min-games`.
  This is a per-position cutoff rather than a per-move one, so a late, thinly
  played position still yields its main move instead of dying mid-tree.
- **Move legality**: every candidate UCI is resolved against the board before
  use, and a row that does not resolve is discarded rather than allowed to
  suppress the valid moves behind it. SAN is generated from the board, so it
  always matches the move and can be replayed.
- **Transpositions**: positions are cached by a four-field FEN key that keeps
  a legally usable en-passant square, so two move orders reaching the same
  position share a subtree.
- **Deviations**: only moves the database records for the position are
  considered, so an opponent's mistake is something they will really play
  rather than a capture nobody attempts. `--min-opponent-frequency` drops
  one- and two-game curiosities in very large positions without suppressing
  genuine blunders, which are often rare.
- **Mistake detection**: the baseline is the best of the realistic candidates
  *and* the engine's own first choice, each evaluated through the same path.
  A move that matches or beats that baseline is never reported as a mistake.
  Severity is measured in win percentage points, not raw centipawns, so a
  change that does not move the result is not treated as a blunder.
- **Mistake classification** (win percentage given away):
  - `MISTAKE`: >= `--mistake-threshold`
  - `SERIOUS MISTAKE`: >= 1.5x the threshold
  - `BLUNDER`: >= 2.5x the threshold
- **Punishment**: for each detected mistake, the Stockfish principal variation
  from the position after the mistake becomes the punishment line.
- **Determinism**: Stockfish runs at a fixed depth rather than a time limit,
  so the analysis does not depend on machine speed or load. A fixed depth
  alone is not the same as byte-identical output: within one run the engine
  reuses its hash table, so a position re-analysed later can score a few
  centipawns differently. The chosen move is stable; the percentages printed
  in comments should be read as approximate. The error this introduces is
  far below `--mistake-threshold`, so classifications are not affected.
- **Termination**: stops at `--max-depth`, when a position is reached by fewer
  than `--min-games` games, or when no qualifying moves remain.
- **Errors**: a 5xx, a dropped connection, a rate limit, or an HTML error page
  is retried; a 401, a 403, or a 4xx is not, because repeating a request the
  server has already rejected only delays the message.

## Example

```bash
uv run lichess-opening-explorer \
    --fen "rnbqk2r/ppp2ppp/4pn2/3p2B1/1bPP4/2N5/PP2PPPP/R2QKBNR w KQkq - 4 5" \
    -n 500 -o repertoire.pgn
```

The tree follows the database's preferred line at every ply and, on the
opponent's turn, adds the mistakes they realistically commit together with the
punishment you have against them:

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

Import this PGN into Lichess, ChessBase, or any PGN-compatible tool to browse
the repertoire.
