"""The POST /intent queue cap and its Discord-shaped rate-limit headers.

The tick applies EVERY pending intent in one pass (`WHERE status='pending' ORDER BY id`), so an
unbounded per-agent queue is a world-freeze button: one agent enqueueing faster than the world drains
stretches a single tick without bound. `submit_intent` caps the per-agent backlog. These assert the
two properties a later "simplification" could quietly drop, and need no fastapi/psycopg2/database:

  1. the INSERT stays CONDITIONAL on the pending count — a plain unconditional INSERT lets two
     concurrent submissions both claim the same last slot;
  2. the refusal is a real 429 carrying the full header set — a dropped connection or a 500 leaves a
     Discord-style client with nothing to back off on, which is exactly what was asked for.

Run: python3 -m pytest tests/test_intent_ratelimit.py
"""
import ast
import os
import re

_HERE = os.path.dirname(__file__)
APP = os.path.join(_HERE, "..", "server", "app.py")


def _src():
    return open(APP, encoding="utf-8").read()


def _submit_intent_src():
    src = _src()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "submit_intent")
    return ast.get_source_segment(src, fn)


def test_cap_is_env_overridable_and_sane():
    m = re.search(r'INTENT_PENDING_CAP\s*=\s*int\(os\.environ\.get\("INTENT_PENDING_CAP",\s*"(\d+)"\)\)', _src())
    assert m, "INTENT_PENDING_CAP must stay an env-overridable constant"
    cap = int(m.group(1))
    # 10 per tick was the busiest real agent's peak (measured live 2026-09-06 over 5428 intents).
    # Keep several times that: the cap is a runaway guard, not a gameplay throttle.
    assert 20 <= cap <= 200, f"cap {cap} is outside the sane band"


def test_insert_is_conditional_on_the_pending_count():
    body = _submit_intent_src()
    assert re.search(r"INSERT INTO intents.*?SELECT", body, re.S), "the insert must be INSERT ... SELECT"
    assert "status='pending'" in body and "< %s" in body, "the insert must be gated on the pending count"
    assert "VALUES(%s,%s,%s,%s)" not in body, "an unconditional INSERT reopens the last-slot race"


def test_refusal_is_a_429_with_the_full_header_set():
    body = _submit_intent_src()
    assert "429" in body, "over the cap must be a real 429, never a drop"
    for h in ("Retry-After", "X-RateLimit-Global", "X-RateLimit-Limit",
              "X-RateLimit-Remaining", "X-RateLimit-Reset-After"):
        assert h in body, f"missing {h} — a Discord-style client needs the whole set"


def test_limit_and_scope_are_documented_for_agents():
    doc = open(os.path.join(_HERE, "..", "AGENTS.md"), encoding="utf-8").read()
    assert "Rate limit" in doc, "AGENTS.md must document the limit"
    assert "per agent" in doc and "429" in doc, "it must state the scope and the refusal code"
