"""Development-only wrapper: the root agent with a fixed depth cap, for A/B games."""

import importlib.util
import pathlib

_source = pathlib.Path(__file__).resolve().parents[2] / "agent.py"
_spec = importlib.util.spec_from_file_location("root_engine_d4", _source)
assert _spec is not None and _spec.loader is not None
_engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_engine)
_engine.MAX_SEARCH_DEPTH = 4

get_move = _engine.get_move
