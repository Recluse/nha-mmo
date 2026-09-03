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
import atexit
import os
import sys
import pytest

_HOST = "host=127.0.0.1 port=15432 user=nhamoo"
_ADMIN_DSN = _HOST + " dbname=nhamoo"
# PER-PROCESS test DB. It used to be a single shared `nha_test`, and _recreate_test_db() below runs
# pg_terminate_backend over EVERY connection to it — so two concurrent runs (e.g. a developer running the suite
# locally while CI runs the same job against the same cluster Postgres) killed each other's connections mid-tick
# and both failed with "server closed the connection unexpectedly". Observed for real on 2026-09-03. The DB name
# never enters the hash chain, so scoping it per process is behaviour-neutral.
_TEST_DB = "nha_test_%d" % os.getpid()
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
    """Best-effort teardown so per-process DBs don't accumulate."""
    try:
        import psycopg2
        c = psycopg2.connect(_ADMIN_DSN, connect_timeout=3); c.autocommit = True; cur = c.cursor()
        cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()", (_TEST_DB,))
        cur.execute("DROP DATABASE IF EXISTS " + _TEST_DB)
        c.close()
    except Exception:
        pass


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
        sys.modules.pop(m, None)                       # fresh import → carried tick state reset
    import engine
    engine._STATE = engine._TickState()                # explicit fresh carried state (structural reset — audit #15)
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


def _run_in_subprocess(n, hashseed):
    """Run the SAME replay in a fresh interpreter under an explicit PYTHONHASHSEED, returning the chain as JSON.

    Why this exists: both replays used to run in ONE interpreter, so str/bytes hash randomisation was IDENTICAL
    for both by construction — the equality assertion literally could not observe the class of bug it exists to
    catch (a set/dict iteration order leaking into hashed state). Varying the seed across processes makes that
    class reachable (audit 2026-09-03)."""
    import json
    import subprocess
    env = dict(os.environ, PYTHONHASHSEED=str(hashseed))
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "--chain", str(n)],
                       capture_output=True, text=True, env=env, timeout=600)
    if r.returncode != 0:
        raise AssertionError("subprocess replay failed:\n" + (r.stdout or "")[-2000:] + (r.stderr or "")[-2000:])
    return [tuple(x) for x in json.loads(r.stdout.strip().splitlines()[-1])]


@pytest.mark.integration
def test_tick_chain_is_deterministic_and_evolving():
    """Same seed → identical tick_hash chain (replay-safe), and the chain actually changes tick-to-tick
    (so the maintenance systems are genuinely exercised, not a static no-op world).

    The second replay runs in a SEPARATE interpreter with a DIFFERENT PYTHONHASHSEED, so this also proves the
    chain does not depend on Python's per-process string hash randomisation."""
    _connect_or_skip()
    N = 40
    _recreate_test_db(); chain1 = _run_once(N)
    chain2 = _run_in_subprocess(N, hashseed=12345)     # different interpreter AND different hash seed
    assert len(chain1) == N, f"expected {N} ticks, got {len(chain1)}"
    assert chain1 == chain2, ("NON-DETERMINISTIC: the same seed produced different tick_hash chains across "
                              "processes — most likely a set/dict iteration order reaching hashed state")
    assert len({h for _, h in chain1}) > 1, "world never changed — seed too static to exercise the tick systems"


if __name__ == "__main__":
    # `--chain N` is the subprocess entry point used by the cross-process determinism check: seed a fresh DB,
    # run N ticks, print the chain as JSON on the last stdout line. Kept machine-readable and quiet.
    if "--chain" in sys.argv:
        import json
        _n = int(sys.argv[sys.argv.index("--chain") + 1])
        _recreate_test_db()
        print(json.dumps(_run_once(_n)))
        sys.exit(0)
    _connect_or_skip()
    import hashlib
    _recreate_test_db(); c1 = _run_once(40)
    _recreate_test_db(); c2 = _run_once(40)
    fp = hashlib.sha256("|".join(f"{t}:{h}" for t, h in c1).encode()).hexdigest()[:16]
    print("deterministic:", c1 == c2, "| evolved:", len({h for _, h in c1}) > 1, "| CHAIN_FINGERPRINT:", fp)
