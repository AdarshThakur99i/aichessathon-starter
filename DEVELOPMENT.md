# This fork

The agent is **drunkenmaster**. `agent.py` is the whole submission — one 61 KB file, no data, no
dependencies beyond `python-chess`, which the platform preinstalls.

There is also a web interface under `public/` and `api/` for playing the same engine in a browser.

## What is in the engine

Alpha-beta with principal variation search and iterative deepening, a transposition table keyed on
`board._transposition_key()`, quiescence search with delta pruning, MVV-LVA capture ordering with
killers and history, late move reductions, and a tapered piece-square-table evaluation on top of
ten hand-weighted features.

Everything else worth knowing is in comments next to the code it explains, including the reasons
for several things that look wrong but are deliberate — `NEXT_ITERATION_SHARE` is the clearest
example, where the obviously more principled version measured worse.

### Rules compliance

Verified against <https://aichessathon.com/docs> on a real Python 3.12.14 interpreter with only
`chess==1.11.2` installed, running the extracted zip from an unrelated working directory:

| | requirement | measured |
| --- | --- | --- |
| size | 50 MB unzipped | 61,391 bytes |
| Python | 3.12 | tested on 3.12.14 |
| python-chess | 1.11.2 | 1.11.2 |
| memory | 2 GB | 30 MB peak |
| network | none | no networking imports at all |
| filesystem | read-only | no writes |
| stdout | 8 KB | silent unless `CHESSATHON_DEBUG=1` |
| init budget | 90s | 0.49s import |
| clock | 120s + 0.5s | never exceeds the remaining time |
| ply cap | 600 | survives 600 plies with 3.5s spare |

`harness/rules.py` in the upstream starter disagreed with the published rules on four values (ply
cap 300, init budget 60s, stdout 4096). They are corrected here, because the 300-ply cap was
adjudicating local test games at half the real length.

## Stockfish is a test instrument here, not a component

`baselines/stockfish/agent.py` is a UCI bridge used **only** to grade our own moves during
development, and `tools/move_quality.py` uses it as a neutral referee for centipawn loss.

**Neither ships.** `harness/package.py` collects Python files at the repository root plus a
`weights` directory, so nothing under `baselines/`, `tools/`, `api/` or `public/` can enter the
zip. `make zip` prints exactly what went in; it is `agent.py` and nothing else. The competition
rules prohibit *shipping* a third-party engine, and permit using one to analyse your own games.

## Tools

All development-only. Nothing here ships.

```
python -m tools.move_quality --pgn game.pgn --depth 16     # centipawn loss vs a reference
python -m tools.endgame_suite --depth 32 --positions 4     # can it finish a won ending?
python -m tools.build_book --pgn corpus/ --out weights/book.bin
python -m tools.tune_weights ...                           # least squares fit of the features
```

`tools/build_book.py` builds a Polyglot opening book from master-game PGNs. It is kept because it
works, but the book was **removed from the submission**: rated games start from a preset position
at move 10, so a book keyed on lines from the initial position scored zero hits in a real game.

### Measuring anything

Games are far noisier than they look. To be 95% sure a change is real:

| change | games needed |
| --- | --- |
| +20 Elo | 773 |
| +40 Elo | 193 |
| +80 Elo | 48 |

A 20-game match can only detect changes bigger than ~124 Elo, and a 3-game match ~321 Elo. Several
conclusions were reached and then overturned in this repository's history for exactly this reason,
so prefer `tools/move_quality.py` over short matches: it compares builds on fixed positions with no
game-outcome noise, which gives comparable resolution for a fraction of the wall time.

## The web interface

Plays the same `agent.py`. Deploy target is Vercel.

```
public/index.html, app.js, style.css   the page: board, clocks, player folders
api/move.py                            POST {start_fen, moves, time_left_ms} -> a move
api/games.py                           GET/POST player folders
api/_store.py                          Upstash Redis over REST, with an in-memory fallback
```

Two things it has to get right. The engine keeps state in module globals and a warm serverless
container serves many requests, so every request resets the engine and replays that game's own
positions into the repetition history — otherwise one player's game poisons the next, and the
engine cannot see repetitions at all, since `get_move` receives a position rather than a history.
And the engine spends about a sixteenth of whatever clock it is told about, so the clock is capped
at `MAX_CLOCK_MS` to keep a move inside the function timeout. That costs strength; it is the price
of running in a request.

### Deploying

Import the repository on Vercel. No build step; `public/` is served statically and `api/*.py`
become Python functions. For player folders to survive a restart, add an Upstash Redis integration
and set either pair:

```
KV_REST_API_URL / KV_REST_API_TOKEN
UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN
```

Without them the site still runs and games are kept in process, which is fine locally and useless
in production. The page says so when storage is unconfigured.

## Known weaknesses

Measured, not guessed.

- **Ordinary-move accuracy.** Against a depth-16 reference over a real rated game, mean centipawn
  loss excluding blunders is ~17 where leaderboard agents are ~4-6.
- **Blunder rate.** Roughly 7% of moves lose more than 200 centipawns, against ~1-4% for the
  agents at the top. Every case traced so far was a position the engine gets right at depth 5 and
  wrong at depth 4, so this is search depth rather than evaluation.
- **KBB v K and KBN v K are not converted.** `_mating_drive` drives the losing king to the edge
  but has no notion of which corner, which those two mates require. KQ v K and KR v K are fine.
- **Time is front-loaded.** `MIN_MOVES_TO_GO` spends a sixteenth of the remaining clock every move,
  which is safe but leaves little for long endgames.
