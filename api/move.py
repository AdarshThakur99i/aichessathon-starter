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
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _learn
import _ponder

import agent

# Keep the engine's own budget under the function timeout. It spends about a sixteenth of the
# clock it is told about, so this ends up near four seconds of thinking per move.
MAX_CLOCK_MS = 64_000
MAX_MOVES = 600


def _static_rank(board: chess.Board) -> Callable[[str], float]:
    """Score a candidate move by the engine's own static evaluation of where it leads.

    Only used when the book has run out of tried moves and has to pick something new, so that
    exploring is at least guided by the engine's judgement. evaluate() reports from the point of
    view of the side to move, and after our own move that is the opponent, hence the negation.
    """

    def rank(uci: str) -> float:
        board.push(chess.Move.from_uci(uci))
        try:
            return -agent.evaluate(board)
        finally:
            board.pop()

    return rank


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

    # A reply worked out while the player was thinking. The cache is keyed on this exact game, so
    # a hit is a reply to this position rather than to one that resembles it; the legality check
    # is belt and braces.
    ready = _ponder.take(start_fen, moves)
    if ready and ready.get("move"):
        prepared = chess.Move.from_uci(str(ready["move"]))
        if prepared in board.legal_moves:
            return {**ready, "pondered": True}

    agent._piece_count = chess.popcount(board.occupied)
    budget_clock = min(max(int(clock_ms), 1_000), MAX_CLOCK_MS)
    started = time.perf_counter()
    uci = agent.get_move(board.fen(), budget_clock)
    spent = time.perf_counter() - started

    move = chess.Move.from_uci(uci)
    if move not in board.legal_moves:
        raise ValueError(f"the engine returned an illegal move: {uci}")

    # What earlier games in this interface suggest. It can only ever name another legal move, and
    # the result is checked again here anyway rather than trusted.
    uci, learned = _learn.advise(board, uci, _static_rank(board))
    move = chess.Move.from_uci(uci)
    if move not in board.legal_moves:
        raise ValueError(f"the learned book returned an illegal move: {uci}")

    san = board.san(move)
    board.push(move)
    finish = board.outcome(claim_draw=True)
    return {
        "move": uci,
        "san": san,
        "fen": board.fen(),
        "thinking_ms": round(spent * 1000),
        "nodes": agent._nodes,
        "learned": learned,
        "pondered": False,
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
