"""Ask the engine for a move. Serverless entry point for the web interface.

Not part of the submission: the packager only collects Python files at the repository root, so
nothing under api/ can reach the zip. This imports the same agent.py that gets submitted, so the
site plays the competition engine rather than a copy that can drift away from it.

Two things need care in a serverless setting.

The engine keeps state in module globals, and a warm container serves many requests, possibly from
different players. So every request resets the engine and then replays the game's own positions
into the repetition history. Without the replay the engine cannot see a repetition, because
get_move is handed a position and not a history; without the reset one player's game would poison
the next.

The platform gives the engine two minutes a side, and a serverless function does not get two
minutes. The clock handed to the engine is therefore capped so its own budget calculation lands
under the function's limit, which costs playing strength and is the price of running in a request.
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent

# Keep the engine's own budget under the function timeout. It spends about a sixteenth of the
# clock it is told about, so this ends up near four seconds of thinking per move.
MAX_CLOCK_MS = 64_000
MAX_MOVES = 600


def _think(start_fen: str, moves: list[str], clock_ms: int) -> dict[str, object]:
    board = chess.Board(start_fen)
    agent._reset_for_new_game()
    for uci in moves[:MAX_MOVES]:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise ValueError(f"illegal move in history: {uci}")
        board.push(move)
        # What get_move would have recorded had it been asked for every move of this game.
        agent._game_history[agent._key(board)] = (
            agent._game_history.get(agent._key(board), 0) + 1
        )
    if board.is_game_over(claim_draw=False):
        raise ValueError("the game is already over")

    agent._piece_count = chess.popcount(board.occupied)
    budget_clock = min(max(int(clock_ms), 1_000), MAX_CLOCK_MS)
    started = time.perf_counter()
    uci = agent.get_move(board.fen(), budget_clock)
    spent = time.perf_counter() - started

    move = chess.Move.from_uci(uci)
    if move not in board.legal_moves:
        raise ValueError(f"the engine returned an illegal move: {uci}")
    san = board.san(move)
    board.push(move)
    finish = board.outcome(claim_draw=True)
    return {
        "move": uci,
        "san": san,
        "fen": board.fen(),
        "thinking_ms": round(spent * 1000),
        "nodes": agent._nodes,
        "over": finish is not None,
        "termination": finish.termination.name.lower() if finish else None,
        "winner": ("white" if finish.winner else "black")
        if finish and finish.winner is not None
        else None,
    }


class handler(BaseHTTPRequestHandler):  # noqa: N801 (Vercel requires this exact name)
    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            result = _think(
                payload.get("start_fen") or chess.STARTING_FEN,
                list(payload.get("moves") or []),
                int(payload.get("time_left_ms") or 60_000),
            )
            self._send(200, result)
        except ValueError as failure:
            self._send(400, {"error": str(failure)})
        except Exception as failure:
            self._send(500, {"error": f"{type(failure).__name__}: {failure}"})

    def do_OPTIONS(self) -> None:
        self._send(204, {})

    def _send(self, status: int, body: dict[str, object]) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_: object) -> None:
        """Quiet: the platform captures stdout and the default logger is noise."""
