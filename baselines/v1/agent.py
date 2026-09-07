"""The submission entrypoint. The platform imports this file and calls get_move."""

from __future__ import annotations

import math
import time

import chess

PIECE_VALUE = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}
CENTER_SQUARES = {
    19,
    20,
    27,
    28,
    35,
    36,
    43,
    44,
}

# Priority order requested by the user:
# 1. mobility
# 2. material
# 3. king safety
# 4. center control
MOBILITY_WEIGHT = 3.0
MATERIAL_WEIGHT = 2.0
KING_SAFETY_WEIGHT = 1.5
CENTER_WEIGHT = 1.0
PASSED_PAWN_WEIGHT = 18.0
HANGING_PIECE_WEIGHT = 35.0
PAWN_STRUCTURE_WEIGHT = 12.0
THREAT_WEIGHT = 20.0
SPACE_WEIGHT = 3.0

MATE_SCORE = 1_000_000.0
MAX_SEARCH_DEPTH = 3


class SearchTimeout(Exception):
    pass


def _mobility_for_color(board: chess.Board, color: chess.Color) -> int:
    board_copy = board.copy()
    board_copy.turn = color
    return len(list(board_copy.legal_moves))


def _center_score(board: chess.Board) -> float:
    score = 0.0
    for square, piece in board.piece_map().items():
        if square not in CENTER_SQUARES:
            continue
        if piece.color == chess.WHITE:
            score += 1.0
        else:
            score -= 1.0
    return score


def _king_safety_score(board: chess.Board) -> float:
    score = 0.0
    for color in (chess.WHITE, chess.BLACK):
        king_square = next(
            (
                square
                for square, piece in board.piece_map().items()
                if piece.color == color and piece.piece_type == chess.KING
            ),
            None,
        )
        if king_square is None:
            continue

        shield = 0.0
        for square, piece in board.piece_map().items():
            if piece.color != color:
                continue
            if (
                abs(chess.square_file(square) - chess.square_file(king_square)) <= 1
                and abs(chess.square_rank(square) - chess.square_rank(king_square)) <= 1
            ):
                shield += 0.75
                if piece.piece_type == chess.PAWN:
                    shield += 0.25

        if color == chess.WHITE:
            score += shield
        else:
            score -= shield
    return score


def _count_attacks(board: chess.Board, color: chess.Color) -> int:
    return sum(board.is_attacked_by(color, square) for square in chess.SQUARES)


def _hanging_pieces(board: chess.Board, color: chess.Color) -> int:
    count = 0
    for square, piece in board.piece_map().items():
        if (
            piece.color == color
            and piece.piece_type != chess.KING
            and board.is_attacked_by(not color, square)
            and not board.attackers(color, square)
        ):
            count += 1
    return count


def _pawn_structure(board: chess.Board, color: chess.Color) -> tuple[int, int]:
    pawns = list(board.pieces(chess.PAWN, color))
    files = [chess.square_file(square) for square in pawns]
    doubled = sum(files.count(file) - 1 for file in set(files) if files.count(file) > 1)
    isolated = 0
    for file in set(files):
        if not any(abs(file - other_file) == 1 for other_file in set(files)):
            isolated += files.count(file)
    return doubled, isolated


def _passed_pawns(board: chess.Board, color: chess.Color) -> int:
    own_pawns = board.pieces(chess.PAWN, color)
    enemy_pawns = board.pieces(chess.PAWN, not color)
    count = 0
    for square in own_pawns:
        file = chess.square_file(square)
        rank = chess.square_rank(square)
        blocked = False
        for enemy_square in enemy_pawns:
            enemy_file = chess.square_file(enemy_square)
            enemy_rank = chess.square_rank(enemy_square)
            ahead = enemy_rank > rank if color == chess.WHITE else enemy_rank < rank
            if abs(enemy_file - file) <= 1 and ahead:
                blocked = True
                break
        if not blocked:
            count += 1
    return count


def _space_score(board: chess.Board) -> float:
    score = 0.0
    for square, piece in board.piece_map().items():
        if piece.piece_type in (chess.PAWN, chess.KING):
            continue
        rank = chess.square_rank(square)
        in_enemy_half = rank >= 4 if piece.color == chess.WHITE else rank <= 3
        if in_enemy_half:
            score += 1.0 if piece.color == chess.WHITE else -1.0
    return score


def _white_evaluation(board: chess.Board) -> float:
    material = 0.0
    for _, piece in board.piece_map().items():
        value = PIECE_VALUE[piece.piece_type]
        if piece.color == chess.WHITE:
            material += value
        else:
            material -= value

    mobility = _mobility_for_color(board, chess.WHITE) - _mobility_for_color(board, chess.BLACK)
    king_safety = _king_safety_score(board)
    center = _center_score(board)
    attacks = _count_attacks(board, chess.WHITE) - _count_attacks(board, chess.BLACK)
    hanging = _hanging_pieces(board, chess.BLACK) - _hanging_pieces(board, chess.WHITE)
    white_doubled, white_isolated = _pawn_structure(board, chess.WHITE)
    black_doubled, black_isolated = _pawn_structure(board, chess.BLACK)
    pawn_structure = (black_doubled + black_isolated) - (white_doubled + white_isolated)
    passed_pawns = _passed_pawns(board, chess.WHITE) - _passed_pawns(board, chess.BLACK)
    threats = 0
    for square, piece in board.piece_map().items():
        if piece.piece_type == chess.KING:
            continue
        attackers = board.attackers(not piece.color, square)
        if attackers and not board.attackers(piece.color, square):
            threats += 1 if piece.color == chess.BLACK else -1

    return (
        MOBILITY_WEIGHT * mobility
        + MATERIAL_WEIGHT * material
        + KING_SAFETY_WEIGHT * king_safety
        + CENTER_WEIGHT * center
        + 0.5 * attacks
        + HANGING_PIECE_WEIGHT * hanging
        + PAWN_STRUCTURE_WEIGHT * pawn_structure
        + PASSED_PAWN_WEIGHT * passed_pawns
        + THREAT_WEIGHT * threats
        + SPACE_WEIGHT * _space_score(board)
    )


def evaluate(board: chess.Board) -> float:
    """Evaluate from the perspective of the side whose turn it is."""
    white_score = _white_evaluation(board)
    return white_score if board.turn == chess.WHITE else -white_score


def _move_order_key(move: chess.Move, board: chess.Board) -> tuple[int, int, int]:
    score = 0
    if board.is_capture(move):
        score += 100
    if move.promotion:
        score += 50
    if board.gives_check(move):
        score += 25
    if move.to_square in chess.SquareSet(chess.BB_CENTER):
        score += 10
    return (score, 0, 0)


def _ordered_moves(board: chess.Board) -> list[chess.Move]:
    return sorted(board.legal_moves, key=lambda move: _move_order_key(move, board), reverse=True)


def negamax(board: chess.Board, depth: int, alpha: float, beta: float, deadline: float) -> float:
    if time.monotonic() >= deadline:
        raise SearchTimeout

    if board.is_checkmate():
        return -MATE_SCORE + (MAX_SEARCH_DEPTH - depth)
    if board.is_stalemate() or board.is_insufficient_material() or board.is_repetition(3):
        return 0.0
    if depth == 0:
        return evaluate(board)

    legal_moves = _ordered_moves(board)
    if not legal_moves:
        return 0.0

    best_score = -math.inf
    for move in legal_moves:
        board.push(move)
        score = -negamax(board, depth - 1, -beta, -alpha, deadline)
        board.pop()

        if score > best_score:
            best_score = score
        if score > alpha:
            alpha = score
        if alpha >= beta:
            break

    return best_score


def get_move(fen: str, time_left_ms: int) -> str:
    """Return a legal move in UCI notation.

    This version adds a depth-limited negamax search on top of the static evaluator, plus
    alpha-beta pruning and basic move ordering. It remains simple and contest-safe.
    """
    board = chess.Board(fen)
    legal_moves = list(_ordered_moves(board))
    if not legal_moves:
        raise ValueError(f"No legal move available for position: {fen}")

    budget_seconds = min(5.0, max(0.05, time_left_ms / 1000.0 * 0.03))
    deadline = time.monotonic() + budget_seconds
    best_move = legal_moves[0]

    for depth in range(1, MAX_SEARCH_DEPTH + 1):
        completed_move = best_move
        completed_score = -math.inf
        alpha = -math.inf
        try:
            for move in legal_moves:
                board.push(move)
                score = -negamax(board, depth - 1, -math.inf, -alpha, deadline)
                board.pop()
                if score > completed_score:
                    completed_score = score
                    completed_move = move
                alpha = max(alpha, score)
        except SearchTimeout:
            break
        best_move = completed_move

    return best_move.uci()
