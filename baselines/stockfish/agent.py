"""Development-only opponent: a UCI bridge to a locally installed Stockfish.

THIS IS NOT PART OF THE SUBMISSION AND MUST NEVER BE SHIPPED. Third party engines inside the zip
are an instant disqualification. It lives under baselines/ so harness.package cannot pick it up:
that packager takes root-level *.py plus the weights directory, and nothing from here.

Its only job is to give the arena a calibrated opponent to measure against. Point it at the
binary and pick a strength with environment variables:

    STOCKFISH_PATH   path to the executable
    STOCKFISH_DEPTH  fixed search depth per move, default 1
    STOCKFISH_ELO    optional UCI_LimitStrength target, 1320 upwards

Depth is the more repeatable knob of the two, so the default limits depth and leaves the built in
strength limiter alone.
"""

from __future__ import annotations

import os
import subprocess
from typing import IO

_DEFAULT_PATH = (
    r"C:\Users\Adarsh Thakur\Downloads\stockfish-windows-x86-64-avx2"
    r"\stockfish\stockfish-windows-x86-64-avx2.exe"
)


class Engine:
    def __init__(self) -> None:
        path = os.environ.get("STOCKFISH_PATH", _DEFAULT_PATH)
        self.depth = int(os.environ.get("STOCKFISH_DEPTH", "1"))
        self.process = subprocess.Popen(
            [path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._send("uci")
        self._wait_for("uciok")
        # One core, to match what the platform would give a real opponent.
        self._send("setoption name Threads value 1")
        self._send("setoption name Hash value 16")
        elo = os.environ.get("STOCKFISH_ELO")
        if elo:
            self._send("setoption name UCI_LimitStrength value true")
            self._send(f"setoption name UCI_Elo value {int(elo)}")
        self._send("isready")
        self._wait_for("readyok")

    def _pipe(self, stream: IO[str] | None) -> IO[str]:
        if stream is None:
            raise RuntimeError("stockfish pipe is unavailable")
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
        raise RuntimeError(f"stockfish exited before returning {expected}")

    def move(self, fen: str) -> str:
        self._send(f"position fen {fen}")
        self._send(f"go depth {self.depth}")
        return self._wait_for("bestmove").split()[1]


_engine: Engine | None = None


def get_move(fen: str, time_left_ms: int) -> str:
    global _engine
    if _engine is None:
        _engine = Engine()
    return _engine.move(fen)
