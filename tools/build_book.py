"""Build a Polyglot opening book from master game PGNs.

Development tooling only. Nothing here ships; the book it writes does.

The book is built from human master games on purpose. The rules allow an opening book as shipped
data inside the size cap, but they also say a database of engine moves or evaluations shipped for
lookup at runtime counts as an engine. Games between titled humans are neither, so the moves in
this book are the moves people played, weighted by how they scored, and no engine is consulted at
any point in the build.

    python -m tools.build_book --pgn scratch/twic --out weights/book.bin

Polyglot is used rather than a bespoke format because python-chess can already read it with a
memory-mapped binary search, so probing costs no load time and almost no memory.
"""

from __future__ import annotations

import argparse
import struct
from collections import defaultdict
from pathlib import Path

import chess
import chess.pgn
import chess.polyglot

# A book only has to cover the part of the game the search handles worst. Past this the engine is
# on its own anyway, and every extra ply multiplies the number of positions to store.
DEFAULT_MAX_PLY = 24
# Both players this strong or the game does not go in. The point of the book is to inherit
# judgement the evaluator does not have, which means it has to come from players who have it.
DEFAULT_MIN_ELO = 2300
# A line has to have been played a few times before it is worth trusting, otherwise the book fills
# up with one-off novelties and transcription errors.
DEFAULT_MIN_POSITION_GAMES = 8
DEFAULT_MIN_MOVE_GAMES = 3

_PROMOTION_CODE = {
    None: 0,
    chess.KNIGHT: 1,
    chess.BISHOP: 2,
    chess.ROOK: 3,
    chess.QUEEN: 4,
}
# Polyglot writes a castling move as the king capturing its own rook, not as the two-square king
# move python-chess reports, so these have to be translated on the way out.
_CASTLE_TARGET = {
    (chess.E1, chess.G1): chess.H1,
    (chess.E1, chess.C1): chess.A1,
    (chess.E8, chess.G8): chess.H8,
    (chess.E8, chess.C8): chess.A8,
}
_MAX_WEIGHT = 0xFFFF


def encode_move(board: chess.Board, move: chess.Move) -> int:
    """Pack a move the way Polyglot expects: to square low, from square high, promotion on top."""
    to_square = move.to_square
    if board.piece_type_at(move.from_square) == chess.KING:
        to_square = _CASTLE_TARGET.get((move.from_square, move.to_square), to_square)
    return (
        chess.square_file(to_square)
        | (chess.square_rank(to_square) << 3)
        | (chess.square_file(move.from_square) << 6)
        | (chess.square_rank(move.from_square) << 9)
        | (_PROMOTION_CODE[move.promotion] << 12)
    )


def _elo(headers: chess.pgn.Headers, key: str) -> int:
    try:
        return int(headers.get(key, "0"))
    except ValueError:
        return 0


def harvest(
    paths: list[Path], max_ply: int, min_elo: int
) -> tuple[dict[tuple[int, int], list[int]], dict[int, int]]:
    """Count how often each move was played from each position, and how it scored.

    Scores are kept from the point of view of the side to move, so a move is rewarded for winning
    for whoever played it rather than for winning for white.
    """
    moves: dict[tuple[int, int], list[int]] = defaultdict(lambda: [0, 0])
    positions: dict[int, int] = defaultdict(int)
    games = kept = 0
    for path in paths:
        with path.open(encoding="utf-8", errors="replace") as handle:
            while True:
                game = chess.pgn.read_game(handle)
                if game is None:
                    break
                games += 1
                if games % 5000 == 0:
                    print(f"  read {games:,} games, kept {kept:,}", flush=True)
                result = game.headers.get("Result", "*")
                if result not in ("1-0", "0-1", "1/2-1/2"):
                    continue
                if min(_elo(game.headers, "WhiteElo"), _elo(game.headers, "BlackElo")) < min_elo:
                    continue
                kept += 1
                # Two points for a win and one for a draw, so integer arithmetic throughout.
                white_points = {"1-0": 2, "1/2-1/2": 1, "0-1": 0}[result]
                board = game.board()
                for ply, move in enumerate(game.mainline_moves()):
                    if ply >= max_ply:
                        break
                    if move not in board.legal_moves:
                        break
                    key = chess.polyglot.zobrist_hash(board)
                    entry = moves[(key, encode_move(board, move))]
                    entry[0] += 1
                    entry[1] += white_points if board.turn == chess.WHITE else 2 - white_points
                    positions[key] += 1
                    board.push(move)
    print(f"  read {games:,} games, kept {kept:,} at {min_elo}+")
    return moves, positions


def build(arguments: argparse.Namespace) -> None:
    paths = sorted(
        path
        for source in arguments.pgn
        for path in ([source] if source.is_file() else source.rglob("*.pgn"))
    )
    if not paths:
        raise SystemExit("no pgn files found")
    print(f"{len(paths)} pgn files")
    moves, positions = harvest(paths, arguments.max_ply, arguments.min_elo)

    entries: list[tuple[int, int, int]] = []
    for (key, move), (count, points) in moves.items():
        if positions[key] < arguments.min_position_games or count < arguments.min_move_games:
            continue
        # Polyglot picks by weight, so the weight has to say "how often, and how well" at once.
        # Points already carry both: a move played ten times and drawn every time scores the same
        # as one played five times and won every time, which is the trade a book should make.
        weight = min(points, _MAX_WEIGHT)
        if weight > 0:
            entries.append((key, move, weight))

    entries.sort(key=lambda entry: (entry[0], -entry[2]))
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    # Written beside the target and moved into place. Reading a book memory-maps it, so a live
    # agent process holds a lock on the old file and opening it for writing fails outright, which
    # would otherwise throw away the whole parse at the very last step. The move also means a
    # failed build leaves the previous book intact rather than a half-written one.
    staging = arguments.out.with_suffix(arguments.out.suffix + ".tmp")
    with staging.open("wb") as handle:
        for key, move, weight in entries:
            handle.write(struct.pack(">QHHI", key, move, weight, 0))
    staging.replace(arguments.out)
    size = arguments.out.stat().st_size
    print(f"{len(entries):,} entries over {len({e[0] for e in entries}):,} positions")
    print(f"wrote {arguments.out} ({size:,} bytes)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pgn", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, default=Path("weights/book.bin"))
    parser.add_argument("--max-ply", type=int, default=DEFAULT_MAX_PLY)
    parser.add_argument("--min-elo", type=int, default=DEFAULT_MIN_ELO)
    parser.add_argument("--min-position-games", type=int, default=DEFAULT_MIN_POSITION_GAMES)
    parser.add_argument("--min-move-games", type=int, default=DEFAULT_MIN_MOVE_GAMES)
    parser.set_defaults(handler=build)
    arguments = parser.parse_args()
    arguments.handler(arguments)


if __name__ == "__main__":
    main()
