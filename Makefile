SHELL := /bin/bash

.PHONY: setup play arena zip gate serve harvest learn

setup:
	uv sync

play:
	uv run python -m harness.play --white . --black baselines/greedy $(if $(FEN),--fen "$(FEN)")

arena:
	uv run python -m harness.arena --opponent baselines/greedy --games 20

zip:
	uv run python -m harness.package

serve:
	uv run python -m tools.serve $(if $(PORT),--port $(PORT))

harvest:
	uv run python -m tools.harvest

# The offline learning loop. STOCKFISH labels the positions; it never ships.
learn: harvest
	@test -n "$(STOCKFISH)" || { echo "usage: make learn STOCKFISH=/path/to/stockfish"; exit 1; }
	uv run python -m tools.tune_weights --stockfish "$(STOCKFISH)" --pgn games/corpus.pgn --positions $(or $(POSITIONS),20000) --ridge 1.0 --anchor-material

gate:
	uv run ruff check .
	uv run mypy
	uv run python -m harness.arena --opponent baselines/random --games 2 --base-ms 5000
