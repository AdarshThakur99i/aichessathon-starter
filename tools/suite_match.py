"""Measure strength against a reference engine reliably.

Development tooling only. Nothing here ships.

Two things make a wall-clock match at a fast time control almost useless for comparing builds.
The agent is limited by the clock while a fixed-depth reference is not, so any change in machine
load moves the agent's search depth and nothing else; at a ten second control the search sits at
three or four ply, right where losing half a ply flips whole games. Measured on one machine with
one build and one book, three runs of twenty games scored 60%, 45% and 20%.

So this fixes the depth instead of the clock, which removes machine speed from the result. That
alone would make every game identical, because two deterministic engines from one position play
one game, so diversity comes from starting each pair from a different master position instead.
Each position is played twice with colours swapped, which also cancels any first-move advantage
baked into the opening.

    python -m tools.suite_match --build-suite scratch/twic --suite suite.json
    python -m tools.suite_match --suite suite.json --agent . --depth 4 --engine-depth 3
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

import chess
import chess.pgn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.referee import play_match
from harness.sandbox import local

# Far enough in that the players have committed to a structure, not so far that the game is
# decided. Material has to be level or the result says more about the position than the engine.
SUITE_PLY = 16
SUITE_MATERIAL_TOLERANCE = 0
PIECE_VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}


def _material(board: chess.Board, color: chess.Color) -> int:
    return sum(value * len(board.pieces(piece, color)) for piece, value in PIECE_VALUE.items())


def build_suite(arguments: argparse.Namespace) -> None:
    """Pull level positions out of master games, spread across as many openings as possible."""
    paths = sorted(
        path
        for source in arguments.build_suite
        for path in ([source] if source.is_file() else source.rglob("*.pgn"))
    )
    seen: set[str] = set()
    positions: list[str] = []
    for path in paths:
        if len(positions) >= arguments.count:
            break
        with path.open(encoding="utf-8", errors="replace") as handle:
            while len(positions) < arguments.count:
                game = chess.pgn.read_game(handle)
                if game is None:
                    break
                board = game.board()
                for ply, move in enumerate(game.mainline_moves()):
                    if ply >= SUITE_PLY:
                        break
                    board.push(move)
                if len(board.move_stack) < SUITE_PLY or board.is_check():
                    continue
                edge = _material(board, chess.WHITE) - _material(board, chess.BLACK)
                if abs(edge) > SUITE_MATERIAL_TOLERANCE:
                    continue
                # Key on the first four plies so the suite spreads over openings rather than
                # filling up with forty Najdorfs from one tournament.
                opening = " ".join(m.uci() for m in board.move_stack[:4])
                if opening in seen:
                    continue
                seen.add(opening)
                positions.append(board.fen())
    random.Random(0).shuffle(positions)
    arguments.suite.write_text(json.dumps(positions, indent=1))
    print(f"wrote {arguments.suite} with {len(positions)} level positions at ply {SUITE_PLY}")


def run(arguments: argparse.Namespace) -> None:
    positions = json.loads(arguments.suite.read_text())[: arguments.positions]
    os.environ["STOCKFISH_DEPTH"] = str(arguments.engine_depth)
    os.environ["CHESSATHON_MAX_DEPTH"] = str(arguments.depth)
    agent = arguments.agent.resolve()
    opponent = arguments.opponent.resolve()

    wins = draws = losses = 0
    terminations: dict[str, int] = {}
    for index, fen in enumerate(positions):
        for plays_white in (True, False):
            white, black = (agent, opponent) if plays_white else (opponent, agent)
            outcome = play_match(
                local(white), local(black), arguments.base_ms, arguments.increment_ms, start_fen=fen
            )
            terminations[outcome.termination] = terminations.get(outcome.termination, 0) + 1
            if outcome.result in ("draw", "void"):
                draws += 1
            elif (outcome.result == "white") == plays_white:
                wins += 1
            else:
                losses += 1
        played = (index + 1) * 2
        print(
            f"  {played:3d} games: +{wins} ={draws} -{losses} "
            f"({(wins + draws / 2) / played:.1%})",
            flush=True,
        )

    total = wins + draws + losses
    score = (wins + draws / 2) / total
    # Binomial standard error, which is the floor on the noise, not the whole of it.
    error = (0.25 / total) ** 0.5
    print(f"\nagent depth {arguments.depth} vs {arguments.opponent} depth {arguments.engine_depth}")
    print(f"+{wins} ={draws} -{losses} over {total} games")
    print(f"score {score:.1%} +- {error:.1%}")
    print("terminations: " + ", ".join(f"{k} {v}" for k, v in sorted(terminations.items())))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-suite", type=Path, action="append")
    parser.add_argument("--suite", type=Path, default=Path("suite.json"))
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--positions", type=int, default=40)
    parser.add_argument("--agent", type=Path, default=Path("."))
    parser.add_argument("--opponent", type=Path, default=Path("baselines/stockfish"))
    parser.add_argument("--depth", type=int, default=4)
    parser.add_argument("--engine-depth", type=int, default=3)
    # Generous, because the point is for the depth cap to decide when the search stops, not the
    # clock. If the clock ever binds, machine speed is back in the measurement.
    parser.add_argument("--base-ms", type=int, default=300_000)
    parser.add_argument("--increment-ms", type=int, default=1_000)
    arguments = parser.parse_args()
    if arguments.build_suite:
        build_suite(arguments)
    else:
        run(arguments)


if __name__ == "__main__":
    main()
