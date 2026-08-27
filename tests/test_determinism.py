#!/usr/bin/env python3
"""Engine determinism harness — the replay/equivalence guard that WORLD-SCALE.md calls the prerequisite
for any tick-loop optimization (P0/P1/P2). It seeds a small-but-RICH world (deposits that regrow, hurt &
downed agents, a war relation, asteroids — so every per-tick maintenance system does real work and the
tick_hash chain evolves), runs N ticks TWICE from the same seed, and asserts an IDENTICAL chain.

Use it to prove a refactor is behaviour-preserving: capture the CHAIN_FINGERPRINT before your change,
then after — they must match (this is how P1's type-index was validated: fp 9922767f180849f0 unchanged).

Needs a Postgres reachable at 127.0.0.1:15432 (isolated throwaway DB `nha_test`, never touches prod
`nhamoo`). Locally:  kubectl -n nha-mmo port-forward deploy/postgres 15432:5432  &  then run pytest.
If no such Postgres is reachable (e.g. the CI shell runner), the test SKIPS rather than fails.
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


def _seed_rich(conn, engine):
    from psycopg2.extras import Json
    cur = conn.cursor()

    def ent(tp, x=0, y=0, owner=None, attrs=None):
        cur.execute("INSERT INTO entities(type,x,y,owner,attrs) VALUES(%s,%s,%s,%s,%s) RETURNING id",
                    (tp, x, y, owner, Json(attrs or {})))
        return cur.fetchone()[0]

    a_ids = []
    for i in range(6):
        a_ids.append(ent("agent", i, i * 2, attrs={"name": f"a{i}", "hp": 3 + i % 4, "hp_max": 10, "born": 0,
                          "notoriety": i % 3, "vigilance": (i + 1) % 3, "wanted_until": 4 if i == 0 else 0,
                          "robbed_recent": 1 if i == 1 else 0}))
    ent("agent", 20, 20, attrs={"name": "downed", "hp": 0, "hp_max": 10, "downed_until": 5, "born": 0})
    ent("relation", 0, 0, attrs={"a": a_ids[0], "b": a_ids[1], "state": "war", "war_combat_tick": 5})
    for i in range(25):
        ent("deposit", i % 40, (i * 7) % 40, attrs={"resource": "wood", "amount": i % 20, "gen_seed": "42", "biome": "forest"})
    for i in range(18):
        ent("deposit", (i * 5) % 40, (i * 3) % 40, attrs={"resource": "copper", "amount": i % 16, "gen_seed": "42", "biome": "mountain"})
    for i in range(12):
        ent("deposit", (i * 2) % 40, i % 40, attrs={"resource": "herb", "amount": i % 10, "gen_seed": "42", "biome": "plains"})
    for i in range(6):
        ent("asteroid", i * 3, i * 3, attrs={"resource": "iron", "amount": i, "max": 10})
    conn.commit()


def _run_once(n):
    import psycopg2
    from psycopg2.extras import RealDictCursor
    if _ENGINE_DIR not in sys.path:
        sys.path.insert(0, _ENGINE_DIR)
    for m in ("engine", "crafting", "vehicles", "worldgen"):
        sys.modules.pop(m, None)                       # fresh import → _WORLD globals reset
    import engine
    engine._WORLD = engine._WORLD_LOADED_TICK = None
    engine._WORLD_MAX_ID = 0
    conn = psycopg2.connect(_TEST_DSN)
    cur = conn.cursor(); cur.execute(engine.SCHEMA); conn.commit()
    engine.seed_demo(conn)
    _seed_rich(conn, engine)
    for _ in range(n):
        engine.tick(conn)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT tick, hash FROM tick_hashes ORDER BY tick")
    chain = [(r["tick"], r["hash"]) for r in cur.fetchall()]
    conn.close()
    return chain


def test_tick_chain_is_deterministic_and_evolving():
    """Same seed → identical tick_hash chain (replay-safe), and the chain actually changes tick-to-tick
    (so the maintenance systems are genuinely exercised, not a static no-op world)."""
    _connect_or_skip()
    N = 40
    _recreate_test_db(); chain1 = _run_once(N)
    _recreate_test_db(); chain2 = _run_once(N)
    assert len(chain1) == N, f"expected {N} ticks, got {len(chain1)}"
    assert chain1 == chain2, "NON-DETERMINISTIC: identical seed produced different tick_hash chains"
    assert len({h for _, h in chain1}) > 1, "world never changed — seed too static to exercise the tick systems"


if __name__ == "__main__":
    _connect_or_skip()
    import hashlib
    _recreate_test_db(); c1 = _run_once(40)
    _recreate_test_db(); c2 = _run_once(40)
    fp = hashlib.sha256("|".join(f"{t}:{h}" for t, h in c1).encode()).hexdigest()[:16]
    print("deterministic:", c1 == c2, "| evolved:", len({h for _, h in c1}) > 1, "| CHAIN_FINGERPRINT:", fp)
