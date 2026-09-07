"""Think on the player's time: work out replies before they are asked for.

Not part of the submission. The packager only picks up Python files at the repository root, so
nothing under api/ can reach the zip, and `agent.py` is untouched.

## Why a cache of answers rather than a warm search

The obvious way to ponder is to keep searching and let the transposition table carry the work
forward. That cannot work here: `move._think` calls `agent._reset_for_new_game()` on every
request, which clears the transposition table and the evaluation cache, because a warm container
may serve a different player's game next and a stale repetition history would make the engine
misjudge draws. Anything the search learned between requests is thrown away.

So what is cached is the finished answer. While the player thinks, the engine is asked what it
would reply to each of the moves they are most likely to play, and each reply is filed under the
exact game that would produce it. When the player finally moves, a filed reply is returned
immediately.

The payoff is not a deeper move, it is a free one. The interface charges the engine the wall time
its reply took, so a reply that was worked out in advance costs it almost none of its clock. That
matters most in the 10 and 30 second games, where the engine otherwise spends a large share of its
clock on every move.

## The loop is driven by the page, not by a thread here

`agent` keeps its search state in module globals, so two searches in one process corrupt each
other; every search has to hold the engine lock. A background thread holding that lock would have
to be interruptible, and the search has no stop hook, only a deadline it was given at the start.

Instead the page asks for one candidate at a time and simply stops asking when the player moves.
There is no cancellation to get wrong, only a request that is not sent. The worst a real move ever
waits is the tail of one candidate search, which is what PONDER_CLOCK_MS bounds.

Cached replies are keyed on the starting position and the exact list of moves, so a hit is by
construction a reply to the position being asked about, not a guess that resembles it.

On a serverless deployment each request may land in a different container, so the cache rarely
survives to be used. Pondering is a local-server feature that costs a deployment nothing but the
requests.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

import chess

import agent

# How much clock a single pondered candidate is allowed. The engine spends about a sixteenth of
# what it is told, so this is close to one second of thinking, which is also the longest a real
# move can end up queued behind a candidate that is already running.
PONDER_CLOCK_MS = 16_000
# Replies are worth having for the moves a player might actually choose, not for all forty.
MAX_CANDIDATES = 12
# Two full move lists per position is plenty; the rest is a memory leak with extra steps.
CACHE_MAX = 64

Key = tuple[str, tuple[str, ...]]

_lock = threading.RLock()
_cache: OrderedDict[Key, dict[str, Any]] = OrderedDict()
_hits = 0
_misses = 0


def _key(start_fen: str, moves: list[str]) -> Key:
    return (start_fen or chess.STARTING_FEN, tuple(moves))


def take(start_fen: str, moves: list[str]) -> dict[str, Any] | None:
    """The reply filed for exactly this game, if there is one.

    Left in place rather than removed: the same position can be reached twice in one game, and a
    reply that was right the first time is still right.
    """
    global _hits, _misses
    with _lock:
        found = _cache.get(_key(start_fen, moves))
        if found is None:
            _misses += 1
            return None
        _cache.move_to_end(_key(start_fen, moves))
        _hits += 1
        return found


def remember(start_fen: str, moves: list[str], reply: dict[str, Any]) -> None:
    with _lock:
        _cache[_key(start_fen, moves)] = reply
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)


def known(start_fen: str, moves: list[str]) -> bool:
    """Whether this game has already been pondered, hit counters untouched."""
    with _lock:
        return _key(start_fen, moves) in _cache


def candidates(board: chess.Board) -> list[chess.Move]:
    """The player's moves, most worth pondering first.

    The engine's own ordering is used as the stand-in for what a player is likely to pick: it puts
    captures, checks and killers first, which is a fair description of the moves a human considers.
    """
    try:
        ordered = agent._ordered_moves(board, None, 0)
    except Exception:
        ordered = list(board.legal_moves)
    return ordered[:MAX_CANDIDATES]


def next_candidate(start_fen: str, moves: list[str], board: chess.Board) -> chess.Move | None:
    """The next likely player move that has not been answered yet."""
    for move in candidates(board):
        if not known(start_fen, [*moves, move.uci()]):
            return move
    return None


def summary() -> dict[str, Any]:
    with _lock:
        return {"cached": len(_cache), "hits": _hits, "misses": _misses}


def forget() -> None:
    with _lock:
        _cache.clear()
