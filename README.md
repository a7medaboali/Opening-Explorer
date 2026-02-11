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
| `--fen` | standard starting position | Starting FEN position |
| `-n`, `--min-games` | 50 | Min games in a position before stopping exploration |
| `--threshold` | 0.8 | Cumulative frequency cutoff for "our" moves |
| `-o`, `--output` | `repertoire.pgn` | Output PGN file path |

The repertoire color is inferred from the side to move in the starting FEN.

## Algorithm

- **Our moves**: includes moves by descending frequency until cumulative frequency >= threshold
- **Opponent moves**: always includes the most popular move; also includes any top-4 move with a higher performance score (white win% + 50% * draw%)
- **Termination**: stops exploring when a position has fewer than `min-games` total games

## Example

```bash
uv run lichess-opening-explorer \
    --fen "rnb1kb1r/ppp1pppp/3q1n2/8/3P4/2N2N2/PPP2PPP/R1BQKB1R b KQkq - 2 5" \
    -n 500 -o repertoire.pgn
```

Output (`repertoire.pgn`):

```pgn
[Event "Opening Repertoire"]
[Site "?"]
[Date "????.??.??"]
[Round "?"]
[White "?"]
[Black "?"]
[Result "*"]
[FEN "rnb1kb1r/ppp1pppp/3q1n2/8/3P4/2N2N2/PPP2PPP/R1BQKB1R b KQkq - 2 5"]
[SetUp "1"]

5... a6 ( 5... c6 6. Ne5 ( 6. g3 Bg4 ( 6... Bf5 ) ) ( 6. h3 Bf5 ( 6... g6 ) )
( 6. Be3 Bf5 ( 6... Bg4 ) ) 6... Nbd7 7. Nc4 ( 7. f4 Nb6 ( 7... g6 ) ) 7... Qc7 )
( 5... g6 6. Nb5 Qb6 ( 6... Qd8 ) ) 6. g3 Bg4 ( 6... b5 ) *
```

Import this PGN into Lichess, ChessBase, or any PGN-compatible tool to browse the repertoire.
