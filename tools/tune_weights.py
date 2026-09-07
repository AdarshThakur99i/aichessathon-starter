"""Offline evaluator-weight tuning with a locally installed Stockfish.

This file is development tooling only. Do not include it in the competition submission.
"""

from __future__ import annotations

import argparse
import random
import subprocess
import sys
from pathlib import Path

import chess
import chess.pgn
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent


class Stockfish:
    def __init__(self, executable: Path, depth: int) -> None:
        self.process = subprocess.Popen(
            [str(executable)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self.depth = depth
        self._send("uci")
        self._wait_for("uciok")
        self._send("isready")
        self._wait_for("readyok")

    def _send(self, command: str) -> None:
        if self.process.stdin is None:
            raise RuntimeError("Stockfish stdin is unavailable")
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()

    def _wait_for(self, expected: str) -> list[str]:
        if self.process.stdout is None:
            raise RuntimeError("Stockfish stdout is unavailable")
        lines: list[str] = []
        for line in self.process.stdout:
            line = line.strip()
            lines.append(line)
            if line == expected or line.startswith(expected + " "):
                return lines
        raise RuntimeError("Stockfish exited before returning " + expected)

    def evaluate(self, fen: str) -> tuple[float, bool]:
        self._send(f"position fen {fen}")
        self._send(f"go depth {self.depth}")
        lines = self._wait_for("bestmove")
        score: float | None = None
        is_mate = False
        for line in lines:
            if " score cp " in line:
                score = float(line.split(" score cp ", 1)[1].split()[0])
            elif " score mate " in line:
                mate = int(line.split(" score mate ", 1)[1].split()[0])
                score = 100_000.0 if mate > 0 else -100_000.0
                is_mate = True
        if score is None:
            raise RuntimeError("Stockfish returned no evaluation")
        return score, is_mate

    def close(self) -> None:
        if self.process.poll() is None:
            self._send("quit")
            self.process.wait(timeout=5)


def features(board: chess.Board) -> np.ndarray:
    """The agent's own feature vector, so the fitted weights land on what the engine evaluates.

    This deliberately calls into the agent rather than recomputing the features here. A second
    copy drifts, and a weight fitted to a feature the engine does not compute is worse than no
    tuning at all.
    """
    return np.array(agent._features(board), dtype=np.float64)


# Stockfish reports centipawns, where a pawn is 100, and PIECE_VALUE already counts a pawn as
# 100. So the anchor is 1.0, not the agent's current MATERIAL_WEIGHT: the agent's internal scale
# happens to run at twice centipawns, and anchoring to it would force every other feature to
# absorb the factor of two. Nothing downstream cares, because PAWN_UNITS and every margin in
# agent.py derive from MATERIAL_WEIGHT rather than assuming a fixed scale.
MATERIAL_ANCHOR = 1.0

def fit(matrix: np.ndarray, labels: np.ndarray, ridge: float, anchor_material: bool) -> np.ndarray:
    """Least squares, optionally ridge-penalised and optionally with material held fixed.

    The penalty is applied to standardised columns so that it falls evenly on features whose
    raw scales differ by three orders of magnitude, and the result is mapped back to raw units.
    The intercept is never penalised.
    """
    material_index = 1
    targets = labels
    if anchor_material:
        # Fit only what material does not already explain.
        targets = labels - MATERIAL_ANCHOR * matrix[:, material_index]
        matrix = matrix.copy()
        matrix[:, material_index] = 0.0

    if ridge <= 0.0:
        weights, _, _, _ = np.linalg.lstsq(matrix, targets, rcond=None)
    else:
        columns = matrix[:, :-1]
        centre = columns.mean(axis=0)
        spread = columns.std(axis=0)
        spread[spread < 1e-9] = 1.0
        scaled = np.column_stack([(columns - centre) / spread, np.ones(len(columns))])
        penalty = np.eye(scaled.shape[1]) * ridge
        penalty[-1, -1] = 0.0
        scaled_weights = np.linalg.solve(scaled.T @ scaled + penalty, scaled.T @ targets)
        raw = scaled_weights[:-1] / spread
        intercept = scaled_weights[-1] - float(raw @ centre)
        weights = np.append(raw, intercept)

    if anchor_material:
        weights[material_index] = MATERIAL_ANCHOR
    return weights


def sample_from_pgn(
    paths: list[Path], count: int, seed: int, skip_opening: int
) -> list[chess.Board]:
    """Positions taken from real games: any PGN, including engine games or AlphaZero records.

    Real games are what the weights have to generalise to. Positions still in the opening are
    skipped because they are near-identical across games and would dominate the fit, and
    positions in check are skipped because their evaluation is dominated by the forced reply
    rather than by the features being fitted.
    """
    rng = random.Random(seed)
    positions: list[chess.Board] = []
    for path in paths:
        with path.open(encoding="utf-8", errors="replace") as handle:
            while len(positions) < count:
                game = chess.pgn.read_game(handle)
                if game is None:
                    break
                board = game.board()
                for ply, move in enumerate(game.mainline_moves()):
                    board.push(move)
                    if ply < skip_opening or board.is_check():
                        continue
                    if board.is_game_over(claim_draw=False):
                        continue
                    # Thin the sample so one long game cannot dominate the fit.
                    if rng.random() < 0.25:
                        positions.append(board.copy(stack=False))
        if len(positions) >= count:
            break
    rng.shuffle(positions)
    return positions[:count]


def sample_positions(count: int, seed: int) -> list[chess.Board]:
    rng = random.Random(seed)
    positions: list[chess.Board] = []
    while len(positions) < count:
        board = chess.Board()
        for _ in range(rng.randint(8, 45)):
            moves = list(board.legal_moves)
            if not moves:
                break
            board.push(rng.choice(moves))
        if not board.is_game_over(claim_draw=False):
            positions.append(board)
    return positions


def label_positions(
    engine: Stockfish, boards: list[chess.Board]
) -> tuple[np.ndarray, np.ndarray, int]:
    feature_rows: list[np.ndarray] = []
    labels: list[float] = []
    mate_count = 0
    for board in boards:
        label, is_mate = engine.evaluate(board.fen())
        if is_mate:
            mate_count += 1
            continue
        feature_rows.append(features(board))
        perspective = 1.0 if board.turn == chess.WHITE else -1.0
        labels.append(float(np.clip(label * perspective, -2000.0, 2000.0)))
    return np.vstack(feature_rows), np.array(labels), mate_count


def calculate_rmse(predictions: np.ndarray, labels: np.ndarray) -> float:
    return float(np.sqrt(np.mean((predictions - labels) ** 2)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stockfish", type=Path, required=True)
    parser.add_argument("--positions", type=int, default=100)
    parser.add_argument("--depth", type=int, default=8)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--pgn",
        type=Path,
        action="append",
        default=[],
        help="sample positions from these PGN files instead of from random play",
    )
    parser.add_argument("--skip-opening", type=int, default=12)
    parser.add_argument(
        "--ridge",
        type=float,
        default=0.0,
        help="L2 penalty. The features overlap heavily, so plain least squares hands out large "
        "cancelling coefficients; a penalty shrinks them towards a usable set",
    )
    parser.add_argument(
        "--anchor-material",
        action="store_true",
        help="hold MATERIAL_WEIGHT at the agent's current value and fit the rest as corrections, "
        "since piece values are the one part of the evaluation already known to be right",
    )
    arguments = parser.parse_args()

    validation_count = max(100, arguments.positions // 5)
    if arguments.pgn:
        pool = sample_from_pgn(
            arguments.pgn,
            arguments.positions + validation_count,
            arguments.seed,
            arguments.skip_opening,
        )
        if len(pool) < arguments.positions + validation_count:
            print(f"note: the PGNs yielded only {len(pool)} usable positions")
        training_boards = pool[: arguments.positions]
        validation_boards = pool[arguments.positions :]
        print(f"sampled {len(training_boards)} training and {len(validation_boards)} validation "
              f"positions from {len(arguments.pgn)} PGN file(s)")
    else:
        training_boards = sample_positions(arguments.positions, arguments.seed)
        validation_boards = sample_positions(validation_count, arguments.seed + 1)
    engine = Stockfish(arguments.stockfish, arguments.depth)
    try:
        training_matrix, training_labels, training_mates = label_positions(engine, training_boards)
        validation_matrix, validation_labels, validation_mates = label_positions(
            engine, validation_boards
        )
    finally:
        engine.close()

    training_matrix = np.column_stack([training_matrix, np.ones(len(training_matrix))])
    validation_matrix = np.column_stack([validation_matrix, np.ones(len(validation_matrix))])
    weights = fit(
        training_matrix, training_labels, arguments.ridge, arguments.anchor_material
    )
    training_error = calculate_rmse(training_matrix @ weights, training_labels)
    validation_error = calculate_rmse(validation_matrix @ weights, validation_labels)

    names = [
        "MOBILITY_WEIGHT",
        "MATERIAL_WEIGHT",
        "KING_SAFETY_WEIGHT",
        "CENTER_WEIGHT",
        "ATTACK_WEIGHT",
        "HANGING_PIECE_WEIGHT",
        "PAWN_STRUCTURE_WEIGHT",
        "PASSED_PAWN_WEIGHT",
        "THREAT_WEIGHT",
        "SPACE_WEIGHT",
        "KING_PRESSURE_WEIGHT",
        "STORM_PROGRESS_WEIGHT",
        "KING_FILE_WEIGHT",
    ]
    print(
        f"trained on {len(training_labels)} positions, validated on {len(validation_labels)}; "
        f"Stockfish depth {arguments.depth}"
    )
    print(f"discarded forced-mate positions: {training_mates} train, {validation_mates} validation")
    print(f"training RMSE: {training_error:.1f} centipawns")
    print(f"validation RMSE: {validation_error:.1f} centipawns")
    for name, value in zip(names, weights[:-1], strict=True):
        print(f"{name} = {value:.6f}")
    print(f"EVALUATION_BIAS = {weights[-1]:.6f}")


if __name__ == "__main__":
    main()
