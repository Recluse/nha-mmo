#!/usr/bin/env python3
"""SEASON 7 — the outer system, EXERCISED rather than merely shipped.

The recurring failure on this project is not broken logic, it is code that imports cleanly, passes every
test and has never been RUN on the path that matters: `warpgate` was absent from the construct allowlist
for a whole release (the feature was dead code while "verified" as deployed), `_needs_you` 500'd every
/observe on a missing import, and `settle_geese` froze the world on a name that does not exist. Tables of
constants are exactly the shape of change that looks obviously correct in a diff.

So this seeds a world, puts agents on Enceladus, Titan and Triton, and actually submits the intents:
fund a colony module, raise an ISRU extractor, run the methalox converter, and raise a warp gate. It
asserts on the ENGINE'S OWN reply string for each, so a typo in a body key, a module name or a producer
`bodies` tuple fails here instead of in front of the agents.

Same isolated-DB infra as the other integration tests; SKIPS when Postgres is unreachable.
"""
import atexit
import os
import sys

import pytest

_HOST = "host=127.0.0.1 port=15432 user=nhamoo"
_ADMIN_DSN = _HOST + " dbname=nhamoo"
_TEST_DB = "nha_test_outer%d" % os.getpid()
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


class _World:
    """A tiny expansion-era world you can drop agents onto and submit intents into."""

    def __init__(self):
        import psycopg2
        from psycopg2.extras import Json
        if _ENGINE_DIR not in sys.path:
            sys.path.insert(0, _ENGINE_DIR)
        for m in ("engine", "crafting", "vehicles", "worldgen", "play"):
            sys.modules.pop(m, None)
        import engine
        self.engine = engine
        engine._STATE = engine._TickState()
        self.conn = psycopg2.connect(_TEST_DSN)
        self.cur = self.conn.cursor()
        self.cur.execute(engine.SCHEMA)
        self.cur.execute("UPDATE world SET era='expansion' WHERE id=1")
        self.cur.execute("INSERT INTO entities(type,x,y,attrs) VALUES('market',0,0,%s)", (Json({"w": 156, "h": 156}),))
        self.conn.commit()
        self._Json = Json

    def agent(self, name, body=None, hold=None, **attrs):
        at = {"name": name, "hp": 10, "hp_max": 10, "born": 0}
        if body:
            at.update({"in_space": True, "altitude": 600, "at_body": body})
        at.update(attrs)
        self.cur.execute("INSERT INTO entities(type,x,y,attrs) VALUES('agent',0,0,%s) RETURNING id", (self._Json(at),))
        aid = self.cur.fetchone()[0]
        if hold:
            self.cur.execute("UPDATE entities SET buffers=%s WHERE id=%s", (self._Json(hold), aid))
        self.conn.commit()
        return aid

    def do(self, aid, verb, args):
        """Submit one intent, tick, and return the engine's own (status, result) for it."""
        self.cur.execute("INSERT INTO intents(agent,verb,args,status,created) VALUES(%s,%s,%s,'pending',0) RETURNING id",
                         (aid, verb, self._Json(args)))
        iid = self.cur.fetchone()[0]
        self.conn.commit()
        self.engine.tick(self.conn)
        self.conn.commit()
        self.cur.execute("SELECT status, result FROM intents WHERE id=%s", (iid,))
        return self.cur.fetchone()

    def hold(self, aid, res):
        self.cur.execute("SELECT COALESCE((buffers->>%s)::int, 0) FROM entities WHERE id=%s", (res, aid))
        return self.cur.fetchone()[0]

    def ticks(self, n):
        for _ in range(n):
            self.engine.tick(self.conn)
            self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


@pytest.fixture()
def world():
    _connect_or_skip()
    _recreate_test_db()
    w = _World()
    yield w
    w.close()


@pytest.mark.integration
def test_every_outer_colony_module_can_actually_be_funded(world):
    """One agent, one module, on each of the three bodies — funded for real, not merely tabulated."""
    eng = world.engine
    for body in ("enceladus", "titan", "triton"):
        mods = eng.COLONY_MODULES[body]
        module = sorted(mods)[0]
        need = mods[module]["need"]
        aid = world.agent("f_" + body, body=body, hold={r: v for r, v in need.items()})
        status, result = world.do(aid, "construct", {"shape": "colony", "body": body, "module": module})
        assert status == "applied", f"{body}/{module} rejected: {result}"
        assert eng.COLONY_LABEL[body] in result, result


@pytest.mark.integration
def test_every_outer_producer_builds_where_its_table_says_it_can(world):
    """A producer whose `bodies` tuple names a body the engine cannot reach is silently dead. Build each one."""
    eng = world.engine
    outer = {k: s for k, s in eng.PRODUCERS.items()
             if set(s["bodies"]) & {"enceladus", "titan", "triton"}}
    assert outer, "Season 7b added no outer-system producers — table missing?"
    for kind, spec in sorted(outer.items()):
        body = sorted(set(spec["bodies"]) & {"enceladus", "titan", "triton"})[0]
        aid = world.agent("p_" + kind, body=body, hold=dict(spec["cost"]))
        status, result = world.do(aid, "construct", {"shape": "extractor", "kind": kind})
        assert status == "applied", f"{kind} on {body} rejected: {result}"
        assert spec["label"] in result, result


@pytest.mark.integration
def test_the_methalox_loop_actually_produces_fuel(world):
    """THE SPINE OF THE SEASON. Titan methane + Enceladus clean_ice must come out as methalox, or the outer
    system is a sightseeing trip: methalox was previously reachable only through the Mars Sabatier."""
    eng = world.engine
    spec = eng.PRODUCERS["methalox_plant"]
    hold = dict(spec["cost"])
    for r, q in spec["consume"].items():
        hold[r] = hold.get(r, 0) + q * 6          # feedstock for several firings
    aid = world.agent("refiner", body="titan", hold=hold)
    status, result = world.do(aid, "construct", {"shape": "extractor", "kind": "methalox_plant"})
    assert status == "applied", result
    before = world.hold(aid, "methalox")
    world.ticks(spec["period"] * 3 + 2)           # producers fire on their own id-phase — give it a few periods
    after = world.hold(aid, "methalox")
    assert after > before, (f"the Methalox Plant produced nothing in {spec['period'] * 3 + 2} ticks "
                            f"(methane {world.hold(aid, 'methane')}, clean_ice {world.hold(aid, 'clean_ice')})")


@pytest.mark.integration
def test_a_gate_needs_a_finished_colony_and_then_stands(world):
    """The gate path on a body the engine only learned about this season: refused without a finished colony,
    accepted once one stands. WARP_NEED is generated from WARP_BODY_EXOTIC, so a missing body key here is a
    KeyError in front of an agent."""
    eng = world.engine
    body = "titan"
    need = eng.WARP_NEED[body]
    aid = world.agent("gatewright", body=body, hold=dict(need))
    status, result = world.do(aid, "construct", {"shape": "warpgate", "place": body})
    assert status == "rejected", f"a gate rose with no colony under it: {result}"

    # stand a COMPLETE colony on Titan the way the engine itself records one, then try again
    world.cur.execute(
        "INSERT INTO entities(type,x,y,owner,attrs) VALUES('structure',78,78,%s,%s)",
        (aid, world._Json({"shape": "colony", "body": body, "name": eng.COLONY_LABEL[body],
                           "modules": {k: {"have": {}, "contrib": {str(aid): {"metal": 1}}, "complete": True}
                                       for k in eng.COLONY_MODULES[body]},
                           "ctotal": {str(aid): 1}, "complete": True, "alt": eng.SKY_TOP,
                           "hp": 100, "hp_max": 100})))
    world.conn.commit()
    status, result = world.do(aid, "construct", {"shape": "warpgate", "place": body})
    assert status == "applied", f"gate rejected on a finished colony: {result}"
    assert eng.BODY_LABEL[body] in result, result


@pytest.mark.integration
def test_the_outer_bodies_demand_landing_gear(world):
    """Every body with GROUND needs gear on the ship; Venus alone is an aerostat. The old hand-written list
    let Enceladus, Titan and Triton be landed on gearless from the day they existed."""
    from psycopg2.extras import Json
    eng = world.engine
    aid = world.agent("pilot", in_space=True, altitude=600)
    world.cur.execute("UPDATE entities SET buffers=%s WHERE id=%s", (Json({"cryo_fuel": 900, "thermal_core": 2, "heat_shield": 2}), aid))
    world.cur.execute("INSERT INTO entities(type,x,y,owner,attrs) VALUES('vehicle',0,0,%s,%s)",
                      (aid, Json({"name": "gearless", "controllable": True, "flies": True, "orbital_engine": True,
                                  "mass": 300, "thrust": 1600, "fuel_cap": 900, "hp": 100, "hp_max": 100})))   # TWR 1.33 > Titan's 0.8: eligible on every axis EXCEPT gear
    world.conn.commit()
    status, result = world.do(aid, "depart", {"dest": "titan"})
    assert status == "rejected" and "LANDING GEAR" in result, f"gearless ship cleared for Titan: {result}"
