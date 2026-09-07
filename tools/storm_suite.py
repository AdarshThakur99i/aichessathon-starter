"""Does the agent find a wing attack when a wing attack is objectively the best move?

Development tooling only. Nothing here ships.

The suite is built rather than curated. Hand-picking games where a kingside attack won would bias
the test towards attacks that happened to work, so instead every candidate position is put to a
strong reference search, and only positions where the reference itself answers with a flank pawn
push are kept. That makes "the storm is correct here" ground truth rather than an opinion, and it
scales to as many positions as the corpus holds.

Build a suite, then score any agent directory against it:

    python -m tools.storm_suite build --engine SF --pgn corpus.pgn --out suite.json
    python -m tools.storm_suite score --suite suite.json --agent .
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import IO

import chess
import chess.pgn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent as reference_agent


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

    def best_move(self, fen: str) -> str:
        self._send(f"position fen {fen}")
        self._send(f"go depth {self.depth}")
        return self._wait_for("bestmove").split()[1]

    def close(self) -> None:
        if self.process.poll() is None:
            self._send("quit")
            self.process.wait(timeout=5)


def candidates(pgn_paths: list[Path], skip_opening: int) -> list[str]:
    """Positions where the opponent's king has committed to a wing, so a storm is even possible."""
    found: list[str] = []
    for path in pgn_paths:
        with path.open(encoding="utf-8", errors="replace") as handle:
            while True:
                game = chess.pgn.read_game(handle)
                if game is None:
                    break
                board = game.board()
                for ply, move in enumerate(game.mainline_moves()):
                    board.push(move)
                    if ply < skip_opening or board.is_check():
                        continue
                    if board.is_game_over(claim_draw=False):
                        continue
                    if reference_agent._wing_files(board, not board.turn):
                        found.append(board.fen())
    return found


def build(arguments: argparse.Namespace) -> None:
    fens = candidates(arguments.pgn, arguments.skip_opening)
    print(f"{len(fens)} positions have a committed enemy king")
    engine = Engine(arguments.engine, arguments.depth)
    suite: list[dict[str, str]] = []
    try:
        for index, fen in enumerate(fens):
            if len(suite) >= arguments.limit:
                break
            board = chess.Board(fen)
            best = engine.best_move(fen)
            move = chess.Move.from_uci(best)
            files = reference_agent._wing_files(board, not board.turn)
            if reference_agent._is_storm_move(board, move, files):
                suite.append({"fen": fen, "best": best})
            if (index + 1) % 200 == 0:
                print(f"  probed {index + 1}, kept {len(suite)}", flush=True)
    finally:
        engine.close()
    arguments.out.write_text(json.dumps(suite, indent=1))
    print(f"kept {len(suite)} positions where the reference itself plays a flank push")
    print(f"wrote {arguments.out}")


def load_agent(directory: Path) -> object:
    spec = importlib.util.spec_from_file_location(
        f"suite_agent_{abs(hash(str(directory)))}", directory / "agent.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"no agent.py in {directory}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def score(arguments: argparse.Namespace) -> None:
    suite = json.loads(arguments.suite.read_text())
    module = load_agent(arguments.agent)
    exact = storm = 0
    for case in suite:
        board = chess.Board(case["fen"])
        played = module.get_move(case["fen"], arguments.clock_ms)  # type: ignore[attr-defined]
        move = chess.Move.from_uci(played)
        files = reference_agent._wing_files(board, not board.turn)
        if played == case["best"]:
            exact += 1
        if reference_agent._is_storm_move(board, move, files):
            storm += 1
    total = len(suite)
    print(f"{arguments.agent} over {total} positions where a flank push is the reference answer")
    print(f"  played the exact reference move: {exact:4d} / {total}  ({exact / total:.1%})")
    print(f"  played some flank push:          {storm:4d} / {total}  ({storm / total:.1%})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    builder = sub.add_parser("build")
    builder.add_argument("--engine", type=Path, required=True)
    builder.add_argument("--pgn", type=Path, action="append", required=True)
    builder.add_argument("--out", type=Path, required=True)
    builder.add_argument("--depth", type=int, default=14)
    builder.add_argument("--limit", type=int, default=200)
    builder.add_argument("--skip-opening", type=int, default=12)
    builder.set_defaults(handler=build)

    scorer = sub.add_parser("score")
    scorer.add_argument("--suite", type=Path, required=True)
    scorer.add_argument("--agent", type=Path, default=Path("."))
    scorer.add_argument("--clock-ms", type=int, default=10_000)
    scorer.set_defaults(handler=score)

    arguments = parser.parse_args()
    arguments.handler(arguments)


if __name__ == "__main__":
    main()
