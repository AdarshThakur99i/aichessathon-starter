"""Collect every game the engine has played into one PGN corpus, for weight tuning.

Development tooling only. Nothing here ships.

    python -m tools.harvest                     # gather the usual places into games/corpus.pgn
    python -m tools.harvest --pgn extra.pgn     # and some more
    python -m tools.harvest --min-plies 30      # only games with a middlegame in them

## The agent cannot learn while it plays

Worth being blunt about, because it shapes everything here. The platform starts a fresh process
for each game, keeps no module state between games, mounts the filesystem read-only apart from a
scratch directory that does not outlive the container, and allows no network. There is nowhere for
a lesson learned in one game to be written down, so nothing the agent notices can reach the next
game. Anything that calls itself online learning inside `agent.py` would be a no-op at best.

Improvement therefore happens offline, between rounds, and the loop is:

    make harvest                                # every game played, into games/corpus.pgn
    uv run python -m tools.tune_weights \\
        --stockfish <path> --pgn games/corpus.pgn \\
        --positions 20000 --ridge 1.0 --anchor-material
    # paste the printed numbers over FEATURE_WEIGHTS in agent.py
    make arena                                  # confirm they actually score better
    make zip                                    # and upload

Tuning against a local Stockfish is allowed: the rules ban shipping or running another engine
inside the zip, and permit training on data an engine annotated. What gets uploaded stays a
`FEATURE_WEIGHTS` tuple.

## Where the games come from

    games/played.pgn    every game finished in the local web interface, written by tools.serve
    *.pgn at the root   round PGNs downloaded from the dashboard, and `make play --pgn` output
    the game store      the web interface's Upstash storage, when KV_REST_API_* are set

Games are deduplicated on their starting position and moves, so the sources may overlap and this
can be re-run as often as you like. Corpus quality matters more than corpus size: see the note at
the top of tools/make_corpus.py about weights fitted on unrealistic positions scoring -108 Elo.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

MIRROR = ROOT / "games" / "played.pgn"
CORPUS = ROOT / "games" / "corpus.pgn"


def read_games(text: str) -> list[chess.pgn.Game]:
    """Every game in one PGN document. Unparseable tails are dropped rather than raised."""
    games: list[chess.pgn.Game] = []
    handle = io.StringIO(text)
    while True:
        try:
            game = chess.pgn.read_game(handle)
        except (ValueError, RuntimeError):
            break
        if game is None:
            break
        games.append(game)
    return games


def fingerprint(game: chess.pgn.Game) -> tuple[str, ...]:
    """A game is where it started plus what was played. Headers are deliberately ignored, so the
    same game recorded by the web interface and by a round download counts once."""
    start = game.headers.get("FEN", chess.STARTING_FEN)
    return (start, *(move.uci() for move in game.mainline_moves()))


def plies(game: chess.pgn.Game) -> int:
    return sum(1 for _ in game.mainline_moves())


def from_store() -> list[chess.pgn.Game]:
    """Games the deployed web interface recorded, if Upstash is configured for this shell."""
    try:
        import _store
    except ImportError as failure:
        print(f"  game store: unavailable ({failure})")
        return []
    if not _store.is_persistent():
        print("  game store: KV_REST_API_URL is not set, skipping")
        return []
    games: list[chess.pgn.Game] = []
    try:
        for player in _store.players():
            for record in _store.games_for(str(player.get("name") or "")):
                games.extend(read_games(str(record.get("pgn") or "")))
    except (OSError, ValueError, RuntimeError) as failure:
        print(f"  game store: read failed ({failure})")
        return games
    print(f"  game store: {len(games)} game(s)")
    return games


def sources(extra: list[Path]) -> list[Path]:
    """The PGN files to read, in a stable order, without repeats."""
    found: list[Path] = []
    for path in [MIRROR, *sorted(ROOT.glob("*.pgn")), *extra]:
        resolved = path.resolve()
        if resolved.is_file() and resolved != CORPUS.resolve() and resolved not in found:
            found.append(resolved)
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pgn", type=Path, action="append", default=[], help="another PGN file to fold in"
    )
    parser.add_argument("--out", type=Path, default=CORPUS)
    parser.add_argument(
        "--min-plies",
        type=int,
        default=16,
        help="drop games shorter than this. tune_weights skips the first 12 plies of every game, "
        "so anything shorter contributes no positions at all",
    )
    parser.add_argument(
        "--no-store", action="store_true", help="do not read the web interface's storage"
    )
    arguments = parser.parse_args()

    print("reading")
    collected: list[chess.pgn.Game] = []
    for path in sources(arguments.pgn):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as failure:
            print(f"  {path.name}: unreadable ({failure})")
            continue
        found = read_games(text)
        collected.extend(found)
        print(f"  {path.relative_to(ROOT) if ROOT in path.parents else path}: {len(found)} game(s)")
    if not arguments.no_store:
        collected.extend(from_store())

    unique: dict[tuple[str, ...], chess.pgn.Game] = {}
    duplicates = 0
    tooshort = 0
    for game in collected:
        if plies(game) < arguments.min_plies:
            tooshort += 1
            continue
        key = fingerprint(game)
        if key in unique:
            duplicates += 1
            continue
        unique[key] = game

    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    with arguments.out.open("w", encoding="utf-8") as handle:
        exporter = chess.pgn.FileExporter(handle)
        for game in unique.values():
            game.accept(exporter)

    total_plies = sum(plies(game) for game in unique.values())
    usable = sum(max(0, plies(game) - 12) for game in unique.values())
    print(
        f"\n{len(collected)} game(s) read, {duplicates} duplicate(s) and {tooshort} too short "
        f"dropped\n{len(unique)} game(s) written to {arguments.out.relative_to(ROOT)}, "
        f"{total_plies} plies, about {usable} sampleable positions"
    )
    if not unique:
        print(
            "\nNothing to tune on yet. Play some games in the web interface (make serve) or run "
            "make play with --pgn, then try again."
        )


if __name__ == "__main__":
    main()
