"""The submission entrypoint. The platform imports this file and calls get_move.

The engine is a plain alpha-beta searcher with the pieces that make depth pay for itself in
Python: a transposition table, a quiescence search, staged move ordering, a bitboard evaluator
whose features are computed in one pass and cached, and a time manager that spends the clock in
proportion to how much of the game is left.
"""

from __future__ import annotations

import math
import operator
import os
import time
from collections.abc import Hashable

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
ATTACK_WEIGHT = 0.5
PASSED_PAWN_WEIGHT = 18.0
HANGING_PIECE_WEIGHT = 35.0
PAWN_STRUCTURE_WEIGHT = 12.0
THREAT_WEIGHT = 20.0
SPACE_WEIGHT = 3.0
# Wing attack terms. Starting values are deliberately modest: they are hand-set rather than
# fitted, because fitting the ten older features against Stockfish reached only parity with
# these hand-picked ones, so the fit is not a trustworthy source of a starting point.
KING_PRESSURE_WEIGHT = 8.0
STORM_PROGRESS_WEIGHT = 6.0
KING_FILE_WEIGHT = 15.0

# Same order as _features, and as the names tools/tune_weights.py prints.
FEATURE_WEIGHTS = (
    MOBILITY_WEIGHT,
    MATERIAL_WEIGHT,
    KING_SAFETY_WEIGHT,
    CENTER_WEIGHT,
    ATTACK_WEIGHT,
    HANGING_PIECE_WEIGHT,
    PAWN_STRUCTURE_WEIGHT,
    PASSED_PAWN_WEIGHT,
    THREAT_WEIGHT,
    SPACE_WEIGHT,
    KING_PRESSURE_WEIGHT,
    STORM_PROGRESS_WEIGHT,
    KING_FILE_WEIGHT,
)

MATE_SCORE = 1_000_000.0
MATE_THRESHOLD = MATE_SCORE - 1_000.0
# One pawn is MATERIAL_WEIGHT * 100 = 200 evaluation units. The margins below are in those units.
PAWN_UNITS = MATERIAL_WEIGHT * PIECE_VALUE[chess.PAWN]
DELTA_MARGIN = 2.0 * PAWN_UNITS
# A draw is worth slightly less than nothing to the side that is thinking, so the search prefers a
# playable position to a repetition. Set this to 0.0 to score draws dead level.
CONTEMPT = 0.1 * PAWN_UNITS
# Once the opponent's king has committed to a wing, lean towards a pawn storm down that wing: h4,
# g4, a4, b4 and the like. The bonus only tips the choice between root moves the search already
# rates within this much of each other, so a real refutation still wins. Set to 0.0 to switch off.
FLANK_STORM_BONUS = 0.2 * PAWN_UNITS
# g and h against a king that went short, a and b against a king that went long.
KINGSIDE_STORM_FILES = frozenset({chess.square_file(chess.G1), chess.square_file(chess.H1)})
QUEENSIDE_STORM_FILES = frozenset({chess.square_file(chess.A1), chess.square_file(chess.B1)})
# A king on f, g or h has gone short; a king on a, b or c has gone long.
SHORT_WING_FILE = chess.square_file(chess.F1)
LONG_WING_FILE = chess.square_file(chess.C1)

# Once the search sees this much of an advantage there is little left to learn from another ply, so
# it stops deepening and banks the clock for a later move. Scores here are relative to the side
# thinking, so this one threshold covers +4 playing white and -4 playing black: both mean the agent
# is the one four pawns up. It deliberately does not fire when the agent is four pawns down, where
# the need is to search harder for a swindle, not to give up early.
#
# The minimum depth is the safeguard, and it has to be high. A huge score from a shallow search is
# often a mirage the next ply refutes. Worse, depth is what converts a won position: a floor of 4
# measured at -83 Elo over 30 games, because it cut the search short in won endgames and left the
# engine shuffling into a repetition instead of finding the mate.
DECISIVE_ADVANTAGE = 4.0 * PAWN_UNITS
DECISIVE_MIN_DEPTH = 6

MAX_PLY = 64
# Iterative deepening stops when the clock says so, so this is only a ceiling. It is set above what
# a middlegame can afford because an endgame reaches depth 7 in well under a second, and converting
# a won endgame is exactly where the extra plies pay.
MAX_SEARCH_DEPTH = int(os.environ.get("CHESSATHON_MAX_DEPTH", "8"))
DEBUG = os.environ.get("CHESSATHON_DEBUG") == "1"

# Time control, from https://aichessathon.com/docs/rules.md. This is the increment the rules
# quote; _observe_increment measures the real one, because a control that pays less than this
# would otherwise walk the clock down to a flag.
INCREMENT_S = 0.5
INCREMENT_SHARE = 0.6
EXPECTED_GAME_MOVES = 44
MIN_MOVES_TO_GO = 16
# Never bet more than this share of the remaining clock on one move, and always hand back enough
# for the reply to travel and for one last evaluation to finish.
MAX_CLOCK_SHARE = 0.35
CLOCK_RESERVE_S = 0.15
# A new iteration costs several times the last one, so do not start one past this much of the plan.
NEXT_ITERATION_SHARE = 0.45
# Nodes between clock readings. Most nodes cost about 50 us, but a check evasion in the quiescence
# search can cost four times that, so 64 keeps the worst overshoot near 10 ms. time.monotonic is
# about 50 ns, so reading it this often costs well under a tenth of a percent.
CLOCK_CHECK_MASK = 63

TT_EXACT, TT_LOWER, TT_UPPER = 0, 1, 2
# The tables below are the only state that outlives a move. The platform gives the agent one
# process per game, so they stay warm for the next move and never leak into another game.
TT_MAX_ENTRIES = 250_000
EVAL_CACHE_MAX_ENTRIES = 150_000
HISTORY_MAX = 1 << 15

TT_MOVE_BONUS = 1 << 30
GOOD_CAPTURE_BONUS = 1 << 24
KILLER_BONUS = 1 << 20
BAD_CAPTURE_BONUS = 1 << 16

_KING_ZONE = [chess.BB_KING_ATTACKS[square] | chess.BB_SQUARES[square] for square in chess.SQUARES]
_CENTER_MASK = sum(chess.BB_SQUARES[square] for square in CENTER_SQUARES)
_WHITE_HALF = chess.BB_RANK_5 | chess.BB_RANK_6 | chess.BB_RANK_7 | chess.BB_RANK_8
_BLACK_HALF = chess.BB_RANK_1 | chess.BB_RANK_2 | chess.BB_RANK_3 | chess.BB_RANK_4


def _passed_pawn_spans() -> list[list[int]]:
    """For each colour and square, the squares an enemy pawn has to hold to stop a passer."""
    spans: list[list[int]] = [[0] * 64, [0] * 64]
    for square in chess.SQUARES:
        file = chess.square_file(square)
        rank = chess.square_rank(square)
        files = sum(chess.BB_FILES[other] for other in range(8) if abs(other - file) <= 1)
        ahead_white = sum(chess.BB_RANKS[other] for other in range(rank + 1, 8))
        ahead_black = sum(chess.BB_RANKS[other] for other in range(rank))
        spans[chess.WHITE][square] = files & ahead_white
        spans[chess.BLACK][square] = files & ahead_black
    return spans


# Pressure on a king counts by attacker: a queue of pawns is not a queen. Indexed by piece type,
# with the king itself contributing nothing.
_ATTACKER_WEIGHT = [0.0] * 7
_ATTACKER_WEIGHT[chess.PAWN] = 1.0
_ATTACKER_WEIGHT[chess.KNIGHT] = 2.0
_ATTACKER_WEIGHT[chess.BISHOP] = 2.0
_ATTACKER_WEIGHT[chess.ROOK] = 3.0
_ATTACKER_WEIGHT[chess.QUEEN] = 5.0

_PASSED_SPAN = _passed_pawn_spans()
_first = operator.itemgetter(0)
_second = operator.itemgetter(1)

_transposition_table: dict[Hashable, tuple[int, float, int, chess.Move | None]] = {}
_evaluation_cache: dict[Hashable, float] = {}
_killers: list[list[chess.Move | None]] = [[None, None] for _ in range(MAX_PLY + 1)]
_history: dict[tuple[chess.Color, int, int], int] = {}
_path: dict[Hashable, int] = {}
_game_history: dict[Hashable, int] = {}
_piece_count = 33
_root_turn = chess.WHITE
_nodes = 0
_increment_s = INCREMENT_S
_last_clock_ms: int | None = None
_last_elapsed_s = 0.0


class SearchTimeout(Exception):
    pass


def _key(board: chess.Board) -> Hashable:
    """The position's identity for the tables: pieces, side, castling rights and en passant.

    python-chess builds this tuple in constant time, which makes it about twenty times cheaper
    here than hashing the position with chess.polyglot.
    """
    return board._transposition_key()


def _attack_summary(
    board: chess.Board, color: chess.Color, target: int = 0
) -> tuple[int, int, float]:
    """Return the squares this side attacks, its pseudo-mobility, and its pressure on `target`.

    One pass over the side's pieces feeds four features that would otherwise each walk the
    pieces again: mobility, the attacked-square count, the hanging-piece test, and how hard
    this side leans on the square set in `target`, normally the enemy king's zone. Pressure is
    weighted by attacker, because a queen bearing down on a king is not a pawn doing it.
    """
    own = board.occupied_co[color]
    pawns = own & board.pawns
    attacked = 0
    mobility = 0
    pressure = 0.0
    # One pass, as before. Working out which piece is on the square is only worth doing for the
    # few that actually reach the target, so the piece lookup sits behind that test.
    for square in chess.scan_forward(own & ~pawns):
        attacks = board.attacks_mask(square)
        attacked |= attacks
        mobility += chess.popcount(attacks & ~own)
        if attacks & target:
            pressure += _ATTACKER_WEIGHT[board.piece_type_at(square) or 0]
    for square in chess.scan_forward(pawns):
        attacks = board.attacks_mask(square)
        attacked |= attacks
        if attacks & target:
            pressure += _ATTACKER_WEIGHT[chess.PAWN]
    pushes = (pawns << 8) if color == chess.WHITE else (pawns >> 8)
    mobility += chess.popcount(pushes & ~board.occupied & chess.BB_ALL)
    return attacked, mobility, pressure


def _mobility_for_color(board: chess.Board, color: chess.Color) -> int:
    return _attack_summary(board, color)[1]


def _count_attacks(board: chess.Board, color: chess.Color) -> int:
    return chess.popcount(_attack_summary(board, color)[0])


def _king_zone(board: chess.Board, color: chess.Color) -> int:
    """The squares around this side's king, or nothing when there is no king on the board."""
    king = board.king(color)
    return _KING_ZONE[king] if king is not None else 0


def _material(board: chess.Board) -> float:
    white = board.occupied_co[chess.WHITE]
    black = board.occupied_co[chess.BLACK]
    total = 0.0
    for mask, value in (
        (board.pawns, PIECE_VALUE[chess.PAWN]),
        (board.knights, PIECE_VALUE[chess.KNIGHT]),
        (board.bishops, PIECE_VALUE[chess.BISHOP]),
        (board.rooks, PIECE_VALUE[chess.ROOK]),
        (board.queens, PIECE_VALUE[chess.QUEEN]),
    ):
        total += value * (chess.popcount(mask & white) - chess.popcount(mask & black))
    return total


def _center_score(board: chess.Board) -> float:
    white = chess.popcount(board.occupied_co[chess.WHITE] & _CENTER_MASK)
    black = chess.popcount(board.occupied_co[chess.BLACK] & _CENTER_MASK)
    return float(white - black)


def _king_safety_score(board: chess.Board) -> float:
    score = 0.0
    for color in (chess.WHITE, chess.BLACK):
        king = board.king(color)
        if king is None:
            continue
        zone = _KING_ZONE[king]
        own = board.occupied_co[color]
        shield = 0.75 * chess.popcount(own & zone)
        shield += 0.25 * chess.popcount(own & board.pawns & zone)
        score += shield if color == chess.WHITE else -shield
    return score


def _hanging_mask(board: chess.Board, color: chess.Color, attacked: int, defended: int) -> int:
    """This side's non-king pieces that the opponent attacks and this side does not defend."""
    return board.occupied_co[color] & ~board.kings & attacked & ~defended


def _hanging_pieces(board: chess.Board, color: chess.Color) -> int:
    defended, _, _ = _attack_summary(board, color)
    attacked, _, _ = _attack_summary(board, not color)
    return chess.popcount(_hanging_mask(board, color, attacked, defended))


def _pawn_structure(board: chess.Board, color: chess.Color) -> tuple[int, int]:
    pawns = board.pawns & board.occupied_co[color]
    counts = [chess.popcount(pawns & chess.BB_FILES[file]) for file in range(8)]
    doubled = sum(count - 1 for count in counts if count > 1)
    isolated = 0
    for file, count in enumerate(counts):
        if not count:
            continue
        left = counts[file - 1] if file > 0 else 0
        right = counts[file + 1] if file < 7 else 0
        if not left and not right:
            isolated += count
    return doubled, isolated


def _passed_pawns(board: chess.Board, color: chess.Color) -> int:
    enemy_pawns = board.pawns & board.occupied_co[not color]
    spans = _PASSED_SPAN[color]
    count = 0
    for square in chess.scan_forward(board.pawns & board.occupied_co[color]):
        if not enemy_pawns & spans[square]:
            count += 1
    return count


def _space_score(board: chess.Board) -> float:
    pieces = board.occupied & ~board.pawns & ~board.kings
    white = chess.popcount(pieces & board.occupied_co[chess.WHITE] & _WHITE_HALF)
    black = chess.popcount(pieces & board.occupied_co[chess.BLACK] & _BLACK_HALF)
    return float(white - black)


def _features(board: chess.Board) -> tuple[float, ...]:
    """The static features, in the order FEATURE_WEIGHTS lists them, from white's point of view."""
    white_zone = _king_zone(board, chess.WHITE)
    black_zone = _king_zone(board, chess.BLACK)
    white_attacks, white_mobility, white_pressure = _attack_summary(board, chess.WHITE, black_zone)
    black_attacks, black_mobility, black_pressure = _attack_summary(board, chess.BLACK, white_zone)

    white_doubled, white_isolated = _pawn_structure(board, chess.WHITE)
    black_doubled, black_isolated = _pawn_structure(board, chess.BLACK)
    hanging = chess.popcount(
        _hanging_mask(board, chess.BLACK, white_attacks, black_attacks)
    ) - chess.popcount(_hanging_mask(board, chess.WHITE, black_attacks, white_attacks))

    return (
        float(white_mobility - black_mobility),
        _material(board),
        _king_safety_score(board),
        _center_score(board),
        float(chess.popcount(white_attacks) - chess.popcount(black_attacks)),
        float(hanging),
        float((black_doubled + black_isolated) - (white_doubled + white_isolated)),
        float(_passed_pawns(board, chess.WHITE) - _passed_pawns(board, chess.BLACK)),
        # The previous evaluator's "threats" term counted exactly the hanging pieces a second
        # time, under a second weight. Keeping both entries keeps the tuned weights meaningful.
        float(hanging),
        _space_score(board),
        # Wing-aware terms. The evaluator had no notion of enemy pieces bearing down on a king,
        # only of friendly pieces standing near one, so a king under four attackers scored the
        # same as one in total safety.
        white_pressure - black_pressure,
        _storm_progress(board, chess.WHITE) - _storm_progress(board, chess.BLACK),
        _king_file_pressure(board, chess.WHITE) - _king_file_pressure(board, chess.BLACK),
    )


def _white_evaluation(board: chess.Board) -> float:
    features = _features(board)
    return sum(weight * feature for weight, feature in zip(FEATURE_WEIGHTS, features, strict=True))


def evaluate(board: chess.Board) -> float:
    """Evaluate from the perspective of the side whose turn it is."""
    white_score = _white_evaluation(board)
    return white_score if board.turn == chess.WHITE else -white_score


def _evaluate_cached(board: chess.Board, key: Hashable) -> float:
    score = _evaluation_cache.get(key)
    if score is None:
        score = evaluate(board)
        if len(_evaluation_cache) >= EVAL_CACHE_MAX_ENTRIES:
            _evaluation_cache.clear()
        _evaluation_cache[key] = score
    return score


def _capture_victim(board: chess.Board, move: chess.Move) -> int | None:
    """The value of the piece a move captures, or None when the move captures nothing."""
    captured = board.piece_type_at(move.to_square)
    if captured is not None:
        return PIECE_VALUE[captured]
    if move.to_square == board.ep_square and board.pawns & chess.BB_SQUARES[move.from_square]:
        return PIECE_VALUE[chess.PAWN]
    return None


def _capture_gain(board: chess.Board, move: chess.Move, victim: int) -> int:
    """What a capture wins in material if the opponent recaptures with anything.

    This is a one-ply stand-in for a full static exchange evaluation: cheap enough to run on
    every capture, and right often enough to sort queen-takes-defended-pawn to the back.
    """
    gain = victim
    if move.promotion is not None:
        gain += PIECE_VALUE[move.promotion] - PIECE_VALUE[chess.PAWN]
    attacker = board.piece_type_at(move.from_square)
    if attacker is not None and board.is_attacked_by(not board.turn, move.to_square):
        gain -= PIECE_VALUE[attacker]
    return gain


def _ordered_moves(board: chess.Board, tt_move: chess.Move | None, ply: int) -> list[chess.Move]:
    """Every legal move, best guess first: table move, good captures, killers, then history."""
    killers = _killers[ply] if ply <= MAX_PLY else [None, None]
    turn = board.turn
    scored: list[tuple[int, chess.Move]] = []
    for move in board.legal_moves:
        if move == tt_move:
            scored.append((TT_MOVE_BONUS, move))
            continue
        victim = _capture_victim(board, move)
        if victim is not None or move.promotion is not None:
            gain = _capture_gain(board, move, victim or 0)
            base = GOOD_CAPTURE_BONUS if gain >= 0 else BAD_CAPTURE_BONUS
            scored.append((base + gain, move))
        elif move in killers:
            scored.append((KILLER_BONUS, move))
        else:
            scored.append((_history.get((turn, move.from_square, move.to_square), 0), move))
    scored.sort(key=_first, reverse=True)
    return [move for _, move in scored]


def _tactical_moves(board: chess.Board) -> list[tuple[chess.Move, int]]:
    """Captures worth a look and queen promotions, each with the material it can win."""
    scored: list[tuple[int, chess.Move, int]] = []
    for move in board.generate_legal_captures():
        victim = _capture_victim(board, move)
        if victim is None:
            continue
        gain = _capture_gain(board, move, victim)
        if gain < 0:
            continue
        attacker = board.piece_type_at(move.from_square)
        order = victim * 16 - (PIECE_VALUE[attacker] if attacker is not None else 0)
        scored.append((order, move, gain))

    promotion_rank = chess.BB_RANK_7 if board.turn == chess.WHITE else chess.BB_RANK_2
    candidates = board.pawns & board.occupied_co[board.turn] & promotion_rank
    if candidates:
        for move in board.generate_legal_moves(candidates, ~board.occupied):
            if move.promotion == chess.QUEEN:
                gain = PIECE_VALUE[chess.QUEEN] - PIECE_VALUE[chess.PAWN]
                scored.append((gain * 16, move, gain))

    scored.sort(key=_first, reverse=True)
    return [(move, gain) for _, move, gain in scored]


def _wing_files(board: chess.Board, color: chess.Color) -> frozenset[int]:
    """The files of the wing this side's king has committed to, empty if it has not.

    Castling is the usual way a king commits, and it is visible in the position rather than in
    the move that produced it: the king sits on the wing and the rights are gone. A king that
    walked to the same square is just as good a target, so the test looks at where it is.
    """
    king = board.king(color)
    if king is None or board.has_castling_rights(color):
        return frozenset()
    file = chess.square_file(king)
    if file >= SHORT_WING_FILE:
        return KINGSIDE_STORM_FILES
    if file <= LONG_WING_FILE:
        return QUEENSIDE_STORM_FILES
    return frozenset()


def _storm_files(board: chess.Board) -> frozenset[int]:
    """The files worth pushing pawns down at the root, given where the opponent's king sits."""
    if not FLANK_STORM_BONUS:
        return frozenset()
    return _wing_files(board, not board.turn)


def _storm_progress(board: chess.Board, color: chess.Color) -> float:
    """How far this side's pawns have advanced down the wing the enemy king committed to.

    Each pawn counts by how far up the board it stands, so h4 is worth more than h3 and h5 more
    than h4. This is the flank storm idea as evaluation knowledge rather than a root tie-break,
    so the search can see a storm coming several plies out instead of only when choosing a move.
    """
    files = _wing_files(board, not color)
    if not files:
        return 0.0
    mask = 0
    for file in files:
        mask |= chess.BB_FILES[file]
    total = 0.0
    for square in chess.scan_forward(board.pawns & board.occupied_co[color] & mask):
        rank = chess.square_rank(square)
        advanced = (rank - 1) if color == chess.WHITE else (6 - rank)
        if advanced > 0:
            total += float(advanced)
    return total


def _king_file_pressure(board: chess.Board, color: chess.Color) -> float:
    """This side's rooks and queens on a pawnless file aimed at the enemy king's file.

    A heavy piece on an open file next to the enemy king is the other half of a wing attack: the
    pawns pry the file open and these are what come through it.
    """
    king = board.king(not color)
    if king is None:
        return 0.0
    king_file = chess.square_file(king)
    own = board.occupied_co[color]
    own_pawns = board.pawns & own
    total = 0.0
    for square in chess.scan_forward((board.rooks | board.queens) & own):
        file = chess.square_file(square)
        if abs(file - king_file) <= 1 and not own_pawns & chess.BB_FILES[file]:
            total += 1.0
    return total


def _is_storm_move(board: chess.Board, move: chess.Move, files: frozenset[int]) -> bool:
    """True for one of our own pawn pushes down a storm file, promotions aside."""
    if move.promotion is not None:
        return False
    if not board.pawns & board.occupied_co[board.turn] & chess.BB_SQUARES[move.from_square]:
        return False
    # Same file from and to keeps this to pushes; a capture onto the file is not a storm.
    return (
        chess.square_file(move.from_square) == chess.square_file(move.to_square)
        and chess.square_file(move.to_square) in files
    )


def _draw_score(board: chess.Board) -> float:
    return -CONTEMPT if board.turn == _root_turn else CONTEMPT


def _is_drawn(board: chess.Board) -> bool:
    if board.halfmove_clock >= 100:
        return True
    return chess.popcount(board.occupied) <= 4 and board.is_insufficient_material()


def _is_repetition(key: Hashable) -> bool:
    """True when this position already stands on the search path or earlier in the game."""
    return key in _path or key in _game_history


def _remember_cutoff(board: chess.Board, move: chess.Move, depth: int, ply: int) -> None:
    """Quiet moves that caused a cutoff get tried earlier next time."""
    if move.promotion is not None or _capture_victim(board, move) is not None:
        return
    if ply <= MAX_PLY:
        killers = _killers[ply]
        if killers[0] != move:
            killers[1] = killers[0]
            killers[0] = move
    entry = (board.turn, move.from_square, move.to_square)
    _history[entry] = min(_history.get(entry, 0) + depth * depth, HISTORY_MAX)


def _store(
    key: Hashable, depth: int, ply: int, score: float, flag: int, move: chess.Move | None
) -> None:
    """Keep a bound for this position.

    Mate scores go in as a distance from this position rather than from the root, so the entry
    still means the same thing when the search reaches the position at another ply.
    """
    if score > MATE_THRESHOLD:
        score += ply
    elif score < -MATE_THRESHOLD:
        score -= ply
    existing = _transposition_table.get(key)
    if existing is not None and existing[0] > depth:
        return
    if len(_transposition_table) >= TT_MAX_ENTRIES:
        _transposition_table.clear()
    _transposition_table[key] = (depth, score, flag, move)


def _quiescence(board: chess.Board, alpha: float, beta: float, ply: int, deadline: float) -> float:
    """Search only forcing moves, so the evaluator never grades a half-finished trade."""
    global _nodes
    _nodes += 1
    if not _nodes & CLOCK_CHECK_MASK and time.monotonic() >= deadline:
        raise SearchTimeout

    key = _key(board)
    if _is_drawn(board) or _is_repetition(key):
        return _draw_score(board)
    if ply >= MAX_PLY:
        return _evaluate_cached(board, key)

    in_check = board.is_check()
    if in_check:
        # A side in check has no right to stand pat, so every legal reply gets searched.
        moves = [(move, 0) for move in _ordered_moves(board, None, ply)]
        if not moves:
            return -MATE_SCORE + ply
        best = -math.inf
    else:
        best = _evaluate_cached(board, key)
        if best >= beta:
            return best
        alpha = max(alpha, best)
        moves = _tactical_moves(board)

    _path[key] = 1
    try:
        for move, gain in moves:
            if not in_check and best + MATERIAL_WEIGHT * gain + DELTA_MARGIN < alpha:
                continue
            board.push(move)
            score = -_quiescence(board, -beta, -alpha, ply + 1, deadline)
            board.pop()
            if score > best:
                best = score
            if score > alpha:
                alpha = score
            if alpha >= beta:
                break
    finally:
        del _path[key]
    return best


def _negamax(
    board: chess.Board, depth: int, alpha: float, beta: float, ply: int, deadline: float
) -> float:
    global _nodes
    _nodes += 1
    if not _nodes & CLOCK_CHECK_MASK and time.monotonic() >= deadline:
        raise SearchTimeout

    key = _key(board)
    if _is_drawn(board) or _is_repetition(key):
        return _draw_score(board)

    alpha_at_entry = alpha
    tt_move: chess.Move | None = None
    entry = _transposition_table.get(key)
    if entry is not None:
        entry_depth, entry_score, entry_flag, tt_move = entry
        if entry_depth >= depth:
            score = entry_score
            if score > MATE_THRESHOLD:
                score -= ply
            elif score < -MATE_THRESHOLD:
                score += ply
            if entry_flag == TT_EXACT:
                return score
            if entry_flag == TT_LOWER:
                alpha = max(alpha, score)
            else:
                beta = min(beta, score)
            if alpha >= beta:
                return score

    if depth <= 0:
        return _quiescence(board, alpha, beta, ply, deadline)

    moves = _ordered_moves(board, tt_move, ply)
    if not moves:
        return -MATE_SCORE + ply if board.is_check() else _draw_score(board)

    best_score = -math.inf
    best_move = moves[0]
    _path[key] = 1
    try:
        for index, move in enumerate(moves):
            board.push(move)
            if index == 0:
                score = -_negamax(board, depth - 1, -beta, -alpha, ply + 1, deadline)
            else:
                # Assume the first move is best: refute the rest with a null window, and pay for
                # a full re-search only when one of them beats it.
                score = -_negamax(board, depth - 1, -alpha - 1.0, -alpha, ply + 1, deadline)
                if alpha < score < beta:
                    score = -_negamax(board, depth - 1, -beta, -alpha, ply + 1, deadline)
            board.pop()

            if score > best_score:
                best_score = score
                best_move = move
            if score > alpha:
                alpha = score
            if alpha >= beta:
                _remember_cutoff(board, move, depth, ply)
                break
    finally:
        del _path[key]

    if best_score <= alpha_at_entry:
        flag = TT_UPPER
    elif best_score >= beta:
        flag = TT_LOWER
    else:
        flag = TT_EXACT
    _store(key, depth, ply, best_score, flag, best_move)
    return best_score


def _search_root(
    board: chess.Board, depth: int, deadline: float, principal: chess.Move | None,
    files: frozenset[int],
) -> tuple[list[tuple[chess.Move, float]], bool]:
    """One iteration of iterative deepening, scoring every root move.

    The flag says whether the iteration ran to the end. A storm move is searched against a floor
    one bonus below the best score so far, because alpha-beta otherwise only proves "no better
    than the best", and an upper bound cannot tell a move a shade behind from a bad one. Beating
    that lower floor is all the storm move has to prove, so it skips the exact re-search unless
    it also beats the best outright: one null window is cheap, a re-search on every storm move
    is not.
    """
    moves = _ordered_moves(board, principal, 0)
    scored: list[tuple[chess.Move, float]] = []
    alpha = -math.inf
    for index, move in enumerate(moves):
        storm = bool(files) and alpha != -math.inf and _is_storm_move(board, move, files)
        floor = alpha - FLANK_STORM_BONUS if storm else alpha
        board.push(move)
        try:
            if index == 0:
                score = -_negamax(board, depth - 1, -math.inf, math.inf, 1, deadline)
            else:
                score = -_negamax(board, depth - 1, -floor - 1.0, -floor, 1, deadline)
                if score > (alpha if storm else floor):
                    score = -_negamax(board, depth - 1, -math.inf, -alpha, 1, deadline)
        except SearchTimeout:
            # The plies below did not unwind on the way out, so put the board back by hand.
            while board.move_stack:
                board.pop()
            return scored, False
        board.pop()

        scored.append((move, score))
        alpha = max(alpha, score)
    return scored, True


def _pick_root_move(
    board: chess.Board, scored: list[tuple[chess.Move, float]], files: frozenset[int]
) -> tuple[chess.Move, chess.Move, float]:
    """Choose what to play from the scored root moves.

    Returns the move to play, the highest-scoring move, and its score. The two differ only when
    the flank bonus tips the choice: the highest-scoring move still seeds the next iteration's
    move ordering, because that is the one the search actually rates first.

    A storm move's score is a bound rather than an exact figure, which is enough to prove it is
    within the bonus of the best but too coarse to rank two of them against each other. So the
    candidates are ranked by how far up the board they push, and the score only breaks ties.
    """
    principal, best_score = max(scored, key=_second)
    if not files or abs(best_score) > MATE_THRESHOLD:
        return principal, principal, best_score

    def rank(entry: tuple[chess.Move, float]) -> tuple[int, int, float]:
        move, score = entry
        within = score + FLANK_STORM_BONUS > best_score
        if not within or not _is_storm_move(board, move, files):
            return 0, 0, score
        pushed_to = chess.square_rank(move.to_square)
        return 1, pushed_to if board.turn == chess.WHITE else 7 - pushed_to, score

    chosen = max(scored, key=rank)[0]
    return chosen, principal, best_score


def _observe_increment(time_left_ms: int) -> None:
    """Work out the real increment from the clock, rather than trusting the rules.

    The clock handed over this move is the last one, less what the referee charged for that move,
    plus the increment. The time spent is known, so the increment falls out of the difference.
    The referee charges a little more than the agent measures, so this reads slightly low, which
    is the safe direction; taking the minimum keeps one noisy sample from raising the estimate.
    """
    global _increment_s
    if _last_clock_ms is None:
        return
    observed = (time_left_ms - _last_clock_ms) / 1000.0 + _last_elapsed_s
    if 0.0 <= observed < INCREMENT_S:
        _increment_s = min(_increment_s, observed)


def _time_budget(board: chess.Board, time_left_ms: int) -> float:
    """How long this move may take, in seconds.

    A move is worth roughly its share of what is left on the clock plus most of the increment it
    earns back. The share cap and the reserve keep a long think from turning into a flag fall.
    """
    time_left = max(0.0, time_left_ms / 1000.0)
    moves_to_go = max(MIN_MOVES_TO_GO, EXPECTED_GAME_MOVES - board.fullmove_number)
    target = time_left / moves_to_go + _increment_s * INCREMENT_SHARE
    budget = min(target, time_left * MAX_CLOCK_SHARE, time_left - CLOCK_RESERVE_S)
    return max(0.02, budget)


def _reset_for_new_game() -> None:
    global _increment_s, _last_clock_ms, _last_elapsed_s
    _transposition_table.clear()
    _evaluation_cache.clear()
    _history.clear()
    _game_history.clear()
    for killers in _killers:
        killers[0] = None
        killers[1] = None
    _increment_s = INCREMENT_S
    _last_clock_ms = None
    _last_elapsed_s = 0.0


def get_move(fen: str, time_left_ms: int) -> str:
    """Return a legal move in UCI notation."""
    global _piece_count, _root_turn, _nodes, _last_clock_ms, _last_elapsed_s
    started_at = time.monotonic()
    board = chess.Board(fen)

    # Pieces only ever leave the board, so a fuller position than last time means a new game.
    count = chess.popcount(board.occupied)
    if count > _piece_count:
        _reset_for_new_game()
    _piece_count = count
    _root_turn = board.turn

    _observe_increment(time_left_ms)
    _last_clock_ms = time_left_ms

    root_key = _key(board)
    _game_history[root_key] = _game_history.get(root_key, 0) + 1
    for entry in list(_history):
        _history[entry] //= 2

    root_moves = _ordered_moves(board, None, 0)
    if not root_moves:
        raise ValueError(f"No legal move available for position: {fen}")

    storm_files = _storm_files(board)

    budget = _time_budget(board, time_left_ms)
    deadline = started_at + budget
    _nodes = 0

    # Deepen while the clock allows. Every iteration scores the root moves in a good order, so an
    # iteration cut short by the clock still leaves a usable move behind.
    chosen = principal = root_moves[0]
    best_score = 0.0
    reached = 0
    for depth in range(1, MAX_SEARCH_DEPTH + 1):
        _path.clear()
        scored, completed = _search_root(board, depth, deadline, principal, storm_files)
        if scored:
            chosen, principal, best_score = _pick_root_move(board, scored, storm_files)
        if not completed:
            break
        reached = depth
        if abs(best_score) > MATE_THRESHOLD:
            break
        if reached >= DECISIVE_MIN_DEPTH and best_score >= DECISIVE_ADVANTAGE:
            break
        if time.monotonic() - started_at > budget * NEXT_ITERATION_SHARE:
            break

    _last_elapsed_s = time.monotonic() - started_at
    if DEBUG:
        rate = _nodes / _last_elapsed_s if _last_elapsed_s else 0.0
        storm = "" if chosen == principal else f" (flank bonus over {principal.uci()})"
        # Flushed, because the referee kills the process at the end of the game and anything
        # still sitting in the buffer never reaches the validation log.
        print(
            f"depth {reached} score {best_score:9.1f} nodes {_nodes:7d} "
            f"{_last_elapsed_s:5.2f}s of {budget:5.2f}s inc {_increment_s:.2f}s "
            f"{rate:7.0f} n/s {chosen.uci()}{storm}",
            flush=True,
        )
    return chosen.uci()
