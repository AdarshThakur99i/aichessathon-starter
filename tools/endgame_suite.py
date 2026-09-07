"""Can the engine actually finish a won endgame?

Development tooling only. Nothing here ships.

Basic mates are the one part of chess where the right answer is known without consulting anything:
king and queen against a bare king is a win from every legal position, and so are king and rook,
two bishops, and bishop and knight. So the engine plays the strong side from randomly placed legal
positions against the strongest defence available, and the only question asked is whether it
delivers mate before the fifty move rule takes the win away. A failure here is unambiguous.

    python -m tools.endgame_suite --depth 5 --positions 5
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.referee import play_match
from harness.sandbox import local

# Material for the strong side, with the weak side left a bare king. Ordered easiest first, so a
# run that stops early still says something useful.
MATERIAL = (
    ("KQ v K", [chess.QUEEN]),
    ("KR v K", [chess.ROOK]),
    ("KBB v K", [chess.BISHOP, chess.BISHOP]),
    ("KBN v K", [chess.BISHOP, chess.KNIGHT]),
)


def make_position(pieces: list[int], rng: random.Random) -> str:
    """A random legal position with this material, strong side to move and no immediate capture."""
    while True:
        squares = rng.sample(range(64), len(pieces) + 2)
        board = chess.Board(None)
        board.set_piece_at(squares[0], chess.Piece(chess.KING, chess.WHITE))
        board.set_piece_at(squares[1], chess.Piece(chess.KING, chess.BLACK))
        for piece, square in zip(pieces, squares[2:], strict=True):
            board.set_piece_at(square, chess.Piece(piece, chess.WHITE))
        # Two bishops on one colour cannot mate, so such a position is a genuine draw and
        # counting it as a failed conversion would be measuring the wrong thing.
        bishops = list(chess.scan_forward(board.bishops))
        if len(bishops) == 2 and (
            chess.BB_DARK_SQUARES & chess.BB_SQUARES[bishops[0]] != 0
        ) == (chess.BB_DARK_SQUARES & chess.BB_SQUARES[bishops[1]] != 0):
            continue
        board.turn = chess.WHITE
        if not board.is_valid():
            continue
        # A position where the lone king can immediately take the winning piece tests nothing.
        if any(board.is_capture(move) for move in board.legal_moves):
            continue
        if board.is_check() or board.is_game_over():
            continue
        return board.fen()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", type=Path, default=Path("."))
    parser.add_argument("--opponent", type=Path, default=Path("baselines/stockfish"))
    parser.add_argument("--depth", type=int, default=5)
    parser.add_argument("--engine-depth", type=int, default=6)
    parser.add_argument("--positions", type=int, default=5)
    parser.add_argument("--base-ms", type=int, default=300_000)
    parser.add_argument("--increment-ms", type=int, default=1_000)
    arguments = parser.parse_args()

    os.environ["CHESSATHON_MAX_DEPTH"] = str(arguments.depth)
    os.environ["STOCKFISH_DEPTH"] = str(arguments.engine_depth)
    agent = arguments.agent.resolve()
    opponent = arguments.opponent.resolve()
    rng = random.Random(1)

    overall_won = overall_total = 0
    for label, pieces in MATERIAL:
        won = 0
        details: list[str] = []
        for _ in range(arguments.positions):
            fen = make_position(list(pieces), rng)
            outcome = play_match(
                local(agent), local(opponent), arguments.base_ms, arguments.increment_ms,
                start_fen=fen,
            )
            if outcome.result == "white":
                won += 1
                details.append("win")
            else:
                details.append(outcome.termination)
        overall_won += won
        overall_total += arguments.positions
        print(f"  {label:10s} {won}/{arguments.positions} mated   {', '.join(details)}", flush=True)

    print(f"\nconverted {overall_won}/{overall_total} theoretically won endings")


if __name__ == "__main__":
    main()
