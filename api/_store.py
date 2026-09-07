"""Game storage for the web interface, backed by Upstash Redis over its REST API.

Not part of the chess engine and not part of the submission. The packager only picks up Python
files at the repository root, so nothing under api/ can reach the zip.

REST rather than a Redis client library because serverless Python has no connection pooling worth
speaking of: a plain HTTPS call per request avoids holding a socket open across cold starts, and it
needs no dependency beyond the standard library.

Set either pair of environment variables and storage turns itself on:

    KV_REST_API_URL / KV_REST_API_TOKEN                 (Vercel's Upstash integration sets these)
    UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN   (Upstash's own names)

With neither set it falls back to a process-local dictionary, so the site runs locally with no
setup at all. That fallback is per-process and evaporates on restart, which is fine for a
development run and useless in production, so is_persistent() reports which one is in play.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

PLAYER_INDEX = "chessathon:players"
GAMES_PREFIX = "chessathon:games:"
# A player folder is a page, not an archive. Older games fall off the end rather than growing a
# list that has to be fetched in full on every view.
MAX_GAMES_PER_PLAYER = 60
REQUEST_TIMEOUT_S = 8.0

_memory: dict[str, list[str]] = {}
_memory_players: set[str] = set()


def _credentials() -> tuple[str, str] | None:
    for url_name, token_name in (
        ("KV_REST_API_URL", "KV_REST_API_TOKEN"),
        ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"),
    ):
        url = os.environ.get(url_name)
        token = os.environ.get(token_name)
        if url and token:
            return url.rstrip("/"), token
    return None


def is_persistent() -> bool:
    """True when games will outlive this process."""
    return _credentials() is not None


def _command(*parts: str | int) -> Any:
    """Run one Redis command. Arguments go in the body so values need no URL escaping."""
    credentials = _credentials()
    if credentials is None:
        raise RuntimeError("no storage configured")
    url, token = credentials
    request = urllib.request.Request(
        url,
        data=json.dumps([str(part) for part in parts]).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
        return json.load(response).get("result")


def slug(name: str) -> str:
    """A key and URL safe form of a display name, so two spellings share one folder."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return cleaned[:40] or "anonymous"


def save_game(player: str, record: dict[str, Any]) -> str:
    """Store one finished game under a player and return the folder it went to."""
    key = slug(player)
    record = dict(record)
    record["player"] = player
    record["player_slug"] = key
    record.setdefault("finished_at", time.time())
    payload = json.dumps(record, separators=(",", ":"))
    if is_persistent():
        try:
            _command("LPUSH", GAMES_PREFIX + key, payload)
            _command("LTRIM", GAMES_PREFIX + key, 0, MAX_GAMES_PER_PLAYER - 1)
            # A sorted set keyed on time gives the index and the ordering in one structure.
            _command("ZADD", PLAYER_INDEX, int(record["finished_at"]), key)
        except (urllib.error.URLError, OSError, ValueError, RuntimeError):
            # A storage outage must not lose the player their game, so it is kept in process
            # and the caller is told persistence is degraded rather than shown an error.
            _memory.setdefault(key, []).insert(0, payload)
            _memory_players.add(key)
    else:
        _memory.setdefault(key, []).insert(0, payload)
        del _memory[key][MAX_GAMES_PER_PLAYER:]
        _memory_players.add(key)
    return key


def games_for(player: str) -> list[dict[str, Any]]:
    """Every stored game for one player, newest first."""
    key = slug(player)
    raw: list[str] = []
    if is_persistent():
        try:
            result = _command("LRANGE", GAMES_PREFIX + key, 0, MAX_GAMES_PER_PLAYER - 1)
            raw = list(result or [])
        except (urllib.error.URLError, OSError, ValueError, RuntimeError):
            raw = []
    if not raw:
        raw = _memory.get(key, [])
    games = []
    for item in raw:
        try:
            games.append(json.loads(item))
        except json.JSONDecodeError:
            continue
    return games


def players() -> list[dict[str, Any]]:
    """Every player folder, most recently active first, with a game count each."""
    keys: list[str] = []
    if is_persistent():
        try:
            result = _command("ZRANGE", PLAYER_INDEX, 0, -1, "REV")
            keys = list(result or [])
        except (urllib.error.URLError, OSError, ValueError, RuntimeError):
            keys = []
    if not keys:
        keys = sorted(_memory_players)
    listing = []
    for key in keys:
        games = games_for(key)
        if not games:
            continue
        listing.append(
            {
                "slug": key,
                "name": games[0].get("player", key),
                "games": len(games),
                "last_played": games[0].get("finished_at"),
                "record": _tally(games),
            }
        )
    return listing


def _tally(games: list[dict[str, Any]]) -> dict[str, int]:
    """Wins, draws and losses from the human's point of view."""
    tally = {"won": 0, "drawn": 0, "lost": 0}
    for game in games:
        outcome = game.get("result")
        if outcome in tally:
            tally[outcome] += 1
    return tally
