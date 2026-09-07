"""Let the web interface's engine learn from the games it has played.

Not part of the chess engine and not part of the submission. The packager only picks up Python
files at the repository root, so nothing under api/ can reach the zip, and `agent.py` is not
touched by any of this. The engine the competition sees is unchanged.

## Why this can exist here and not there

The platform gives the agent a fresh process per game, keeps no module state between games, mounts
the filesystem read-only and allows no network, so a lesson learned in one rated game has nowhere
to be written down. The web interface has none of those limits: one long-lived process and a disk
it may write to. So here, and only here, the engine can remember how a position went last time.

## What it remembers

For every position the engine moved in, which move it chose and how that game ended for it. That
is all. No evaluation, no search data, no model.

    {position: {move: [games, points]}}      points: 1 a win, 0.5 a draw, 0 a loss

Positions are keyed by EPD, so the move counters do not split one position into many.

That is enough for the thing a human playing the same opening every evening will find first: the
engine walking into an identical lost line twice. After a loss the move that led there is
discouraged; after wins it is repeated. It is a book learned from experience, not a rewrite of the
evaluation, and it deliberately only ever chooses between moves that are legal right now.

Weight tuning is the other half of learning and is a separate, offline thing: see tools/harvest.py.

## Where it is kept

A JSON blob, in Upstash when KV_REST_API_* are set, otherwise a file under games/. Serverless
deployments have a read-only disk, so without Upstash a deployed instance learns only for as long
as its container lives. Locally the file is the normal case and survives restarts.

Set DRUNKENMASTER_NO_LEARNING=1 to turn the whole thing off and get the raw engine back.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import _store
import chess

BOOK_KEY = "chessathon:book"
BOOK_FILE = Path(__file__).resolve().parents[1] / "games" / "book.json"

# A move needs to have been tried this many times before its record counts for anything. One game
# is an anecdote: the opponent may simply have blundered afterwards.
MIN_GAMES = 2
# Score rates, from the engine's point of view, at which a move is worth dropping or repeating.
AVOID_AT_OR_BELOW = 0.34
PREFER_AT_OR_ABOVE = 0.67
# Only the opening and early middlegame are worth learning. Later positions almost never repeat,
# so storing them costs space and buys nothing.
MAX_PLY = 40
# Keep the book bounded. Least-played positions go first.
MAX_POSITIONS = 20_000

_lock = threading.RLock()
_cache: dict[str, dict[str, list[float]]] | None = None
_memory: dict[str, dict[str, list[float]]] = {}


def enabled() -> bool:
    return os.environ.get("DRUNKENMASTER_NO_LEARNING", "") not in ("1", "true", "yes")


def storage() -> str:
    """Which backend is in play, for the interface to report."""
    if not enabled():
        return "off"
    if _store.is_persistent():
        return "upstash"
    return "file" if _writable() else "memory"


def _writable() -> bool:
    try:
        BOOK_FILE.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(BOOK_FILE.parent, os.W_OK)


def _decode(raw: str | None) -> dict[str, dict[str, list[float]]]:
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    # Hand-checked rather than trusted: this file is also editable by hand.
    book: dict[str, dict[str, list[float]]] = {}
    for position, moves in loaded.items():
        if not isinstance(position, str) or not isinstance(moves, dict):
            continue
        cleaned: dict[str, list[float]] = {}
        for move, stat in moves.items():
            if isinstance(move, str) and isinstance(stat, list) and len(stat) == 2:
                try:
                    cleaned[move] = [float(stat[0]), float(stat[1])]
                except (TypeError, ValueError):
                    continue
        if cleaned:
            book[position] = cleaned
    return book


def _load() -> dict[str, dict[str, list[float]]]:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        raw: str | None = None
        if _store.is_persistent():
            try:
                raw = _store._command("GET", BOOK_KEY)
            except Exception:
                raw = None
        elif BOOK_FILE.is_file():
            try:
                raw = BOOK_FILE.read_text(encoding="utf-8")
            except OSError:
                raw = None
        _cache = _decode(raw) or dict(_memory)
        return _cache


def _persist(book: dict[str, dict[str, list[float]]]) -> None:
    """Write the book back. Failure is not allowed to cost the caller its request."""
    payload = json.dumps(book, separators=(",", ":"), sort_keys=True)
    if _store.is_persistent():
        try:
            _store._command("SET", BOOK_KEY, payload)
            return
        except Exception:
            pass
    if _writable():
        try:
            # Write beside the target and move it into place, so a crash mid-write cannot leave a
            # truncated book behind.
            temporary = BOOK_FILE.with_suffix(".json.tmp")
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(BOOK_FILE)
            return
        except OSError:
            pass
    _memory.clear()
    _memory.update(book)


def _trim(book: dict[str, dict[str, list[float]]]) -> None:
    if len(book) <= MAX_POSITIONS:
        return
    ranked = sorted(book, key=lambda position: sum(n for n, _ in book[position].values()))
    for position in ranked[: len(book) - MAX_POSITIONS]:
        del book[position]


def record_game(
    start_fen: str, moves: list[str], engine_colour: str, engine_points: float
) -> int:
    """Fold one finished game into the book. Returns how many positions were updated.

    engine_colour is "white" or "black"; engine_points is 1, 0.5 or 0 from the engine's side.
    """
    if not enabled():
        return 0
    colour = chess.WHITE if engine_colour == "white" else chess.BLACK
    try:
        board = chess.Board(start_fen or chess.STARTING_FEN)
    except ValueError:
        return 0

    updates: list[tuple[str, str]] = []
    for uci in moves[:MAX_PLY]:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            break
        if move not in board.legal_moves:
            break
        if board.turn == colour:
            updates.append((board.epd(), uci))
        board.push(move)

    if not updates:
        return 0
    with _lock:
        book = _load()
        for position, uci in updates:
            entry = book.setdefault(position, {})
            stat = entry.setdefault(uci, [0.0, 0.0])
            stat[0] += 1.0
            stat[1] += engine_points
        _trim(book)
        _persist(book)
    return len(updates)


def advise(
    board: chess.Board, engine_move: str, rank: Callable[[str], float] | None = None
) -> tuple[str, str | None]:
    """The move to actually play, and a note when experience changed it.

    Only ever returns a move that is legal in `board`: the engine's own unless the book has a
    strong enough reason, over enough games, to prefer something else.
    """
    if not enabled():
        return engine_move, None
    entry = _load().get(board.epd())
    if not entry:
        return engine_move, None

    legal = {move.uci() for move in board.legal_moves}
    rates = {
        uci: (stat[0], stat[1] / stat[0])
        for uci, stat in entry.items()
        if uci in legal and stat[0] >= MIN_GAMES and stat[0] > 0
    }
    if not rates:
        return engine_move, None

    # Best by score, then by how much evidence there is for it.
    best = max(rates, key=lambda uci: (rates[uci][1], rates[uci][0]))
    best_games, best_rate = rates[best]
    mine = rates.get(engine_move)

    if mine and mine[1] <= AVOID_AT_OR_BELOW:
        if best != engine_move and best_rate > mine[1]:
            return best, (
                f"{engine_move} has scored {mine[1]:.0%} over {mine[0]:.0f} games, "
                f"so played {best} ({best_rate:.0%} over {best_games:.0f}) instead"
            )
        # Everything tried in this position has gone badly, and the search keeps offering one of
        # them. Without this the same lost line repeats for ever, since the search is given the
        # same position and the same budget every time. So try a move that has not been tried,
        # ranked by the engine's own static evaluation, which explores rather than flails.
        untried = sorted(legal - set(entry))
        if rank is not None and untried:
            fresh = max(untried, key=rank)
            return fresh, (
                f"{engine_move} has scored {mine[1]:.0%} over {mine[0]:.0f} games and nothing "
                f"else tried here has worked, so tried {fresh}"
            )
    worth_repeating = best_rate >= PREFER_AT_OR_ABOVE and best != engine_move
    if worth_repeating and (not mine or best_rate > mine[1]):
        return best, (
            f"played {best} from experience: {best_rate:.0%} over {best_games:.0f} games"
        )
    return engine_move, None


def summary() -> dict[str, Any]:
    """What the interface shows about the book."""
    book = _load() if enabled() else {}
    games = sum(stat[0] for moves in book.values() for stat in moves.values())
    return {
        "storage": storage(),
        "positions": len(book),
        "decisions": int(games),
    }


def forget() -> None:
    """Throw the book away. Used by the interface's reset, and by the tests."""
    global _cache
    with _lock:
        _cache = {}
        _memory.clear()
        _persist({})
