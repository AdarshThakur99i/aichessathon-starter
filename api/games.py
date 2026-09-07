"""Player folders: save a finished game, list players, read one player's games.

Not part of the submission; the packager only collects Python files at the repository root.

    GET  /api/games                -> every player folder, most recent first
    GET  /api/games?player=<slug>  -> that player's games, newest first
    POST /api/games                -> store one finished game
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# The Vercel runtime loads this file by path with only the project root importable, so the path is
# set up by hand: the root so agent can be found, and this directory for the modules sitting next
# to this one. Having only the root was a ModuleNotFoundError for _learn on every deployed request.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _learn
import _store

MAX_PGN_CHARS = 20_000
RESULTS = {"won", "lost", "drawn"}


def _record(payload: dict[str, object]) -> dict[str, object]:
    """Validate what the page sent. Anything unrecognised is dropped rather than stored."""
    name = str(payload.get("player") or "").strip()
    if not name:
        raise ValueError("a player name is required")
    if len(name) > 40:
        raise ValueError("that name is too long")
    result = str(payload.get("result") or "")
    if result not in RESULTS:
        raise ValueError("result must be won, lost or drawn")
    pgn = str(payload.get("pgn") or "")[:MAX_PGN_CHARS]
    return {
        "result": result,
        "pgn": pgn,
        "moves": [str(m) for m in (payload.get("moves") or [])][:600],
        "start_fen": str(payload.get("start_fen") or ""),
        "colour": "white" if payload.get("colour") == "white" else "black",
        "time_control": str(payload.get("time_control") or "")[:20],
        "termination": str(payload.get("termination") or "")[:40],
        "plies": int(payload.get("plies") or 0),
    }


def _learn_from(record: dict[str, object]) -> int:
    """Fold a finished game into the engine's book, and report how many positions it touched.

    The result stored on the record is from the player's point of view, so it is inverted here.
    Never fatal: the game itself is already saved, and a book that failed to update is not worth
    failing the request over.
    """
    points = {"won": 0.0, "lost": 1.0, "drawn": 0.5}.get(str(record.get("result")), 0.5)
    engine_colour = "black" if record.get("colour") == "white" else "white"
    moves = record.get("moves")
    try:
        return _learn.record_game(
            str(record.get("start_fen") or ""),
            [str(uci) for uci in moves] if isinstance(moves, list) else [],
            engine_colour,
            points,
        )
    except Exception:
        return 0


class handler(BaseHTTPRequestHandler):  # noqa: N801 (Vercel requires this exact name)
    def do_GET(self) -> None:
        try:
            query = parse_qs(urlparse(self.path).query)
            wanted = (query.get("player") or [""])[0]
            if wanted:
                games = _store.games_for(wanted)
                self._send(
                    200,
                    {
                        "player": games[0].get("player", wanted) if games else wanted,
                        "slug": _store.slug(wanted),
                        "games": games,
                        "persistent": _store.is_persistent(),
                    },
                )
            else:
                self._send(
                    200,
                    {
                        "players": _store.players(),
                        "persistent": _store.is_persistent(),
                        "learning": _learn.summary(),
                    },
                )
        except Exception as failure:
            self._send(500, {"error": f"{type(failure).__name__}: {failure}"})

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            record = _record(payload)
            folder = _store.save_game(str(payload["player"]).strip(), record)
            self._send(
                200,
                {
                    "slug": folder,
                    "persistent": _store.is_persistent(),
                    "learned": _learn_from(record),
                },
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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_: object) -> None:
        """Quiet: the platform captures stdout and the default logger is noise."""
