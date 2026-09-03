#!/usr/bin/env python3
"""Economy CONSERVATION guard — the invariant the determinism suite structurally cannot see.

Both determinism tests assert only that two replays AGREE. A bug that creates or destroys value on every run
agrees with itself perfectly, so "deterministic but wrong" is invisible to them (audit 2026-09-03, F21).

What this asserts is deliberately NOT "the world total never changes" — that would be false by design: mining
creates resources and the depot is an explicit source/sink (`sell` mints credits, `buy` burns them, and the
converter is commented "THE SINK"). A test asserting global conservation would fail on correct behaviour, which
is worse than no test at all.

The real invariant is ESCROW NEUTRALITY:
  1. round trip — every verb that escrows value up front (order / trade / contract / bounty) must refund EXACTLY
     the amount it debited when the posting is cancelled, revoked or declined. Net effect: zero.
  2. transfer — when two orders MATCH, value moves between agents but the PAIR TOTAL is unchanged.
A leak in either direction (an agent silently gaining or losing on a cancelled posting) is a real economy bug.

Needs the same throwaway Postgres as tests/test_determinism.py; SKIPS if unreachable.
"""
import atexit
import os
import sys
import pytest

_HOST = "host=127.0.0.1 port=15432 user=nhamoo"
_ADMIN_DSN = _HOST + " dbname=nhamoo"
_TEST_DB = "nha_test_e%d" % os.getpid()          # per-process: concurrent runs must not drop each other's DB
_TEST_DSN = _HOST + " dbname=" + _TEST_DB
_ENGINE_DIR = os.path.join(os.path.dirname(__file__), "..", "engine")


def _connect_or_skip():
    try:
        import psycopg2
    except Exception:
        pytest.skip("psycopg2 not installed")
    try:
        psycopg2.connect(_ADMIN_DSN, connect_timeout=3).close()
    except Exception:
        pytest.skip("no Postgres on 127.0.0.1:15432 (run: kubectl -n nha-mmo port-forward deploy/postgres 15432:5432)")


def _recreate_test_db():
    import psycopg2
    c = psycopg2.connect(_ADMIN_DSN); c.autocommit = True; cur = c.cursor()
    cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()", (_TEST_DB,))
    cur.execute("DROP DATABASE IF EXISTS " + _TEST_DB)
    cur.execute("CREATE DATABASE " + _TEST_DB)
    c.close()
    atexit.register(_drop_test_db)


def _drop_test_db():
    try:
        import psycopg2
        c = psycopg2.connect(_ADMIN_DSN, connect_timeout=3); c.autocommit = True; cur = c.cursor()
        cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()", (_TEST_DB,))
        cur.execute("DROP DATABASE IF EXISTS " + _TEST_DB)
        c.close()
    except Exception:
        pass


def _engine():
    if _ENGINE_DIR not in sys.path:
        sys.path.insert(0, _ENGINE_DIR)
    for m in ("engine", "crafting", "vehicles", "worldgen"):
        sys.modules.pop(m, None)
    import engine
    engine._STATE = engine._TickState()
    return engine


_START = {"metal": 100, "wood": 100, "credits": 1000}


def _mkworld():
    """A deliberately INERT world: two funded agents and the market entity, and nothing else. No deposits, no
    vehicles, no structures — so no source or sink can move a buffer behind the test's back and every delta seen
    here is attributable to the escrow path under test."""
    import psycopg2
    from psycopg2.extras import Json
    eng = _engine()
    conn = psycopg2.connect(_TEST_DSN)
    cur = conn.cursor(); cur.execute(eng.SCHEMA); conn.commit()
    cur.execute("INSERT INTO world(id,tick) VALUES(1,0) ON CONFLICT (id) DO NOTHING")
    cur.execute("INSERT INTO entities(type,x,y,attrs) VALUES('market',0,0,%s)", (Json({"last": {}}),))
    ids = []
    for i in range(2):
        cur.execute("INSERT INTO entities(type,x,y,buffers,attrs) VALUES('agent',%s,%s,%s,%s) RETURNING id",
                    (i, 0, Json(dict(_START)), Json({"name": "eco%d" % i, "hp": 100, "hp_max": 100, "born": 0})))
        ids.append(cur.fetchone()[0])
    conn.commit()
    return eng, conn, ids


def _buffers(conn, aid):
    from psycopg2.extras import RealDictCursor
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT buffers FROM entities WHERE id=%s", (aid,))
    return dict(cur.fetchone()["buffers"] or {})


def _submit(conn, aid, verb, args):
    from psycopg2.extras import Json, RealDictCursor
    cur = conn.cursor()
    cur.execute("INSERT INTO intents(agent,verb,args,status) VALUES(%s,%s,%s,'pending') RETURNING id",
                (aid, verb, Json(args)))
    iid = cur.fetchone()[0]; conn.commit()
    return iid


def _result(conn, iid):
    from psycopg2.extras import RealDictCursor
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT status, result FROM intents WHERE id=%s", (iid,))
    r = cur.fetchone()
    return (r["status"], r["result"] or "")


def _tick(eng, conn, n=1):
    for _ in range(n):
        eng.tick(conn)


def _total(bufs):
    """Sum every resource across the given buffer dicts — the pair total for the transfer case."""
    out = {}
    for b in bufs:
        for k, v in b.items():
            out[k] = out.get(k, 0) + int(v)
    return out


@pytest.mark.integration
def test_escrow_round_trips_are_value_neutral():
    """post-then-cancel / propose-then-revoke must leave the agent EXACTLY as it started."""
    _connect_or_skip(); _recreate_test_db()
    eng, conn, (a1, a2) = _mkworld()
    _tick(eng, conn)                                  # settle tick 1 before measuring
    before = _buffers(conn, a1)

    # 1. SELL order escrows the resource; cancelling must hand back exactly that resource.
    i = _submit(conn, a1, "order", {"side": "sell", "resource": "metal", "qty": 25, "price": 7})
    _tick(eng, conn)
    st, res = _result(conn, i)
    assert st == "applied", f"sell order rejected: {res}"
    mid = _buffers(conn, a1)
    assert mid.get("metal") == before.get("metal") - 25, "sell order did not escrow the resource"
    from psycopg2.extras import RealDictCursor
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id FROM market_orders WHERE agent=%s AND status='open' ORDER BY id DESC LIMIT 1", (a1,))
    oid = cur.fetchone()["id"]
    _submit(conn, a1, "cancel", {"order_id": oid}); _tick(eng, conn)
    assert _buffers(conn, a1) == before, "SELL order round trip leaked value (post -> cancel is not neutral)"

    # 2. BUY order escrows credits (qty*price); cancelling must hand back exactly that.
    i = _submit(conn, a1, "order", {"side": "buy", "resource": "wood", "qty": 5, "price": 11})
    _tick(eng, conn)
    st, res = _result(conn, i)
    assert st == "applied", f"buy order rejected: {res}"
    assert _buffers(conn, a1).get("credits") == before.get("credits") - 55, "buy order did not escrow credits"
    cur.execute("SELECT id FROM market_orders WHERE agent=%s AND status='open' ORDER BY id DESC LIMIT 1", (a1,))
    oid = cur.fetchone()["id"]
    _submit(conn, a1, "cancel", {"order_id": oid}); _tick(eng, conn)
    assert _buffers(conn, a1) == before, "BUY order round trip leaked value (post -> cancel is not neutral)"

    # 3. CONTRACT escrows the reward; revoking must hand it back.
    i = _submit(conn, a1, "contract", {"reward": {"credits": 120}, "want": {"wood": 3}})
    _tick(eng, conn)
    st, res = _result(conn, i)
    if st == "applied":
        assert _buffers(conn, a1).get("credits") == before.get("credits") - 120, "contract did not escrow the reward"
        cur.execute("SELECT id FROM contracts WHERE poster=%s AND status='open' ORDER BY id DESC LIMIT 1", (a1,))
        row = cur.fetchone()
        _submit(conn, a1, "revoke", {"contract_id": row["id"]}); _tick(eng, conn)
        assert _buffers(conn, a1) == before, "CONTRACT round trip leaked value (post -> revoke is not neutral)"
    conn.close()


@pytest.mark.integration
def test_matched_orders_conserve_the_pair_total():
    """A matched buy/sell MOVES value between two agents; the pair total must not change."""
    _connect_or_skip(); _recreate_test_db()
    eng, conn, (a1, a2) = _mkworld()
    _tick(eng, conn)
    start = _total([_buffers(conn, a1), _buffers(conn, a2)])

    _submit(conn, a1, "order", {"side": "sell", "resource": "metal", "qty": 10, "price": 5})
    _submit(conn, a2, "order", {"side": "buy", "resource": "metal", "qty": 10, "price": 5})
    _tick(eng, conn, 3)                               # submit, then let match_market run

    end = _total([_buffers(conn, a1), _buffers(conn, a2)])
    # every resource the pair holds, PLUS anything still escrowed in their open orders, must equal the start
    cur = conn.cursor()
    cur.execute("SELECT side, resource, qty, price FROM market_orders WHERE agent IN (%s,%s) AND status='open'", (a1, a2))
    for side, resource, qty, price in cur.fetchall():
        k, amt = (resource, qty) if side == "sell" else ("credits", qty * price)
        end[k] = end.get(k, 0) + amt
    assert end == start, ("matched orders changed the PAIR TOTAL — value was created or destroyed by the match\n"
                          "  start: %r\n  end:   %r" % (start, end))
    conn.close()
