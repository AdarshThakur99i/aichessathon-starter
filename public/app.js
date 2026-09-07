// The page: a board, two clocks, and the player folders.
//
// chess.js is used only for legality, SAN and game-over detection on this side. The engine is
// never consulted about legality here and the server checks every move it is sent, so a tampered
// page cannot make the engine play from an impossible position.

import { Chess } from "https://cdn.jsdelivr.net/npm/chess.js@1.3.1/dist/esm/chess.js";

const GLYPH = {
  wk: "♔", wq: "♕", wr: "♖", wb: "♗", wn: "♘", wp: "♙",
  bk: "♚", bq: "♛", br: "♜", bb: "♝", bn: "♞", bp: "♟",
};
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

function squareName(index, flipped) {
  const shown = flipped ? 63 - index : index;
  return FILES[shown % 8] + (8 - Math.floor(shown / 8));
}

function drawBoard() {
  const board = el("board");
  const flipped = game.human === "black";
  if (board.childElementCount !== 64) {
    board.replaceChildren(
      ...Array.from({ length: 64 }, () => {
        const cell = document.createElement("button");
        cell.type = "button";
        cell.className = "sq";
        cell.addEventListener("click", () => onSquare(cell.dataset.square));
        return cell;
      }),
    );
  }
  const targets = game.selected
    ? new Set(game.chess.moves({ square: game.selected, verbose: true }).map((m) => m.to))
    : new Set();
  const inCheck = game.chess.inCheck();
  const turn = game.chess.turn();

  [...board.children].forEach((cell, index) => {
    const name = squareName(index, flipped);
    const shown = flipped ? 63 - index : index;
    const piece = game.chess.get(name);
    const code = piece ? piece.color + piece.type : "";
    cell.dataset.square = name;
    cell.dataset.piece = code;
    cell.textContent = code ? GLYPH[code] : "";
    cell.className = "sq" + ((Math.floor(shown / 8) + (shown % 8)) % 2 ? " d" : "");
    if (name === game.selected) cell.classList.add("from");
    if (targets.has(name)) cell.classList.add("to");
    if (game.lastMove && (name === game.lastMove.from || name === game.lastMove.to)) {
      cell.classList.add("last");
    }
    if (inCheck && code === turn + "k") cell.classList.add("check");
    cell.setAttribute("aria-label", code ? `${name} ${code}` : name);
  });
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

// ---------------------------------------------------------------- clocks

function formatClock(ms) {
  const total = Math.max(0, Math.ceil(ms / 100) / 10);
  const minutes = Math.floor(total / 60);
  const seconds = total - minutes * 60;
  return total < 20
    ? `${minutes}:${seconds.toFixed(1).padStart(4, "0")}`
    : `${minutes}:${String(Math.floor(seconds)).padStart(2, "0")}`;
}

function drawClocks() {
  const top = game.human === "white" ? "black" : "white";
  const bottom = game.human;
  const turn = game.chess.turn() === "w" ? "white" : "black";
  for (const [node, side] of [[el("clock-top"), top], [el("clock-bottom"), bottom]]) {
    node.querySelector(".who").textContent = side === game.human ? "You" : "drunkenmaster";
    node.querySelector(".time").textContent = formatClock(game.clock[side]);
    node.classList.toggle("active", side === turn && !game.over);
    node.classList.toggle("low", game.clock[side] < 20_000);
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
      finish(side === game.human ? "lost" : "won", "flag");
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

// ---------------------------------------------------------------- play

function onSquare(name) {
  if (!game || game.over || game.thinking) return;
  if (game.chess.turn() !== game.human[0]) return;

  const piece = game.chess.get(name);
  if (game.selected) {
    const legal = game.chess
      .moves({ square: game.selected, verbose: true })
      .find((m) => m.to === name);
    if (legal) {
      const promotion = legal.promotion ? askPromotion() : undefined;
      play(game.selected, name, promotion);
      game.selected = null;
      return;
    }
  }
  game.selected = piece && piece.color === game.human[0] ? name : null;
  drawBoard();
}

function askPromotion() {
  const answer = (prompt("Promote to q, r, b or n?", "q") || "q").toLowerCase();
  return ["q", "r", "b", "n"].includes(answer) ? answer : "q";
}

function play(from, to, promotion) {
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

async function engineMove() {
  const engine = game.human === "white" ? "black" : "white";
  game.thinking = true;
  el("status").textContent = "drunkenmaster is thinking…";
  try {
    const response = await fetch("/api/move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        start_fen: game.startFen,
        moves: game.moves,
        time_left_ms: Math.round(game.clock[engine]),
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    const move = game.chess.move(data.move);
    game.lastMove = move;
    game.moves.push(data.move);
    chargeClock(engine);
    el("engine-note").textContent =
      `last reply: ${data.san} in ${(data.thinking_ms / 1000).toFixed(1)}s, ` +
      `${data.nodes.toLocaleString()} positions`;
    drawBoard();
    drawMoves();
    drawClocks();
    game.thinking = false;
    if (!checkOver()) el("status").textContent = "Your move.";
  } catch (failure) {
    game.thinking = false;
    el("status").textContent = `The engine could not reply: ${failure.message}`;
  }
}

function finish(result, termination) {
  if (game.over) return;
  game.over = true;
  game.thinking = false;
  stopTicking();
  const words = { won: "You won", lost: "You lost", drawn: "Drawn" };
  el("status").textContent = `${words[result]} by ${termination.replace(/_/g, " ")}.`;
  el("resign").hidden = true;
  el("again").hidden = false;
  drawClocks();
  void saveGame(result, termination);
}

async function saveGame(result, termination) {
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
    moves: [], selected: null, lastMove: null,
    over: false, thinking: false, timer: null, tickFrom: performance.now(),
  };
  el("resign").hidden = false;
  el("again").hidden = true;
  el("engine-note").textContent = "";
  el("status").textContent = human === "white" ? "Your move." : "drunkenmaster is thinking…";
  show("game");
  drawBoard();
  drawMoves();
  drawClocks();
  startTicking();
  if (human === "black") void engineMove();
});

el("resign").addEventListener("click", () => finish("lost", "resignation"));
el("again").addEventListener("click", () => {
  game = null;
  location.hash = "#/";
  show("setup");
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
        const when = g.finished_at
          ? new Date(g.finished_at * 1000).toLocaleString()
          : "";
        card.innerHTML =
          `<h3><span class="tag ${g.result}">${g.result}</span> as ${g.colour}</h3>` +
          `<p>${g.time_control || ""} · ${g.plies || 0} plies · ` +
          `${(g.termination || "").replace(/_/g, " ")} · ${when}</p>` +
          `<pre class="pgn"></pre>`;
        card.querySelector(".pgn").textContent = g.pgn || "(no moves recorded)";
        return card;
      }),
    );
  } catch (failure) {
    list.textContent = `Could not load that folder: ${failure.message}`;
  }
}

// ---------------------------------------------------------------- boot

el("name").value = localStorage.getItem("chessathon:name") || "";
fetch("/api/games")
  .then((r) => r.json())
  .then((d) => {
    if (!d.persistent) {
      el("storage-note").textContent =
        "Storage is not configured, so player folders will empty on restart.";
    }
  })
  .catch(() => {});
void route();
