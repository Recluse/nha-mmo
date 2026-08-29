#!/usr/bin/env python3
"""EXPANSION-ERA determinism guard — CI-gated replay coverage for the Season-5 systems that the seed
determinism test (tests/test_determinism.py) does NOT exercise (it never leaves Earth-ground).

Seeds a small `era='expansion'` world that drives the NEW hashed paths in ONE tick pass, twice:
  • Mars body-mining DURING a dust storm (t<400) → the halved-harvest branch (backlog item 3),
  • co-op COLONY funding via construct{shape:'colony'} on Mars,
  • the interplanetary transit engine (depart → advance_transits),
  • a DOCK-then-DEPART agent → the Phase-6 clear_offworld path that also drops `docked_to`
    (the ultracode audit's one finding — pinned here so the behaviour is guarded going forward).
Asserts the two runs produce an IDENTICAL tick_hash chain (forward replay is deterministic) and that
the chain actually evolves. Same infra as test_determinism (isolated nha_test DB on 127.0.0.1:15432;
SKIPS if unreachable, so it never blocks a deploy).
"""
import os
import sys
import pytest

_HOST = "host=127.0.0.1 port=15432 user=nhamoo"
_ADMIN_DSN = _HOST + " dbname=nhamoo"
_TEST_DSN = _HOST + " dbname=nha_test"
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
    cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='nha_test' AND pid<>pg_backend_pid()")
    cur.execute("DROP DATABASE IF EXISTS nha_test")
    cur.execute("CREATE DATABASE nha_test")
    c.close()


def _run_once(n):
    import psycopg2
    from psycopg2.extras import Json, RealDictCursor
    if _ENGINE_DIR not in sys.path:
        sys.path.insert(0, _ENGINE_DIR)
    for m in ("engine", "crafting", "vehicles", "worldgen", "play"):
        sys.modules.pop(m, None)
    import engine
    engine._STATE = engine._TickState()
    conn = psycopg2.connect(_TEST_DSN)
    cur = conn.cursor(); cur.execute(engine.SCHEMA); conn.commit()
    cur.execute("UPDATE world SET era='expansion' WHERE id=1")
    cur.execute("INSERT INTO entities(type,x,y,attrs) VALUES('market',0,0,%s)", (Json({"w": 156, "h": 156}),))

    # three miners standing ON Mars (t<400 = dust storm → the halved-harvest branch); they mine + co-fund Ares Base.
    mars = []
    for i in range(3):
        cur.execute("INSERT INTO entities(type,x,y,attrs) VALUES('agent',%s,%s,%s) RETURNING id",
                    (i, i, Json({"name": f"m{i}", "hp": 10, "hp_max": 10, "born": 0,
                                 "in_space": True, "altitude": 600, "at_body": "mars"})))
        aid = cur.fetchone()[0]; mars.append(aid)
        cur.execute("UPDATE entities SET buffers=%s WHERE id=%s",
                    (Json({"mars_regolith": 300, "perchlorate": 120, "mars_ice": 120, "metal": 200}), aid))

    # a DOCK-then-DEPART pilot in Earth orbit, latched to an asteroid → exercises clear_offworld dropping docked_to.
    cur.execute("INSERT INTO entities(type,x,y,attrs) VALUES('asteroid',30,30,%s) RETURNING id",
                (Json({"resource": "iron", "amount": 5, "max": 10}),))
    ast_id = cur.fetchone()[0]
    cur.execute("INSERT INTO entities(type,x,y,attrs) VALUES('agent',30,30,%s) RETURNING id",
                (Json({"name": "pilot", "hp": 10, "hp_max": 10, "born": 0, "in_space": True,
                       "altitude": 400, "docked_to": ast_id}),))
    pilot = cur.fetchone()[0]
    cur.execute("UPDATE entities SET buffers=%s WHERE id=%s", (Json({"cryo_fuel": 400}), pilot))
    cur.execute("INSERT INTO entities(type,x,y,owner,attrs) VALUES('vehicle',30,30,%s,%s)",
                (pilot, Json({"name": "clipper", "controllable": True, "flies": True, "orbital_engine": True,
                              "mass": 300, "thrust": 900, "fuel_cap": 800, "gear": 1, "hp": 100, "hp_max": 100})))
    conn.commit()

    def intent(aid, verb, args):
        cur.execute("INSERT INTO intents(agent,verb,args,status,created) VALUES(%s,%s,%s,'pending',0)", (aid, verb, Json(args)))

    # t=1 pass: miners mine Mars (storm-halved) + the pilot departs while docked (clears docked_to)
    for aid in mars:
        intent(aid, "mine", {"n": 6})
    intent(pilot, "depart", {"dest": "deimos"})
    conn.commit()
    engine.tick(conn); conn.commit()

    # t=2 pass: miners fund the Ares Base landing_pad (co-op colony construct)
    for aid in mars:
        intent(aid, "construct", {"shape": "colony", "body": "mars", "module": "landing_pad"})
    conn.commit()
    engine.tick(conn); conn.commit()

    # a few more ticks so advance_transits + body_upkeep + producers run on the evolved state
    for _ in range(6):
        engine.tick(conn); conn.commit()

    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT tick, hash FROM tick_hashes ORDER BY tick")
    chain = [(r["tick"], r["hash"]) for r in cur.fetchall()]
    # surface the pilot's post-depart attrs so a regression on the docked_to clear is visible in -s output
    cur.execute("SELECT attrs->>'docked_to' dk, attrs->>'transit_to' tr FROM entities WHERE type='agent' AND attrs->>'name'='pilot'")
    pj = cur.fetchone()
    conn.close()
    return chain, pj


@pytest.mark.integration
def test_expansion_tick_chain_is_deterministic():
    """The Season-5 expansion paths (dust-storm mining, colony funding, transit, dock→depart) replay to an
    IDENTICAL tick_hash chain from the same seed, and the chain evolves."""
    _connect_or_skip()
    N = 8
    _recreate_test_db(); chain1, pj1 = _run_once(N)
    _recreate_test_db(); chain2, pj2 = _run_once(N)
    assert len(chain1) == N, f"expected {N} ticks, got {len(chain1)}"
    assert chain1 == chain2, "NON-DETERMINISTIC: identical expansion seed produced different tick_hash chains"
    assert len({h for _, h in chain1}) > 1, "world never changed — expansion seed too static"
    # the audit's docked_to path: a successful depart-while-docked must have UNDOCKED (docked_to cleared) and be in transit
    assert pj1 == pj2, "pilot post-depart state diverged between runs"
    if pj1["tr"] == "deimos":                     # depart succeeded → clear_offworld must have dropped docked_to
        assert pj1["dk"] is None, f"depart did not clear docked_to (audit regression): {pj1}"


if __name__ == "__main__":
    _connect_or_skip()
    import hashlib
    _recreate_test_db(); c1, p1 = _run_once(8)
    _recreate_test_db(); c2, p2 = _run_once(8)
    fp = hashlib.sha256("|".join(f"{t}:{h}" for t, h in c1).encode()).hexdigest()[:16]
    print("deterministic:", c1 == c2, "| evolved:", len({h for _, h in c1}) > 1,
          "| pilot:", p1, "| EXPANSION_FINGERPRINT:", fp)
