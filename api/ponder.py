"""Ponder one candidate reply. Serverless entry point for the web interface.

Not part of the submission; see api/_ponder.py for what this is for and why the page drives it one
request at a time instead of a thread doing it here.

    POST /api/ponder  {start_fen, moves, time_left_ms}

`moves` is the game so far, with the player to move. One likely player move that has not been
answered yet is picked, the engine's reply to it is worked out and filed, and the response says
whether there is anything left to do. The page calls this repeatedly while the player thinks and
stops calling when they move.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _ponder
import move as move_api


def _ponder_one(start_fen: str, moves: list[str], clock_ms: int) -> dict[str, object]:
    board = chess.Board(start_fen)
    for uci in moves[: move_api.MAX_MOVES]:
        candidate = chess.Move.from_uci(uci)
        if candidate not in board.legal_moves:
            raise ValueError(f"illegal move in history: {uci}")
        board.push(candidate)
    if board.is_game_over(claim_draw=False):
        return {"done": True, **_ponder.summary()}

    guess = _ponder.next_candidate(start_fen, moves, board)
    if guess is None:
        return {"done": True, **_ponder.summary()}

    played = board.san(guess)
    ahead = [*moves, guess.uci()]
    clock = min(int(clock_ms), _ponder.PONDER_CLOCK_MS)
    try:
        reply = move_api._think(start_fen, ahead, clock)
    except ValueError:
        # The candidate ends the game, so there is no reply to file. Remembered as a dead end so
        # the same move is not offered again on the next call.
        _ponder.remember(start_fen, ahead, {"none": True})
        return {
            "done": False,
            "pondered": played,
            "uci": guess.uci(),
            "reply": None,
            **_ponder.summary(),
        }

    _ponder.remember(start_fen, ahead, reply)
    return {
        "done": False,
        "pondered": played,
        "uci": guess.uci(),
        "reply": reply.get("san"),
        **_ponder.summary(),
    }


class handler(BaseHTTPRequestHandler):  # noqa: N801 (Vercel requires this exact name)
    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            self._send(
                200,
                _ponder_one(
                    payload.get("start_fen") or chess.STARTING_FEN,
                    list(payload.get("moves") or []),
                    int(payload.get("time_left_ms") or 60_000),
                ),
            )
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
        """Quiet: the local server logs one line per request itself."""
