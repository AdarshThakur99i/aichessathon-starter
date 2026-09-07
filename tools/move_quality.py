"""How good were the moves in a game, and what would our agent have played instead?

Development tooling only. Nothing here ships. Stockfish is used purely as a neutral referee at
analysis time, which is what the rules allow; it is never shipped and never consulted at runtime.

Every position in the game is scored three ways: the reference's own best move, the move that was
actually played, and the move our agent picks from that same position. The gap between the
reference's evaluation of its best move and of a played move is that move's centipawn loss, and
averaging it over a game is the standard way to compare playing strength without needing hundreds
of games. Blunder counts are reported alongside the mean, because one catastrophe and twenty small
inaccuracies average the same but do not lose the same number of games.

    python -m tools.move_quality --pgn game.pgn --depth 14 --our-ms 3000
"""

from __future__ import annotations

import argparse
import io
import os
import statistics
import sys
from pathlib import Path

import chess
import chess.engine
import chess.pgn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_STOCKFISH = (
    r"C:\Users\Adarsh Thakur\Downloads\stockfish-windows-x86-64-avx2"
    r"\stockfish\stockfish-windows-x86-64-avx2.exe"
)
# Losses are clipped before averaging. Once a position is lost the reference's numbers explode,
# and without a clip a single hopeless position would dominate the mean and say nothing about the
# quality of play that led there.
CLIP_CENTIPAWNS = 1000
BLUNDER_CENTIPAWNS = 200
INACCURACY_CENTIPAWNS = 50


def score_for(info: chess.engine.InfoDict, colour: chess.Color) -> int:
    """The reference's evaluation in centipawns, from the given side's point of view."""
    score = info["score"]
    assert score is not None
    return score.pov(colour).score(mate_score=100_000)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pgn", type=Path, required=True)
    parser.add_argument("--engine", type=Path, default=Path(DEFAULT_STOCKFISH))
    parser.add_argument("--depth", type=int, default=14)
    parser.add_argument("--our-ms", type=int, default=3000)
    parser.add_argument("--max-plies", type=int, default=0)
    parser.add_argument("--dump", type=Path, default=None)
    arguments = parser.parse_args()

    game = chess.pgn.read_game(io.StringIO(arguments.pgn.read_text(encoding="utf-8")))
    if game is None:
        raise SystemExit("could not read a game from that pgn")
    white = game.headers.get("White", "white")
    black = game.headers.get("Black", "black")

    os.environ.setdefault("CHESSATHON_MAX_DEPTH", "32")
    import agent

    engine = chess.engine.SimpleEngine.popen_uci(str(arguments.engine))
    engine.configure({"Threads": 1})
    limit = chess.engine.Limit(depth=arguments.depth)

    losses: dict[str, list[int]] = {white: [], black: [], "our agent": []}
    agreements = 0
    considered = 0
    board = game.board()
    moves = list(game.mainline_moves())
    if arguments.max_plies:
        moves = moves[: arguments.max_plies]

    for played in moves:
        mover = board.turn
        # A fresh game token per probe makes python-chess send ucinewgame, which clears the
        # reference's hash. Without it each result depends on what was analysed before it, and
        # two runs over the same file disagreed by 10 centipawns a move, which is larger than
        # most changes worth measuring.
        best = engine.analyse(board, limit, game=object())
        best_score = score_for(best, mover)

        def loss_after(
            move: chess.Move, mover: chess.Color = mover, reference: int = best_score
        ) -> int:
            board.push(move)
            try:
                value = score_for(engine.analyse(board, limit, game=object()), mover)
            finally:
                board.pop()
            return max(0, min(CLIP_CENTIPAWNS, reference - value))

        who = white if mover == chess.WHITE else black
        losses[who].append(loss_after(played))

        agent._reset_for_new_game()
        agent._piece_count = chess.popcount(board.occupied)
        agent._increment_s = 0.5
        ours = chess.Move.from_uci(agent.get_move(board.fen(), arguments.our_ms * 40))
        losses["our agent"].append(loss_after(ours))
        considered += 1
        if ours == played:
            agreements += 1

        board.push(played)
        if considered % 10 == 0:
            print(f"  scored {considered} positions", flush=True)

    engine.quit()
    if arguments.dump:
        import json
        arguments.dump.write_text(json.dumps(losses))

    print(f"\n{arguments.pgn.name}: {len(moves)} plies scored at reference depth {arguments.depth}")
    print(f"our agent agreed with the move actually played in {agreements}/{considered}\n")
    print(f"  {'player':14s} {'moves':>6s} {'mean cp loss':>13s} {'median':>7s} "
          f"{'inacc>50':>9s} {'blunders>200':>13s}")
    for name, values in losses.items():
        if not values:
            continue
        print(
            f"  {name[:14]:14s} {len(values):6d} {statistics.mean(values):13.1f} "
            f"{statistics.median(values):7.1f} "
            f"{sum(1 for v in values if v > INACCURACY_CENTIPAWNS):9d} "
            f"{sum(1 for v in values if v > BLUNDER_CENTIPAWNS):13d}"
        )


if __name__ == "__main__":
    main()
