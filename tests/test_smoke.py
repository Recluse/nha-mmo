"""Import + contract smoke tests — cheap guards that the server module loads and the security-critical
input validation holds. Runs without a database (importing app does not connect; the pool is lazy)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
os.environ.setdefault("PG_DSN", "host=127.0.0.1 dbname=x user=x")

import app  # noqa: E402
from pydantic import ValidationError  # noqa: E402


def test_app_and_engine_import():
    assert app.app is not None
    assert app.engine is not None


def test_verb_validator_accepts_every_real_verb():
    # the full set the engine dispatches on — all must pass the door validator
    real = ("move mine chop gather combine build finalize launch land land_moon dock ride deploy construct "
            "sell buy order cancel trade heal attack steal collect plant arm detonate attune say tell assist "
            "ally unally accept_ally declare_war make_peace deposit").split()
    for v in real:
        assert app.IntentIn(agent=1, verb=v).verb == v


@pytest.mark.parametrize("bad", [
    "<img src=x onerror=alert(1)>",   # XSS payload
    "DROP TABLE",                      # space + uppercase
    "MOVE",                            # uppercase
    "a" * 41,                          # too long
    "",                                # empty
    "move;rm",                         # punctuation
])
def test_verb_validator_rejects_junk(bad):
    with pytest.raises(ValidationError):
        app.IntentIn(agent=1, verb=bad)
