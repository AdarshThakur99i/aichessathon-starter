"""Development-only wrapper: the root agent with the decisive-advantage exit disabled.

Isolates the depth ceiling from the early exit, so an A/B can attribute a result to one or
the other instead of to both at once.
"""

import importlib.util
import pathlib

_source = pathlib.Path(__file__).resolve().parents[2] / "agent.py"
_spec = importlib.util.spec_from_file_location("root_engine_noexit", _source)
assert _spec is not None and _spec.loader is not None
_engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_engine)
_engine.DECISIVE_ADVANTAGE = float("inf")

get_move = _engine.get_move
