"""Run the web interface locally, without Node or the Vercel CLI.

Development tooling only. Nothing here ships.

    python -m tools.serve            # then open http://localhost:8000
    python -m tools.serve --port 3000

Static files come out of public/ and the two API paths are delegated to the very same handler
methods that Vercel invokes, so what you see locally is the code that runs in production rather
than a second implementation of it that can drift.

Requests are served on threads, because a move takes a couple of seconds and the page fetches
other things while it waits. The engine keeps its state in module globals, so moves are serialised
behind a lock: two searches in one process would otherwise share a transposition table and a
repetition history and quietly corrupt each other.
"""

from __future__ import annotations

import argparse
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
sys.path.insert(0, str(ROOT / "api"))
sys.path.insert(0, str(ROOT))

import _store  # noqa: E402
import games as games_api  # noqa: E402
import move as move_api  # noqa: E402

_engine_lock = threading.Lock()


class Router(SimpleHTTPRequestHandler):
    """Static files from public/, plus the two API routes."""

    def __init__(self, *arguments: object, **keywords: object) -> None:
        super().__init__(*arguments, directory=str(PUBLIC), **keywords)  # type: ignore[arg-type]

    def do_GET(self) -> None:
        if self.path.startswith("/api/games"):
            games_api.handler.do_GET(self)
            return
        if self.path.startswith("/api/"):
            self._send(404, {"error": "no such endpoint"})
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.path.startswith("/api/move"):
            with _engine_lock:
                move_api.handler.do_POST(self)
            return
        if self.path.startswith("/api/games"):
            games_api.handler.do_POST(self)
            return
        self._send(404, {"error": "no such endpoint"})

    def do_OPTIONS(self) -> None:
        self._send(204, {})

    # The API handlers call self._send, so the router has to provide it.
    def _send(self, status: int, body: dict[str, object]) -> None:
        import json

        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *arguments: object) -> None:
        # One tidy line per request instead of the default two.
        sys.stderr.write(f"  {self.command:4s} {self.path} -> {format % arguments}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    arguments = parser.parse_args()

    print(f"serving {PUBLIC} on http://{arguments.host}:{arguments.port}")
    print(f"  engine     agent.py, capped at {move_api.MAX_CLOCK_MS // 1000}s of clock per move")
    if _store.is_persistent():
        print("  storage    Upstash Redis (games will persist)")
    else:
        print("  storage    in memory only, games vanish when this process stops")
        print("             set KV_REST_API_URL and KV_REST_API_TOKEN to keep them")
    print("stop with ctrl-c")
    try:
        ThreadingHTTPServer((arguments.host, arguments.port), Router).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
