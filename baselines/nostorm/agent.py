"""Development-only wrapper: the root agent with the flank-storm bias off, for A/B games."""

import importlib.util
import pathlib

_source = pathlib.Path(__file__).resolve().parents[2] / "agent.py"
_spec = importlib.util.spec_from_file_location("root_engine_nostorm", _source)
assert _spec is not None and _spec.loader is not None
_engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_engine)
_engine.FLANK_STORM_BONUS = 0.0

get_move = _engine.get_move
