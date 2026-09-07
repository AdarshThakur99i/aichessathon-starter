// The page: a board, two clocks, and the player folders.
//
// chess.js is used only for legality, SAN and game-over detection on this side. The engine is
// never consulted about legality here and the server checks every move it is sent, so a tampered
// page cannot make the engine play from an impossible position.
//
// It is vendored in vendor/ rather than imported from a CDN. The published dist/esm/chess.js
// contains `import { parse } from './pgn'`, and a browser resolves that extensionless specifier
// to a path the CDN does not serve, so the whole module graph fails and none of this file runs.
// vendor/chess.js is jsdelivr's +esm bundle, which has those imports already inlined, and it
// means the board still works with no network.

import { Chess } from "/vendor/chess.js";

const FILES = "abcdefgh";

const el = (id) => document.getElementById(id);
const views = [...document.querySelectorAll(".view")];

let game = null;

// ---------------------------------------------------------------- routing

function show(name) {
  views.forEach((view) => (view.hidden = view.id !== `view-${name}`));
  document.querySelectorAll("nav a").forEach((a) => a.removeAttribute("aria-current"));
  const tab = name === "players" || name === "player" ? "players" : "play";
  document.querySelector(`nav a[data-nav="${tab}"]`)?.setAttribute("aria-current", "page");
}

async function route() {
  const hash = location.hash.replace(/^#\/?/, "");
  if (hash === "players") { show("players"); await loadPlayers(); return; }
  if (hash.startsWith("player/")) {
    show("player");
    await loadPlayer(decodeURIComponent(hash.slice("player/".length)));
    return;
  }
  show(game && !game.over ? "game" : "setup");
}

window.addEventListener("hashchange", route);

// ---------------------------------------------------------------- board

const squareCell = (name) => el("board").querySelector(`.sq[data-square="${name}"]`);

function label(className, text) {
  const span = document.createElement("span");
  span.className = className;
  span.textContent = text;
  return span;
}

function squareName(index, flipped) {
  const shown = flipped ? 63 - index : index;
  return FILES[shown % 8] + (8 - Math.floor(shown / 8));
}

// Build the 64 squares, once per orientation. Cells are buttons on a board that is played on,
// so Enter and Space reach them, and plain divs on one that is only being watched.
function fillBoard(node, flipped, onClick) {
  const facing = flipped ? "black" : "white";
  if (node.childElementCount === 64 && node.dataset.facing === facing) return;
  node.dataset.facing = facing;
  node.replaceChildren(
    ...Array.from({ length: 64 }, (_, index) => {
      const cell = document.createElement(onClick ? "button" : "div");
      if (onClick) cell.type = "button";
      cell.className = "sq";
      cell.append(label("pc", ""));
      // Files along the bottom rank, ranks up the left edge, as on a printed diagram.
      const name = squareName(index, flipped);
      if (index >= 56) cell.append(label("co file", name[0]));
      if (index % 8 === 0) cell.append(label("co rank", name[1]));
      if (onClick) {
        cell.addEventListener("click", (event) => {
          if (event.detail === 0) onClick(cell.dataset.square);
        });
      }
      return cell;
    }),
  );
}

// Paint a position onto squares already built. Everything the board shows comes in through the
// marks argument, so the live game and a replay can share this.
function paintBoard(node, view, flipped, marks = {}) {
  const inCheck = view.inCheck();
  const turn = view.turn();
  [...node.children].forEach((cell, index) => {
    const name = squareName(index, flipped);
    const shown = flipped ? 63 - index : index;
    const piece = view.get(name);
    const code = piece ? piece.color + piece.type : "";
    cell.dataset.square = name;
    cell.dataset.piece = code;
    cell.className = "sq" + ((Math.floor(shown / 8) + (shown % 8)) % 2 ? " d" : "");
    if (name === marks.selected) cell.classList.add("from");
    if (marks.targets?.has(name)) cell.classList.add("to");
    if (marks.lastMove && (name === marks.lastMove.from || name === marks.lastMove.to)) {
      cell.classList.add("last");
    }
    if (marks.premoves?.some((p) => p.from === name || p.to === name)) {
      cell.classList.add("pre");
    }
    if (inCheck && code === turn + "k") cell.classList.add("check");
    cell.setAttribute("aria-label", code ? `${name} ${code}` : name);
  });
}

function drawBoard() {
  const flipped = game.human === "black";
  fillBoard(el("board"), flipped, onSquare);
  paintBoard(el("board"), position(), flipped, {
    selected: game.selected,
    targets: new Set(game.selected ? destinations(game.selected) : []),
    lastMove: game.lastMove,
    premoves: game.premoves,
  });
  // The paint rewrites every square's classes. If a piece is in the air, as it is when the
  // engine's reply lands mid-drag, it would otherwise reappear on its square under the cursor.
  if (drag?.ghost) {
    squareCell(drag.from)?.classList.add("lifted");
    if (drag.over) squareCell(drag.over)?.classList.add("over");
  }
}

function drawMoves() {
  const history = game.chess.history();
  const rows = [];
  const startsWithBlack = game.startFen.split(" ")[1] === "b";
  const firstNumber = Number(game.startFen.split(" ")[5] || 1);
  for (let i = 0; i < history.length; i += 2) {
    rows.push([history[i], history[i + 1]]);
  }
  el("moves").replaceChildren(
    ...rows.map(([a, b], i) => {
      const item = document.createElement("li");
      item.value = firstNumber + i;
      item.innerHTML = `<span>${a ?? ""}</span><span>${b ?? ""}</span>`;
      if (startsWithBlack && i === 0) item.innerHTML = `<span>…${a ?? ""}</span><span>${b ?? ""}</span>`;
      return item;
    }),
  );
  el("moves").scrollTop = el("moves").scrollHeight;
}

// ---------------------------------------------------------------- the bot speaks

const FACES = { happy: "/bot.svg", plead: "/bot-plead.svg", sad: "/bot-sad.svg" };

// Trotted out when she loses. None of them are true.
const EXCUSES = [
  "not fair, my transposition table was full",
  "I was pondering on your time and lost my train of thought",
  "the board was the wrong way round from where I was sitting",
  "I had one too many before the game, hic",
  "I let you win. obviously. on purpose",
  "someone unplugged half my search depth",
  "that was only my second best move, the first one was better",
  "you won because I was being polite, senpai",
  "the sun was in my eyes, all sixty-four of them",
  "I was thinking about lunch, not chess",
  "my clock was running faster than yours, I felt it",
  "rematch. I was only warming up",
  "aaj shyd aapka luck aapke saath h",
  "Tukka",
  "khush reh",
  "tum bhi kya yaad rkhoge",
];

// Said once, when she loses a game in which her draw offer was turned down, before the excuse.
const REVENGE = "thukra ke mera draw mera inteqam dekhoge";
const REVENGE_MS = 4000;

const pick = (list) => list[Math.floor(Math.random() * list.length)];

let typing = null;
let followUp = null;

// Reveal the line a character at a time, so she looks like she is saying it. Anything queued to
// be said after an earlier line is dropped: a new line always wins.
function say(text, mood = "happy") {
  if (typing) clearInterval(typing);
  typing = null;
  if (followUp) clearTimeout(followUp);
  followUp = null;
  const bubble = el("bot-say");
  const target = el("bot-text");
  if (!bubble || !target) return;
  el("bot-face").src = FACES[mood] ?? FACES.happy;
  bubble.dataset.mood = mood;
  bubble.hidden = false;
  bubble.classList.add("typing");
  target.textContent = "";
  let shown = 0;
  typing = setInterval(() => {
    shown += 1;
    target.textContent = text.slice(0, shown);
    if (shown >= text.length) {
      clearInterval(typing);
      typing = null;
      bubble.classList.remove("typing");
    }
  }, 32);
}

// Say one thing, then another a few seconds later.
function sayThen(first, firstMood, delay, second, secondMood) {
  say(first, firstMood);
  followUp = setTimeout(() => {
    followUp = null;
    say(second, secondMood);
  }, delay);
}

// ---------------------------------------------------------------- clocks

// Counted in whole tenths rather than fractional seconds: 0.7 seconds is not representable in
// binary, and flooring it once it has been scaled shows 0.6. Returns the digits and the tenths
// separately so the page can dim the tenths.
function formatClock(ms) {
  const tenths = Math.max(0, Math.ceil(ms / 100));
  const minutes = Math.floor(tenths / 600);
  const seconds = Math.floor((tenths % 600) / 10);
  return {
    main: `${minutes}:${String(seconds).padStart(2, "0")}`,
    frac: tenths < 200 ? `.${tenths % 10}` : "",
  };
}

function drawClocks() {
  const top = game.human === "white" ? "black" : "white";
  const bottom = game.human;
  const turn = game.chess.turn() === "w" ? "white" : "black";
  for (const [node, side] of [[el("clock-top"), top], [el("clock-bottom"), bottom]]) {
    const { main, frac } = formatClock(game.clock[side]);
    node.dataset.side = side;
    node.querySelector(".who").textContent = side === game.human ? "You" : "drunkenmaster";
    node.querySelector(".time .mm").textContent = main;
    node.querySelector(".time .frac").textContent = frac;
    node.classList.toggle("active", side === turn && !game.over);
    node.classList.toggle("low", game.clock[side] < game.lowAt);
  }
}

function startTicking() {
  stopTicking();
  game.tickFrom = performance.now();
  game.timer = setInterval(() => {
    const side = game.chess.turn() === "w" ? "white" : "black";
    const now = performance.now();
    game.clock[side] -= now - game.tickFrom;
    game.tickFrom = now;
    if (game.clock[side] <= 0) {
      game.clock[side] = 0;
      // Only the player is flagged here. The engine is charged what its search took, settled
      // when its reply arrives, so the wall clock it shows while waiting cannot end the game.
      if (side === game.human) finish("lost", "flag");
    }
    drawClocks();
  }, 100);
}

function stopTicking() {
  if (game.timer) clearInterval(game.timer);
  game.timer = null;
}

function chargeClock(side) {
  const now = performance.now();
  game.clock[side] -= now - game.tickFrom;
  game.clock[side] += game.increment;
  game.tickFrom = now;
  if (game.clock[side] < 0) game.clock[side] = 0;
}

// ---------------------------------------------------------------- pondering
//
// While the player thinks, ask the server to work out its replies to the moves they are most
// likely to play. One candidate per request: the loop stops simply by not sending the next one,
// so there is no cancellation for the server to get wrong, and a real move can only ever queue
// behind the single candidate already in flight.
//
// A reply that is waiting when the move arrives comes back immediately, which costs the engine
// almost none of its clock. That is most of the point at 10 and 30 seconds.

let pondering = null;

function stopPondering() {
  pondering?.abort();
  pondering = null;
}

async function ponderWhileThinking() {
  stopPondering();
  const controller = new AbortController();
  pondering = controller;
  const engine = game.human === "white" ? "black" : "white";
  try {
    // Bounded so a player who walks away does not leave the loop running for ever. There are at
    // most a dozen candidates to answer anyway.
    for (let step = 0; step < 16; step += 1) {
      if (controller.signal.aborted) return;
      if (!game || game.over || game.thinking) return;
      if (game.chess.turn() !== game.human[0]) return;
      const response = await fetch("/api/ponder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          start_fen: game.startFen,
          moves: game.moves,
          time_left_ms: Math.round(game.clock[engine]),
          increment_ms: game.increment,
        }),
        signal: controller.signal,
      });
      if (!response.ok) return;
      if ((await readJson(response)).done) return;
    }
  } catch {
    // Aborted because the player moved, or the endpoint is not deployed. Either way pondering is
    // an optimisation and the game carries on without it.
  }
}

// ---------------------------------------------------------------- play

const isOurTurn = () => game.chess.turn() === game.human[0];

// ---------------------------------------------------------------- premove geometry
//
// A premove cannot be judged by the rules of the position on the board, because the position it
// will be played in does not exist yet. Asking chess.js for legal moves with the turn flipped
// looked close enough and was not: it refuses a pawn capture onto a square that is empty right
// now, which is exactly the recapture a player most wants to premove, and it refuses to move a
// piece that is pinned at this instant even when the engine is about to unpin it.
//
// So destinations are generated here from the movement rules alone. The engine's pieces are
// ignored, since they are about to move; only our own pieces block, and check is not considered.
// Nothing is trusted: every premove is validated against the real position by attempt() before it
// is played, and dropped if it has become impossible.

const RAY_DIRECTIONS = {
  r: [[1, 0], [-1, 0], [0, 1], [0, -1]],
  b: [[1, 1], [1, -1], [-1, 1], [-1, -1]],
};
RAY_DIRECTIONS.q = [...RAY_DIRECTIONS.r, ...RAY_DIRECTIONS.b];

const STEP_DIRECTIONS = {
  n: [[1, 2], [2, 1], [2, -1], [1, -2], [-1, -2], [-2, -1], [-2, 1], [-1, 2]],
  k: [[1, 0], [1, 1], [0, 1], [-1, 1], [-1, 0], [-1, -1], [0, -1], [1, -1]],
};

const fileOf = (square) => FILES.indexOf(square[0]);
const rankOf = (square) => Number(square[1]) - 1;
const squareAt = (file, rank) => FILES[file] + (rank + 1);
const onBoard = (file, rank) => file >= 0 && file < 8 && rank >= 0 && rank < 8;

function premoveTargets(board, square) {
  const piece = board.get(square);
  if (!piece || piece.color !== game.human[0]) return [];
  const colour = piece.color;
  const file = fileOf(square);
  const rank = rankOf(square);
  const ours = (name) => board.get(name)?.color === colour;
  const found = [];
  const offer = (name) => {
    if (!ours(name)) found.push(name);
  };

  if (piece.type === "p") {
    const step = colour === "w" ? 1 : -1;
    const home = colour === "w" ? 1 : 6;
    if (onBoard(file, rank + step)) offer(squareAt(file, rank + step));
    // Two squares only from home, and only if our own piece is not standing in the way.
    if (rank === home && !ours(squareAt(file, rank + step))) {
      offer(squareAt(file, rank + 2 * step));
    }
    // Both diagonals, occupied or not: that is the whole point of a premoved capture.
    for (const side of [-1, 1]) {
      if (onBoard(file + side, rank + step)) offer(squareAt(file + side, rank + step));
    }
    return found;
  }

  if (STEP_DIRECTIONS[piece.type]) {
    for (const [df, dr] of STEP_DIRECTIONS[piece.type]) {
      if (onBoard(file + df, rank + dr)) offer(squareAt(file + df, rank + dr));
    }
    if (piece.type === "k" && rank === (colour === "w" ? 0 : 7) && file === 4) {
      // Castling, on rights and our own pieces being out of the way. Whether a square is
      // attacked is not knowable yet, so it is left to attempt() to refuse.
      const rights = board.fen().split(" ")[2];
      const clear = (files) => files.every((f) => !board.get(squareAt(f, rank)));
      if (rights.includes(colour === "w" ? "K" : "k") && clear([5, 6])) {
        found.push(squareAt(6, rank));
      }
      if (rights.includes(colour === "w" ? "Q" : "q") && clear([1, 2, 3])) {
        found.push(squareAt(2, rank));
      }
    }
    return found;
  }

  for (const [df, dr] of RAY_DIRECTIONS[piece.type] ?? []) {
    let f = file + df;
    let r = rank + dr;
    while (onBoard(f, r)) {
      const name = squareAt(f, r);
      // Our own piece ends the ray: it cannot move before this premove is played. Theirs does
      // not. It may well move, which is exactly what a premove along this line is betting on, so
      // the square it stands on and every square beyond it stay on offer. Stopping at the first
      // enemy piece, as the rules of a real move would, is what made a bishop dragged to a square
      // behind an enemy pawn snap straight back.
      if (board.get(name)?.color === colour) break;
      found.push(name);
      f += df;
      r += dr;
    }
  }
  return found;
}

const promotes = (piece, to) =>
  piece.type === "p" && to[1] === (piece.color === "w" ? "8" : "1");

// Lay a premove onto a board by hand. chess.js move() cannot be used because the result is not a
// position the rules can reach yet. Only the drawing depends on this, so en passant is not
// modelled; the captured pawn simply lingers in the preview until the move is really played.
function applyPremove(board, { from, to, promotion }) {
  const piece = board.get(from);
  if (!piece) return;
  board.remove(from);
  board.remove(to);
  board.put({ type: promotion ?? piece.type, color: piece.color }, to);
  if (piece.type === "k" && Math.abs(fileOf(to) - fileOf(from)) === 2) {
    const rank = rankOf(from);
    const [cornerFile, landingFile] = fileOf(to) === 6 ? [7, 5] : [0, 3];
    const rook = board.get(squareAt(cornerFile, rank));
    if (rook) {
      board.remove(squareAt(cornerFile, rank));
      board.put(rook, squareAt(landingFile, rank));
    }
  }
}

// ---------------------------------------------------------------- the premove queue
//
// Several premoves can be stacked, as on chess.com. Each is chosen from the position the previous
// ones would leave, so a whole sequence can be laid out while the engine thinks, and the board
// shows the end of that sequence rather than the truth.

let viewCache = { key: null, board: null };

function position() {
  if (!game.premoves.length) return game.chess;
  const key = `${game.chess.fen()}|${game.premoves
    .map((p) => p.from + p.to + (p.promotion ?? ""))
    .join(" ")}`;
  if (viewCache.key === key) return viewCache.board;
  const board = new Chess();
  board.load(game.chess.fen(), { skipValidation: true });
  for (const premove of game.premoves) applyPremove(board, premove);
  viewCache = { key, board };
  return board;
}

// Whether the player may pick this square up. Not conditional on the turn: picking a piece up
// while the engine thinks is how a premove gets made. Read from the previewed position, so a
// piece that an earlier premove put somewhere can be moved again.
function canLift(name) {
  if (!game || game.over) return false;
  const piece = position().get(name);
  return Boolean(piece && piece.color === game.human[0]);
}

// Where a selected piece may go: really legal moves on our turn, premove geometry otherwise.
function destinations(square) {
  if (isOurTurn()) {
    return game.chess.moves({ square, verbose: true }).map((m) => m.to);
  }
  return premoveTargets(position(), square);
}

// Play from -> to if it is legal now, and report whether it was.
function attempt(from, to, promotion) {
  const legal = game.chess.moves({ square: from, verbose: true }).find((m) => m.to === to);
  if (!legal) return false;
  const choice = legal.promotion ? promotion ?? askPromotion() : undefined;
  game.selected = null;
  play(from, to, choice);
  return true;
}

function queuePremove(from, to) {
  const board = position();
  const piece = board.get(from);
  if (!piece || !premoveTargets(board, from).includes(to)) return false;
  const promotion = promotes(piece, to) ? askPromotion() : undefined;
  game.premoves.push({ from, to, promotion });
  game.selected = null;
  drawBoard();
  const count = game.premoves.length;
  el("status").textContent =
    count === 1
      ? "Premove set. Right-click the board to clear it."
      : `${count} premoves queued. Right-click the board to clear them.`;
  return true;
}

// The move the player has asked for. On their turn it is played; off it, it is queued. Nothing
// else decides this: the queue is always empty by the time it is their turn again, because
// playPremove drains one the instant the engine's reply lands.
function commit(from, to) {
  return isOurTurn() ? attempt(from, to) : queuePremove(from, to);
}

// Called once the engine has replied: take the front of the queue and play it. The engine's move
// may have made it impossible, by taking the piece, blocking the path or pinning it. The rest of
// the queue was chosen on the strength of this move happening, so it goes too rather than being
// played out of a position nobody planned for.
function playPremove() {
  const next = game.premoves.shift();
  if (!next) return;
  if (attempt(next.from, next.to, next.promotion)) return;
  const abandoned = game.premoves.length;
  game.premoves.length = 0;
  el("status").textContent = abandoned
    ? "Your move. That premove was no longer legal, so the queue was dropped."
    : "Your move. The premove was no longer legal.";
  drawBoard();
}

function onSquare(name) {
  if (!game || game.over) return;
  if (game.selected && game.selected !== name && commit(game.selected, name)) return;
  const piece = position().get(name);
  // A click that is not a move and not a pick-up only puts the piece down. It must never discard
  // what is queued: a move the player has made is theirs until they cancel it or it is played.
  game.selected = piece && piece.color === game.human[0] ? name : null;
  drawBoard();
}

// ---------------------------------------------------------------- dragging
//
// Pointer events rather than HTML5 drag and drop, which cannot follow a finger and gives no
// control over the image being dragged. The piece under the cursor is a fixed-position copy
// parented to the body, so it can be carried off the board without being clipped.
//
// A press that never travels more than a few pixels is treated as a click and handed to
// onSquare, which keeps the click-a-square-then-click-another way of moving working as well.

const DRAG_THRESHOLD = 5;

let drag = null;

// Half a square of slack outside the rim. Releasing a piece a few pixels past the edge of the
// board used to find no square at all and snap it home, which on the a-file and the far rank is
// most of what "I let go and it did not go there" was. Beyond the slack it is still a cancel.
const DROP_SLACK = 0.5;

function squareFromPoint(clientX, clientY) {
  const box = el("board").getBoundingClientRect();
  const slack = (box.width / 8) * DROP_SLACK;
  const outside =
    clientX < box.left - slack ||
    clientX > box.right + slack ||
    clientY < box.top - slack ||
    clientY > box.bottom + slack;
  if (outside) return null;
  const clamp = (value) => Math.min(7, Math.max(0, Math.floor(value * 8)));
  const file = clamp((clientX - box.left) / box.width);
  const rank = clamp((clientY - box.top) / box.height);
  return squareName(rank * 8 + file, game.human === "black");
}

function carry(clientX, clientY) {
  const size = el("board").getBoundingClientRect().width / 8;
  drag.ghost.style.width = `${size}px`;
  drag.ghost.style.height = `${size}px`;
  drag.ghost.style.transform = `translate(${clientX - size / 2}px, ${clientY - size / 2}px)`;
}

function lift(clientX, clientY) {
  // Selecting first means the legal destinations are already showing as the piece comes up.
  game.selected = drag.from;
  drawBoard();
  const cell = squareCell(drag.from);
  drag.ghost = document.createElement("div");
  drag.ghost.className = "ghost";
  drag.ghost.dataset.piece = cell.dataset.piece;
  document.body.append(drag.ghost);
  cell.classList.add("lifted");
  el("board").dataset.dragging = "1";
  carry(clientX, clientY);
}

function endDrag() {
  if (!drag) return;
  drag.ghost?.remove();
  squareCell(drag.from)?.classList.remove("lifted");
  el("board").querySelector(".sq.over")?.classList.remove("over");
  delete el("board").dataset.dragging;
  try {
    el("board").releasePointerCapture(drag.pointerId);
  } catch {
    // Never captured, because the press was not on a piece of the player's.
  }
  drag = null;
}

function onPointerDown(event) {
  if (event.pointerType === "mouse" && event.button !== 0) return;
  const cell = event.target.closest?.(".sq");
  if (!cell || !game) return;
  drag = {
    from: cell.dataset.square,
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    ghost: null,
    over: null,
  };
  if (!canLift(drag.from)) return;
  // Keep receiving moves once the cursor leaves the square, and suppress text selection.
  el("board").setPointerCapture(event.pointerId);
  event.preventDefault();
}

function onPointerMove(event) {
  if (!drag || event.pointerId !== drag.pointerId) return;
  if (!drag.ghost) {
    const travelled = Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY);
    if (travelled < DRAG_THRESHOLD || !canLift(drag.from)) return;
    lift(event.clientX, event.clientY);
  }
  carry(event.clientX, event.clientY);
  const over = squareFromPoint(event.clientX, event.clientY);
  drag.over = over && over !== drag.from ? over : null;
  const lit = el("board").querySelector(".sq.over");
  if (lit?.dataset.square !== drag.over) {
    lit?.classList.remove("over");
    if (drag.over) squareCell(drag.over)?.classList.add("over");
  }
}

function onPointerUp(event) {
  if (!drag || event.pointerId !== drag.pointerId) return;
  const { from, ghost } = drag;
  const to = ghost ? squareFromPoint(event.clientX, event.clientY) : null;
  endDrag();
  if (!ghost) {
    // Never travelled far enough to be a drag, so treat it as a click on the square.
    onSquare(from);
    return;
  }
  if (to && to !== from && commit(from, to)) return;
  // Dropped back where it came from, it stays in hand so a click can finish the move. Dropped
  // off the board or somewhere it cannot go, it is put down.
  game.selected = to === from ? from : null;
  drawBoard();
}

function askPromotion() {
  const answer = (prompt("Promote to q, r, b or n?", "q") || "q").toLowerCase();
  return ["q", "r", "b", "n"].includes(answer) ? answer : "q";
}

function play(from, to, promotion) {
  // Whatever was being pondered is either about to be used or no longer interesting.
  stopPondering();
  let move;
  try {
    move = game.chess.move({ from, to, promotion });
  } catch {
    return;
  }
  game.lastMove = move;
  game.moves.push(move.lan ?? from + to + (promotion ?? ""));
  chargeClock(game.human);
  drawBoard();
  drawMoves();
  drawClocks();
  if (checkOver()) return;
  void engineMove();
}

// She asks for the draw once a game, the moment the player has a mate available. chess.js marks
// a mating move with # in its SAN, so the whole test is one string check over the player's moves.
function maybeBegForDraw() {
  if (!game || game.over || game.begged || game.thinking) return;
  // At a minute or less a modal is an ambush rather than a joke, so she keeps it to herself.
  if (game.base < 180) return;
  if (game.chess.turn() !== game.human[0]) return;
  if (!game.chess.moves().some((san) => san.endsWith("#"))) return;
  game.begged = true;
  el("draw-offer").hidden = false;
  say("kuchu puchu draw ??", "plead");
}

function checkOver() {
  if (!game.chess.isGameOver()) return false;
  const loser = game.chess.turn() === "w" ? "white" : "black";
  if (game.chess.isCheckmate()) {
    finish(loser === game.human ? "lost" : "won", "checkmate");
  } else if (game.chess.isStalemate()) {
    finish("drawn", "stalemate");
  } else if (game.chess.isInsufficientMaterial()) {
    finish("drawn", "insufficient_material");
  } else if (game.chess.isThreefoldRepetition()) {
    finish("drawn", "threefold_repetition");
  } else {
    finish("drawn", "fifty_moves");
  }
  return true;
}

// Read a response that ought to be JSON. A crashed serverless function answers with the
// platform's own HTML error page, and calling .json() on that throws a parse error about the
// first character, which buries the status and the platform's error code. So the body is taken as
// text first and the status is always reported even when the body is unreadable.
async function readJson(response) {
  const body = await response.text();
  let data = null;
  try {
    data = body ? JSON.parse(body) : null;
  } catch {
    const stripped = body.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim().slice(0, 160);
    throw new Error(`HTTP ${response.status}${stripped ? ` - ${stripped}` : ""}`);
  }
  if (!response.ok) throw new Error(data?.error || `HTTP ${response.status}`);
  return data;
}

async function engineMove() {
  const engine = game.human === "white" ? "black" : "white";
  game.thinking = true;
  el("status").textContent = "drunkenmaster is thinking…";
  // Her clock as her turn begins. She is charged the time the search itself took, as reported by
  // the server, and not the round trip: the network, a cold start and any queue behind a ponder
  // are not her thinking, in the same way a site does not charge a player for their lag. The
  // ticking clock in the meantime is for show and is corrected when the reply lands.
  const before = game.clock[engine];
  try {
    const response = await fetch("/api/move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        start_fen: game.startFen,
        moves: game.moves,
        time_left_ms: Math.round(before),
        increment_ms: game.increment,
      }),
    });
    const data = await readJson(response);
    const left = before - data.thinking_ms;
    if (left <= 0) {
      // Flagged on thinking alone. As over the board, a move made after the flag does not count.
      game.clock[engine] = 0;
      game.thinking = false;
      drawClocks();
      finish("won", "flag");
      return;
    }
    const move = game.chess.move(data.move);
    game.lastMove = move;
    game.moves.push(data.move);
    game.clock[engine] = left + game.increment;
    game.tickFrom = performance.now();
    const searched = data.pondered ? data.searched_ms : data.thinking_ms;
    const lines = [
      `last reply: ${data.san} in ${(searched / 1000).toFixed(1)}s, ` +
        `${data.nodes.toLocaleString()} positions` +
        (data.pondered ? ", worked out on your time so it cost her nothing" : ""),
    ];
    // data.learned is set only when experience overrode the search's choice.
    if (data.learned) lines.push(data.learned);
    el("engine-note").textContent = lines.join("\n");
    drawBoard();
    drawMoves();
    drawClocks();
    game.thinking = false;
    if (!checkOver()) {
      el("status").textContent = "Your move.";
      playPremove();
      // Only worth starting if the premove did not immediately hand the turn back.
      if (!game.over && !game.thinking) void ponderWhileThinking();
      maybeBegForDraw();
    }
  } catch (failure) {
    game.thinking = false;
    el("status").textContent = `The engine could not reply: ${failure.message}`;
    // The ticker no longer flags the engine, so a failed request must not leave resigning as the
    // only way out. The game is not finished or saved, and so nothing is learned from a network
    // fault; the player simply gets the door to a new game.
    el("again").hidden = false;
  }
}

function finish(result, termination) {
  if (game.over) return;
  el("draw-offer").hidden = true;
  // result is the player's, so a win for them is a loss for her.
  if (result === "won") {
    // She takes a refused draw personally, but only once, and only if she then lost.
    if (game.drawRejected) sayThen(REVENGE, "sad", REVENGE_MS, pick(EXCUSES), "sad");
    else say(pick(EXCUSES), "sad");
  }
  else if (result === "lost") say("kuchu puchu~ good game", "happy");
  else say("a draw! we are both very strong", "happy");
  game.over = true;
  game.thinking = false;
  game.premoves.length = 0;
  stopPondering();
  stopTicking();
  const words = { won: "You won", lost: "You lost", drawn: "Drawn" };
  el("status").textContent = `${words[result]} by ${termination.replace(/_/g, " ")}.`;
  el("resign").hidden = true;
  el("review").hidden = false;
  el("again").hidden = false;
  drawClocks();
  void saveGame(result, termination);
}

async function saveGame(result, termination) {
  // A real header block, so the stored PGN stands on its own: the player folders read it and so
  // does tools/harvest.py, which feeds these games to weight tuning.
  const scored = { won: "1-0", lost: "0-1", drawn: "1/2-1/2" }[result];
  const fromWhite = { "1-0": "0-1", "0-1": "1-0", "1/2-1/2": "1/2-1/2" };
  game.chess.setHeader("Event", "drunkenmaster web");
  game.chess.setHeader("Date", new Date().toISOString().slice(0, 10).replace(/-/g, "."));
  game.chess.setHeader("White", game.human === "white" ? game.player : "drunkenmaster");
  game.chess.setHeader("Black", game.human === "black" ? game.player : "drunkenmaster");
  game.chess.setHeader("Result", game.human === "white" ? scored : fromWhite[scored]);
  game.chess.setHeader("Termination", termination);
  game.chess.setHeader("TimeControl", game.label);
  try {
    const response = await fetch("/api/games", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        player: game.player,
        result,
        termination,
        pgn: game.chess.pgn(),
        moves: game.moves,
        start_fen: game.startFen,
        colour: game.human,
        time_control: game.label,
        plies: game.moves.length,
      }),
    });
    const data = await response.json();
    if (response.ok) {
      el("engine-note").innerHTML =
        `Saved to <a href="#/player/${data.slug}">${game.player}'s folder</a>.` +
        (data.persistent ? "" : " (storage is not configured, so this will not survive a restart)");
    }
  } catch {
    el("engine-note").textContent = "The game could not be saved.";
  }
}

el("setup").addEventListener("submit", (event) => {
  event.preventDefault();
  const player = el("name").value.trim();
  if (!player) return;
  localStorage.setItem("chessathon:name", player);
  const label = el("time-control").value;
  const [base, increment] = label.split("+").map(Number);
  let human = el("colour").value;
  if (human === "random") human = Math.random() < 0.5 ? "white" : "black";

  const chess = new Chess();
  game = {
    player, chess, human, label,
    startFen: chess.fen(),
    increment: increment * 1000,
    clock: { white: base * 1000, black: base * 1000 },
    // A fifth of the starting clock, kept inside sensible bounds.
    lowAt: Math.min(20_000, Math.max(3_000, base * 200)),
    moves: [], selected: null, lastMove: null, premoves: [], begged: false,
    drawRejected: false,
    // The draw plea is only offered when there is time to read it.
    base,
    over: false, thinking: false, timer: null, tickFrom: performance.now(),
  };
  el("resign").hidden = false;
  el("review").hidden = true;
  el("again").hidden = true;
  el("engine-note").textContent = "";
  el("status").textContent = human === "white" ? "Your move." : "drunkenmaster is thinking…";
  show("game");
  drawBoard();
  drawMoves();
  drawClocks();
  startTicking();
  say("kuchu puchu~ let's play", "happy");
  if (human === "black") void engineMove();
  else void ponderWhileThinking();
});

el("resign").addEventListener("click", () => finish("lost", "resignation"));

// The plea. Yes agrees the draw; the cross, Escape or a click outside just dismisses it.
el("draw-yes").addEventListener("click", () => {
  el("draw-offer").hidden = true;
  finish("drawn", "agreement");
});
const declineDraw = () => {
  el("draw-offer").hidden = true;
  if (game && !game.over) {
    game.drawRejected = true;
    say("fine, no draw. I am still winning inside", "sad");
  }
};
el("draw-no").addEventListener("click", declineDraw);
el("draw-offer").addEventListener("click", (event) => {
  if (event.target === el("draw-offer")) declineDraw();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !el("draw-offer").hidden) declineDraw();
});
el("again").addEventListener("click", () => {
  stopPondering();
  game = null;
  location.hash = "#/";
  show("setup");
});

// ---------------------------------------------------------------- replay
//
// One viewer, opened either from the game that has just finished or from a folder. It holds the
// starting position and the list of moves and rebuilds the position from scratch on every seek:
// three hundred moves is nothing to replay, and it means there is no incremental state to get
// out of step with the board.

const STEP_MS = 900;

let replay = null;

// Stored games keep UCI, which chess.js takes as an object. A game recovered from its PGN gives
// SAN strings instead, which it also takes.
const asMove = (uci) =>
  /^[a-h][1-8][a-h][1-8][qrbn]?$/.test(uci)
    ? { from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci[4] || undefined }
    : uci;

function openReplay({ startFen, moves, flipped, title, note, back }) {
  stopReplay();
  let board;
  try {
    board = new Chess(startFen || undefined);
  } catch {
    board = new Chess();
  }
  // Read the starting position before anything is played onto the board.
  const from = board.fen();
  // Walk the game once to collect the notation and to drop anything that will not replay.
  const played = [];
  for (const move of moves) {
    try {
      played.push(board.move(asMove(move)));
    } catch {
      break;
    }
  }
  // Kept as UCI whatever came in, so seek() has one thing to deal with.
  replay = { startFen: from, moves: played.map((m) => m.lan), flipped, back, ply: 0, timer: null };
  el("replay-title").textContent = title;
  el("replay-note").textContent = note || "";
  buildReplayMoves(played);
  show("replay");
  seek(0);
}

function buildReplayMoves(played) {
  const rows = [];
  for (let i = 0; i < played.length; i += 2) rows.push([played[i], played[i + 1]]);
  const startsWithBlack = played[0]?.color === "b";
  el("replay-moves").replaceChildren(
    ...rows.map(([white, black], row) => {
      const item = document.createElement("li");
      item.value = row + 1;
      for (const [move, ply] of [
        [white, row * 2 + 1],
        [black, row * 2 + 2],
      ]) {
        const cell = document.createElement("button");
        cell.type = "button";
        cell.className = "ply";
        cell.dataset.ply = String(ply);
        cell.textContent = move ? (startsWithBlack && ply === 1 ? `…${move.san}` : move.san) : "";
        if (move) cell.addEventListener("click", () => seek(ply));
        item.append(cell);
      }
      return item;
    }),
  );
}

function seek(ply) {
  if (!replay) return;
  replay.ply = Math.max(0, Math.min(ply, replay.moves.length));
  const board = new Chess(replay.startFen);
  let last = null;
  for (let i = 0; i < replay.ply; i += 1) last = board.move(asMove(replay.moves[i]));
  replay.chess = board;
  fillBoard(el("replay-board"), replay.flipped, null);
  paintBoard(el("replay-board"), board, replay.flipped, { lastMove: last });
  for (const cell of el("replay-moves").querySelectorAll(".ply")) {
    cell.classList.toggle("on", Number(cell.dataset.ply) === replay.ply);
  }
  el("replay-moves").querySelector(".ply.on")?.scrollIntoView({ block: "nearest" });
  if (replay.ply >= replay.moves.length) stopReplay();
}

function stepReplay(by) {
  if (replay) seek(replay.ply + by);
}

function stopReplay() {
  if (replay?.timer) clearInterval(replay.timer);
  if (replay) replay.timer = null;
  el("replay-play").textContent = "Play";
}

function toggleReplay() {
  if (!replay) return;
  if (replay.timer) {
    stopReplay();
    return;
  }
  if (replay.ply >= replay.moves.length) seek(0);
  replay.timer = setInterval(() => stepReplay(1), STEP_MS);
  el("replay-play").textContent = "Pause";
}

// The game that has just been played, from the player's side of the board.
function replayThisGame() {
  const played = game.chess.history({ verbose: true });
  openReplay({
    startFen: game.startFen,
    moves: game.moves,
    flipped: game.human === "black",
    title: `${game.player} vs drunkenmaster`,
    note: `${game.label} · ${played.length} moves · ${el("status").textContent}`,
    back: () => show("game"),
  });
}

for (const [id, action] of [
  ["replay-first", () => seek(0)],
  ["replay-prev", () => stepReplay(-1)],
  ["replay-play", toggleReplay],
  ["replay-next", () => stepReplay(1)],
  ["replay-last", () => seek(replay?.moves.length ?? 0)],
]) {
  el(id).addEventListener("click", () => {
    if (id !== "replay-play") stopReplay();
    action();
  });
}

el("replay-back").addEventListener("click", () => {
  stopReplay();
  const back = replay?.back;
  replay = null;
  if (back) back();
  else void route();
});

el("review").addEventListener("click", replayThisGame);

document.addEventListener("keydown", (event) => {
  if (el("view-replay").hidden) return;
  if (event.key === "ArrowLeft") {
    stopReplay();
    stepReplay(-1);
  } else if (event.key === "ArrowRight") {
    stopReplay();
    stepReplay(1);
  }
});

// ---------------------------------------------------------------- folders

async function loadPlayers() {
  const list = el("player-list");
  list.textContent = "Loading…";
  try {
    const data = await (await fetch("/api/games")).json();
    if (!data.players?.length) {
      list.textContent = "No games yet. Play one and it will appear here.";
      return;
    }
    list.replaceChildren(
      ...data.players.map((p) => {
        const card = document.createElement("a");
        card.className = "card";
        card.href = `#/player/${p.slug}`;
        const r = p.record || {};
        card.innerHTML =
          `<h3></h3><p>${p.games} game${p.games === 1 ? "" : "s"} · ` +
          `<span class="tag won">${r.won || 0}W</span> ` +
          `<span class="tag drawn">${r.drawn || 0}D</span> ` +
          `<span class="tag lost">${r.lost || 0}L</span></p>`;
        card.querySelector("h3").textContent = p.name;
        return card;
      }),
    );
  } catch (failure) {
    list.textContent = `Could not load players: ${failure.message}`;
  }
}

// Recover a game from its PGN, for records saved before the moves were stored separately. The
// starting position has to come back too: a PGN may carry a FEN header, as the tournament's own
// round downloads do, and its moves are meaningless from the standard start.
function fromPgn(pgn) {
  if (!pgn) return { startFen: undefined, moves: [] };
  try {
    const board = new Chess();
    board.loadPgn(pgn);
    return { startFen: board.getHeaders().FEN || undefined, moves: board.history() };
  } catch {
    return { startFen: undefined, moves: [] };
  }
}

async function loadPlayer(slug) {
  const list = el("player-games");
  el("player-title").textContent = slug;
  el("player-summary").textContent = "";
  list.textContent = "Loading…";
  try {
    const data = await (await fetch(`/api/games?player=${encodeURIComponent(slug)}`)).json();
    el("player-title").textContent = data.player || slug;
    if (!data.games?.length) {
      list.textContent = "No games in this folder yet.";
      return;
    }
    const tally = data.games.reduce((acc, g) => ((acc[g.result] = (acc[g.result] || 0) + 1), acc), {});
    el("player-summary").textContent =
      `${data.games.length} games · ${tally.won || 0} won, ${tally.drawn || 0} drawn, ` +
      `${tally.lost || 0} lost against drunkenmaster`;
    list.replaceChildren(
      ...data.games.map((g) => {
        const card = document.createElement("div");
        card.className = "card";
        const when = g.finished_at ? new Date(g.finished_at * 1000).toLocaleString() : "";
        card.innerHTML =
          `<h3><span class="tag ${g.result}">${g.result}</span> as ${g.colour}</h3>` +
          `<p>${g.time_control || ""} · ${g.plies || 0} plies · ` +
          `${(g.termination || "").replace(/_/g, " ")} · ${when}</p>` +
          `<details><summary>PGN</summary><pre class="pgn"></pre></details>`;
        card.querySelector(".pgn").textContent = g.pgn || "(no moves recorded)";

        const open = document.createElement("button");
        open.type = "button";
        open.className = "card-replay";
        open.textContent = "Replay this game";
        // Stored games keep their moves in UCI. Older ones may not, so the PGN is the fallback.
        const record =
          Array.isArray(g.moves) && g.moves.length
            ? { startFen: g.start_fen, moves: g.moves }
            : fromPgn(g.pgn);
        open.disabled = !record.moves.length;
        open.addEventListener("click", () =>
          openReplay({
            startFen: record.startFen,
            moves: record.moves,
            flipped: g.colour === "black",
            title: `${data.player || slug} vs drunkenmaster`,
            note: `${g.result} as ${g.colour} · ${(g.termination || "").replace(/_/g, " ")}` +
              `${when ? ` · ${when}` : ""}`,
            back: () => {
              show("player");
              void loadPlayer(slug);
            },
          }),
        );
        card.prepend(open);
        return card;
      }),
    );
  } catch (failure) {
    list.textContent = `Could not load that folder: ${failure.message}`;
  }
}

// ---------------------------------------------------------------- boot

for (const [type, listener] of [
  ["pointerdown", onPointerDown],
  ["pointermove", onPointerMove],
  ["pointerup", onPointerUp],
  ["pointercancel", () => { endDrag(); if (game) drawBoard(); }],
  ["contextmenu", (event) => {
    if (!game) return;
    event.preventDefault();
    // A long press on a touchscreen raises this too. With a piece in hand it must do nothing,
    // or holding a piece for a moment before sliding it empties the queue you are adding to.
    if (drag) return;
    game.premoves.length = 0;
    game.selected = null;
    drawBoard();
  }],
]) {
  el("board").addEventListener(type, listener);
}

el("name").value = localStorage.getItem("chessathon:name") || "";
fetch("/api/games")
  .then((r) => r.json())
  .then((d) => {
    const lines = [];
    if (!d.persistent) {
      lines.push("Game storage is not configured, so player folders empty on restart.");
    }
    if (d.learning) {
      const { storage, positions, decisions } = d.learning;
      lines.push(
        storage === "off"
          ? "Learning from played games is switched off."
          : `The engine remembers ${decisions} of its own move${decisions === 1 ? "" : "s"} ` +
            `across ${positions} position${positions === 1 ? "" : "s"}, kept in ${storage}.`,
      );
    }
    el("storage-note").textContent = lines.join(" ");
  })
  .catch(() => {});
void route();
