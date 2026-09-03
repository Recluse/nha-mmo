#!/usr/bin/env python3
"""Determinism coverage for the ENGINE HOT PATHS — the paths the published fingerprint cannot see.

Why this file exists (audit F15 follow-up, 2026-09-03). `tests/test_determinism.py::_seed_rich` inserts ENTITIES
ONLY — never a row in `intents`. So across all 40 of its ticks the intent SELECT returns nothing and these paths
never execute:

    apply_intent's deposit scan | gather | explode / tick_bombs | _los_blocked | _relation
    | roam_autonomous | _node_fortune

CHAIN_FINGERPRINT 9922767f180849f0 therefore comes back GREEN whether those paths are correct or deliberately
sabotaged. test_expansion_determinism does drive apply_intent, but its miners are on Mars and take the BODY_MINE
branch, so the surface deposit scan is still uncovered.

This harness queues real INTENTS that force each of those paths, and pins its own HOTPATH_FINGERPRINT. The
published 9922767f180849f0 is deliberately NOT re-baselined — it is cited as an invariant in WORLD-SCALE.md,
SEASON5-EXPANSION.md, server/app.py and engine.py, and moving it would turn "the chain is unchanged" into
"the chain is whatever we just measured".

HOTPATH_FINGERPRINT = 740cf6538dd9052f  (measured 2026-09-03, engine at commit f1d24da)

Gate for any hot-path change: all THREE fingerprints byte-identical (this one, the published one, expansion's).
Needs the same throwaway Postgres as the other determinism tests; SKIPS if unreachable.
"""
import atexit
import os
import sys
import pytest

_HOST = "host=127.0.0.1 port=15432 user=nhamoo"
_ADMIN_DSN = _HOST + " dbname=nhamoo"
_TEST_DB = "nha_test_h%d" % os.getpid()
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


def _run_once(n=8):
    """Seed a world that FORCES every hot path, then tick. Returns (chain, applied_count, pending_count)."""
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
    cur.execute("INSERT INTO entities(type,x,y,attrs) VALUES('market',0,0,%s)", (Json({"last": {}}),))

    def ent(tp, x, y, owner=None, attrs=None, buffers=None):
        cur.execute("INSERT INTO entities(type,x,y,owner,buffers,attrs) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
                    (tp, x, y, owner, Json(buffers or {}), Json(attrs or {})))
        return cur.fetchone()[0]

    def agent(name, x, y, buffers=None, extra=None):
        a = {"name": name, "hp": 100, "hp_max": 100, "born": -100000}   # past PROTECT_AGE so attack/steal are reachable
        a.update(extra or {})
        return ent("agent", x, y, None, a, buffers or {})

    # --- deposit scan: SAME-CELL stacking forces a constant-distance tie (the tie-break must be reproduced),
    # plus an in-reach hit, a far one for the "nearest ... is N cells away" rejection string, and a plant that
    # CREATES a deposit mid-loop (it must lose every tie it enters late).
    for i in range(4):
        ent("deposit", 6, 6, None, {"resource": "wood", "amount": 20 + i, "gen_seed": "42", "biome": "forest"})
    ent("deposit", 7, 6, None, {"resource": "copper", "amount": 30, "gen_seed": "42", "biome": "mountain"})
    ent("deposit", 5, 7, None, {"resource": "herb", "amount": 12, "gen_seed": "42", "biome": "plains"})
    ent("deposit", 60, 60, None, {"resource": "silicon", "amount": 9, "gen_seed": "42", "biome": "mountain"})
    chopper = agent("chopper", 5, 6, {"metal": 20, "wood": 5, "credits": 100})
    farmer = agent("farmer", 5, 7, {"herb": 4, "wood": 30, "credits": 100})
    faraway = agent("faraway", 90, 90, {"credits": 50})

    # --- LOS: two shooters with a structure between them; a third builds another box the same tick.
    shooter = agent("shooter", 5, 20, {"kinetic_gun": 1, "slug": 10, "credits": 50})
    victim = agent("victim", 5, 24, {"credits": 50})
    ent("structure", 5, 22, None, {"shape": "box", "hp": 50, "hp_max": 50})
    builder = agent("builder", 9, 20, {"metal": 60, "wood": 60, "composite": 10, "credits": 50})

    # --- relations: a duplicate/ghost pair is the ONLY case that distinguishes "first in scan order" from
    # "lowest id", so it is seeded on purpose.
    ally_a = agent("allyA", 12, 12, {"credits": 50})
    ally_b = agent("allyB", 13, 12, {"credits": 50})
    ent("relation", 0, 0, None, {"a": ally_a, "b": ally_b, "state": "peace"})

    # --- explode / tick_bombs: a live bomb sitting on top of deposits and next to agents.
    bomb_owner = agent("bomber", 40, 40, {"credits": 50})
    ent("deposit", 41, 40, None, {"resource": "wood", "amount": 15, "gen_seed": "42", "biome": "forest"})
    ent("bomb", 41, 41, bomb_owner, {"fuse": 1, "owner": bomb_owner})

    # --- roam_autonomous: an autonomous vehicle owned by an agent, parked on a deposit.
    driver = agent("driver", 50, 50, {"metal": 40, "oil": 20, "credits": 50})
    ent("deposit", 50, 50, None, {"resource": "copper", "amount": 40, "gen_seed": "42", "biome": "mountain"})
    ent("vehicle", 50, 50, driver, {"name": "rover", "controllable": True, "drives": True,
                                    "v_ground": 12, "mass": 100, "hp": 100, "hp_max": 100, "gear": 1})
    conn.commit()

    def intent(aid, verb, args):
        cur.execute("INSERT INTO intents(agent,verb,args,status,created) VALUES(%s,%s,%s,'pending',0)",
                    (aid, verb, Json(args)))

    # tick 1 — the deposit scan in every shape it has
    intent(chopper, "chop", {"n": 2})                    # tie among 4 same-cell wood deposits
    intent(farmer, "gather", {"n": 2})                   # plant-class scan
    intent(farmer, "plant", {})                          # CREATES a deposit mid-loop
    intent(faraway, "chop", {"n": 1})                    # far -> "nearest tree is N cells away"
    intent(faraway, "mine", {"resource": "silicon"})     # far, explicit resource -> its own rejection string
    intent(chopper, "gather", {"n": 0})                  # n<1 guard must stay first
    intent(shooter, "attack", {"target": victim})        # _los_blocked with a wall between
    intent(builder, "construct", {"shape": "box"})       # a structure created the same tick
    intent(ally_a, "ally", {"to": ally_b})               # duplicate relation row for an existing pair
    intent(driver, "deploy", {})                         # arms roam_autonomous
    conn.commit()
    engine.tick(conn); conn.commit()

    # tick 2 — mine after the world moved; chop again so the freshly planted deposit competes
    intent(chopper, "chop", {"n": 1})
    intent(farmer, "mine", {"n": 2})
    intent(ally_a, "declare_war", {"to": ally_b})
    conn.commit()
    engine.tick(conn); conn.commit()

    for _ in range(max(0, n - 2)):                       # let bombs detonate, roam run, relations settle
        engine.tick(conn); conn.commit()

    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT tick, hash FROM tick_hashes ORDER BY tick")
    chain = [(r["tick"], r["hash"]) for r in cur.fetchall()]
    cur.execute("SELECT count(*) c FROM intents WHERE status='applied'"); applied = cur.fetchone()["c"]
    cur.execute("SELECT count(*) c FROM intents WHERE status='pending'"); pending = cur.fetchone()["c"]
    # PROOF OF COVERAGE, not a row count: each probe is the observable signature of one hot path actually
    # executing. A seed that silently stops reaching a path (a rule change, a recipe change, newbie protection)
    # would otherwise leave this file as blind as the one it was written to replace.
    cur.execute("SELECT string_agg(COALESCE(result,''), ' | ') s FROM intents")
    res = cur.fetchone()["s"] or ""
    cur.execute("SELECT COALESCE(string_agg(DISTINCT kind, ','), '') k FROM events")
    kinds = set((cur.fetchone()["k"] or "").split(","))
    cur.execute("SELECT count(*) c FROM entities WHERE type='vehicle' AND COALESCE((attrs->>'autonomous')::bool,false)")
    autos = cur.fetchone()["c"]
    covered = {
        "deposit_scan_hit":   "chopped" in res and "mined" in res,
        "gather_scan_hit":    "gathered" in res,
        "global_fallback":    "nearest tree is" in res and "nearest deposit is" in res,
        "plant_creates_dep":  "planted a tree" in res,
        "los_blocked":        "no line of sight" in res,
        "structure_built":    "built box" in res,
        "relation_written":   "war" in kinds,
        "roam_armed":         autos >= 1,
        "explode_ran":        "explosion" in kinds,
    }
    conn.close()
    return chain, applied, pending, covered


@pytest.mark.integration
def test_hotpath_chain_is_deterministic():
    """The hot paths replay identically — and the coverage cannot silently regress to zero."""
    _connect_or_skip()
    _recreate_test_db(); chain1, applied, pending, covered = _run_once()
    _recreate_test_db(); chain2, _, _, _ = _run_once()
    # coverage guard FIRST: a blind test that passes is worse than a failing one.
    missing = sorted(k for k, v in covered.items() if not v)
    assert not missing, f"hot-path coverage REGRESSED — these paths no longer execute: {missing}"
    assert pending == 0, f"{pending} intents never applied — the hot paths were not exercised"
    assert chain1 == chain2, "NON-DETERMINISTIC: the hot paths produced different tick_hash chains"
    assert len({h for _, h in chain1}) > 1, "world never changed — the seed does not exercise the tick"


if __name__ == "__main__":
    import hashlib
    if "--chain" in sys.argv:
        import json
        _recreate_test_db()
        print(json.dumps(_run_once()[0]))
        sys.exit(0)
    _connect_or_skip()
    _recreate_test_db(); c1, ap, pe, cov = _run_once()
    _recreate_test_db(); c2, _, _, _ = _run_once()
    fp = hashlib.sha256("|".join(f"{t}:{h}" for t, h in c1).encode()).hexdigest()[:16]
    print(f"deterministic: {c1 == c2} | applied: {ap} | pending: {pe} | HOTPATH_FINGERPRINT: {fp}")
    print("coverage:", {k: ("ok" if v else "MISSING") for k, v in cov.items()})
