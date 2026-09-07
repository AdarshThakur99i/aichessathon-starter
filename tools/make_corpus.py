"""Build a corpus of realistic games with a local engine, for weight tuning.

Development tooling only. Nothing here ships, and the engine it drives never enters the zip.

Weight tuning needs positions that look like the ones the agent will actually face. The sampler
this replaces played 8 to 45 uniformly random moves, which produces positions with material
hanging everywhere: weights fitted there scored -108 Elo against the untuned ones, because
"piece in the enemy half" correlates with "about to be captured" in random play and the fit
learns that artifact.

Each game opens with a few random plies for variety, then both sides play a fixed depth, so the
middlegames and endgames are ones a real player could reach.
"""

from __future__ import annotations

import argparse
import random
import subprocess
from pathlib import Path
from typing import IO

import chess
import chess.pgn


class Engine:
    def __init__(self, path: Path, depth: int) -> None:
        self.depth = depth
        self.process = subprocess.Popen(
            [str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._send("uci")
        self._wait_for("uciok")
        self._send("setoption name Threads value 1")
        self._send("isready")
        self._wait_for("readyok")

    def _pipe(self, stream: IO[str] | None) -> IO[str]:
        if stream is None:
            raise RuntimeError("engine pipe is unavailable")
        return stream

    def _send(self, command: str) -> None:
        pipe = self._pipe(self.process.stdin)
        pipe.write(command + "\n")
        pipe.flush()

    def _wait_for(self, expected: str) -> str:
        for line in self._pipe(self.process.stdout):
            line = line.strip()
            if line == expected or line.startswith(expected + " "):
                return line
        raise RuntimeError(f"engine exited before returning {expected}")

    def best_move(self, board: chess.Board) -> chess.Move | None:
        self._send(f"position fen {board.fen()}")
        self._send(f"go depth {self.depth}")
        token = self._wait_for("bestmove").split()[1]
        if token in ("(none)", "0000"):
            return None
        return chess.Move.from_uci(token)

    def close(self) -> None:
        if self.process.poll() is None:
            self._send("quit")
            self.process.wait(timeout=5)


def play_game(
    engine: Engine, rng: random.Random, random_plies: int, ply_cap: int
) -> chess.pgn.Game:
    board = chess.Board()
    for _ in range(random_plies):
        moves = list(board.legal_moves)
        if not moves:
            break
        board.push(rng.choice(moves))
    while not board.is_game_over(claim_draw=True) and len(board.move_stack) < ply_cap:
        move = engine.best_move(board)
        if move is None or move not in board.legal_moves:
            break
        board.push(move)
    game = chess.pgn.Game.from_board(board)
    game.headers["Event"] = "tuning corpus"
    return game


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--games", type=int, default=60)
    parser.add_argument("--depth", type=int, default=6)
    parser.add_argument("--random-plies", type=int, default=6)
    parser.add_argument("--ply-cap", type=int, default=200)
    parser.add_argument("--seed", type=int, default=11)
    arguments = parser.parse_args()

    rng = random.Random(arguments.seed)
    engine = Engine(arguments.engine, arguments.depth)
    written = 0
    try:
        with arguments.out.open("w", encoding="utf-8") as handle:
            for index in range(arguments.games):
                game = play_game(engine, rng, arguments.random_plies, arguments.ply_cap)
                handle.write(str(game) + "\n\n")
                written += 1
                if (index + 1) % 10 == 0:
                    print(f"  {index + 1}/{arguments.games} games", flush=True)
    finally:
        engine.close()
    print(f"wrote {written} games to {arguments.out}")


if __name__ == "__main__":
    main()
