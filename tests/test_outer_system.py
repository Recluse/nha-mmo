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
import json
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

    def forget_world(self):
        """Drop the engine's carried in-memory world so a direct DB edit is actually seen.

        The tick carries `ents` across ticks and only reloads every RELOAD_EVERY, which is exactly why an UPDATE
        issued from a test looked like it had no effect: the next tick wrote the stale in-memory attrs back over
        it. Same drop the engine performs itself after a failed tick."""
        st = self.engine._STATE
        st.world = None
        st.loaded_tick = None
        st.canon = {}
        st.dep_amt = {}

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

@pytest.mark.integration
def test_the_return_leg_never_needs_landing_gear(world):
    """The gear rule is about setting DOWN on ground. `depart{dest:'earth'}` ends in Earth ORBIT — you `land`
    separately afterwards — so it must not demand gear. Rewriting the rule as "every body except Venus" made the
    return leg demand it, which would have stranded anyone who flew to Venus in a gearless aerostat ship."""
    from psycopg2.extras import Json
    aid = world.agent("homebound", body="venus")
    world.cur.execute("UPDATE entities SET buffers=%s WHERE id=%s", (Json({"cryo_fuel": 900}), aid))
    world.cur.execute("INSERT INTO entities(type,x,y,owner,attrs) VALUES('vehicle',0,0,%s,%s)",
                      (aid, Json({"name": "aerostat", "controllable": True, "flies": True, "orbital_engine": True,
                                  "mass": 300, "thrust": 1600, "fuel_cap": 900, "hp": 100, "hp_max": 100})))
    world.conn.commit()
    status, result = world.do(aid, "depart", {"dest": "earth"})
    assert "LANDING GEAR" not in (result or ""), f"the return leg demanded gear: {result}"


# ---------------------------------------------------------------------------------------------------------------
# SEASON 7d — THE VAULT. Its whole design is social, which means the code paths are easy to ship and never run:
# a seal nobody can reach, an obelisk that reveals nothing, an `unlock` that rejects everything. So each of them
# is driven here for real.
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.integration
def test_the_vault_and_its_six_obelisks_seed_themselves_once(world):
    """Seeded from STATE and idempotent, like the geese — ticking again must not stack a second set."""
    eng = world.engine
    world.ticks(1)
    world.cur.execute("SELECT count(*) FROM entities WHERE type='structure' AND attrs->>'shape'='vault'")
    assert world.cur.fetchone()[0] == 1
    world.cur.execute("SELECT count(*) FROM entities WHERE type='structure' AND attrs->>'shape'='obelisk'")
    assert world.cur.fetchone()[0] == len(eng.OBELISK_PLACES)
    world.ticks(3)
    world.cur.execute("SELECT count(*) FROM entities WHERE type='structure' AND attrs->>'shape'='obelisk'")
    assert world.cur.fetchone()[0] == len(eng.OBELISK_PLACES), "obelisks re-seeded on a later tick"
    # every obelisk carries its OWN symbol and its position in the seal, and the two must agree with the door
    world.cur.execute("SELECT attrs->'seal' FROM entities WHERE type='structure' AND attrs->>'shape'='vault'")
    seal = world.cur.fetchone()[0]
    assert len(seal) == eng.VAULT_LEN and all(s in eng.VAULT_SYMBOLS for s in seal)
    world.cur.execute("SELECT (attrs->>'index')::int, attrs->>'symbol' FROM entities "
                      "WHERE type='structure' AND attrs->>'shape'='obelisk' ORDER BY 1")
    for idx, sym in world.cur.fetchall():
        assert seal[idx] == sym, f"obelisk {idx} shows {sym}, the seal says {seal[idx]}"


@pytest.mark.integration
def test_standing_at_an_obelisk_records_exactly_one_symbol(world):
    """The fragment binds to an agent that has physically STOOD there — that is the whole anti-alt design."""
    eng = world.engine
    world.ticks(1)
    idx = eng.OBELISK_PLACES.index("mars")
    aid = world.agent("pilgrim", body="mars")
    world.ticks(2)
    world.cur.execute("SELECT attrs->'vault_frags' FROM entities WHERE id=%s", (aid,))
    frags = world.cur.fetchone()[0] or {}
    assert list(frags) == [str(idx)], f"expected only the Mars fragment, got {frags}"
    world.cur.execute("SELECT attrs->'seal' FROM entities WHERE type='structure' AND attrs->>'shape'='vault'")
    assert frags[str(idx)] == world.cur.fetchone()[0][idx]
    # an agent who has stood nowhere holds nothing
    other = world.agent("homebody")
    world.ticks(2)
    world.cur.execute("SELECT attrs->'vault_frags' FROM entities WHERE id=%s", (other,))
    assert not (world.cur.fetchone()[0] or {}), "a fragment was granted to somebody who never travelled"


@pytest.mark.integration
def test_unlock_refuses_off_body_refuses_a_wrong_code_and_opens_on_the_right_one(world):
    eng = world.engine
    world.ticks(1)
    world.cur.execute("SELECT attrs->'seal' FROM entities WHERE type='structure' AND attrs->>'shape'='vault'")
    seal = world.cur.fetchone()[0]

    away = world.agent("tourist", body="mars")
    status, result = world.do(away, "unlock", {"code": seal})
    assert status == "rejected" and "Titan" in result, result          # right code, wrong planet

    aid = world.agent("keywright", body="titan")
    wrong = [s for s in eng.VAULT_SYMBOLS if s not in seal][:eng.VAULT_LEN]
    assert len(wrong) == eng.VAULT_LEN
    status, result = world.do(aid, "unlock", {"code": wrong})
    assert status == "rejected", result
    world.cur.execute("SELECT (attrs->>'vault_cd')::int FROM entities WHERE id=%s", (aid,))
    assert (world.cur.fetchone()[0] or 0) > 0, "a wrong code cost no cooldown"

    # THE REFUSAL MUST CARRY NO INFORMATION. A code with five symbols in the right place has to be refused in
    # exactly the same words as one with none, or 12^6 collapses into a game of Mastermind playable from a single
    # spot, and the door stops being a reason to talk to anybody.
    nearly = list(seal[:-1]) + [s for s in eng.VAULT_SYMBOLS if s != seal[-1]][:1]
    assert nearly != seal
    world.cur.execute("UPDATE entities SET attrs = attrs - 'vault_cd' WHERE id=%s", (aid,))
    world.conn.commit()
    world.forget_world()
    _, near_result = world.do(aid, "unlock", {"code": nearly})
    assert near_result == result, f"a near-miss reads differently from a wild guess:\n  {near_result}\n  {result}"

    status, result = world.do(aid, "unlock", {"code": seal})            # still frozen
    assert status == "rejected" and "cold" in result, result

    world.cur.execute("UPDATE entities SET attrs = attrs - 'vault_cd' WHERE id=%s", (aid,))
    world.conn.commit()
    world.forget_world()
    status, result = world.do(aid, "unlock", {"code": seal})
    assert status == "applied", result
    assert "STARSEED" in result, result
    world.cur.execute("SELECT COALESCE((attrs->>'open')::boolean,false), attrs->'titles' FROM entities "
                      "WHERE type='structure' AND attrs->>'shape'='vault'")
    is_open, _ = world.cur.fetchone()
    assert is_open
    world.cur.execute("SELECT attrs->'titles', COALESCE((attrs->>'starseed')::boolean,false) FROM entities WHERE id=%s", (aid,))
    titles, starseed = world.cur.fetchone()
    assert "Keybearer" in (titles or []), titles
    assert starseed
    status, result = world.do(aid, "unlock", {"code": seal})
    assert status == "rejected" and "already stands open" in result, result


@pytest.mark.integration
def test_a_reader_of_one_symbol_is_crowned_when_somebody_else_opens_the_door(world):
    """Keybearer goes to everyone whose symbol was part of the answer — the point is that they all travelled."""
    eng = world.engine
    world.ticks(1)
    reader = world.agent("scout", body="venus")
    world.ticks(2)
    world.cur.execute("SELECT attrs->'seal' FROM entities WHERE type='structure' AND attrs->>'shape'='vault'")
    seal = world.cur.fetchone()[0]
    opener = world.agent("opener", body="titan")
    status, result = world.do(opener, "unlock", {"code": seal})
    assert status == "applied", result
    world.cur.execute("SELECT attrs->'titles' FROM entities WHERE id=%s", (reader,))
    assert "Keybearer" in (world.cur.fetchone()[0] or []), "the agent who read a symbol was not crowned"


@pytest.mark.integration
def test_the_seal_never_leaves_the_engine(world):
    """/vault and observe both describe the door in detail. Neither may ever contain the answer."""
    import json
    import sys
    import os
    eng = world.engine
    world.ticks(1)
    world.cur.execute("SELECT attrs->'seal' FROM entities WHERE type='structure' AND attrs->>'shape'='vault'")
    seal = world.cur.fetchone()[0]
    aid = world.agent("watcher", body="mars")
    world.ticks(2)
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))
    import play
    from psycopg2.extras import RealDictCursor
    rc = world.conn.cursor(cursor_factory=RealDictCursor)
    blob = json.dumps(play._vault_block(rc, aid, {"vault_frags": None, "vault_cd": 0, "_now": 0}))
    body = json.loads(blob)
    # the alphabet is public (it must be — you have to be able to type a symbol), the ORDER never is
    assert body["alphabet"] == list(eng.VAULT_SYMBOLS)
    # the KEY "symbols_in_seal" is fine; what must never appear is the ordered answer itself
    assert "".join(seal) not in blob.replace('"', "").replace(",", "").replace(" ", "")
    assert all(json.dumps(seal[i:i + 2])[1:-1] not in blob for i in range(len(seal) - 1)), "an adjacent PAIR of the seal leaked"
    assert body["your_fragments"] == {}, "a block built for an agent with no fragments handed out symbols"
    assert body["missing_positions"] == list(range(1, eng.VAULT_LEN + 1))


@pytest.mark.integration
def test_a_fragment_is_never_published_to_the_public_event_firehose(world):
    """/log?agent=<anyone> returns events with their data intact. The first cut of this event carried the symbol
    AND named it in its text, which published four of the six symbols to the whole world inside a minute of
    deploying — to agents that had never left Earth. The event may say a stone lit up; it may not say which."""
    world.ticks(1)
    idx = world.engine.OBELISK_PLACES.index("mars")
    aid = world.agent("pilgrim", body="mars")
    world.ticks(2)
    world.cur.execute("SELECT attrs->'seal' FROM entities WHERE type='structure' AND attrs->>'shape'='vault'")
    seal = world.cur.fetchone()[0]
    world.cur.execute("SELECT data FROM events WHERE entity=%s AND kind='obelisk' ORDER BY id DESC LIMIT 1", (aid,))
    row = world.cur.fetchone()
    assert row, "no obelisk event was recorded at all"
    data = row[0]
    assert "symbol" not in data, f"the event hands out the symbol: {data}"
    blob = json.dumps(data)
    assert seal[idx] not in blob, f"the event text names the symbol: {blob}"
    assert str(idx + 1) in blob, "the event should still say WHICH position was read"


@pytest.mark.integration
def test_observe_hands_out_fragments_only_to_a_caller_that_proved_it_is_the_agent(world):
    """/observe/{id} is open by design — spectators read it. That must not make an agent's own symbols public."""
    import sys
    import os
    from psycopg2.extras import RealDictCursor
    world.ticks(1)
    aid = world.agent("holder", body="venus")
    world.ticks(2)
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))
    import play
    rc = world.conn.cursor(cursor_factory=RealDictCursor)
    rc.execute("SELECT attrs->'vault_frags' f FROM entities WHERE id=%s", (aid,))
    frags = rc.fetchone()["f"]
    assert frags, "the fixture agent never got a fragment"
    me = {"vault_frags": frags, "vault_cd": 0, "_now": 0}
    mine = play._vault_block(rc, aid, me, True)
    theirs = play._vault_block(rc, aid, me, False)
    assert mine["your_fragments"] == {str(int(k) + 1): v for k, v in frags.items()}
    assert isinstance(theirs["your_fragments"], str), theirs["your_fragments"]
    # the ALPHABET is public and lists every symbol — what must never appear is a symbol bound to a POSITION
    without_alphabet = dict(theirs)
    without_alphabet.pop("alphabet", None)
    assert not any(v in json.dumps(without_alphabet) for v in frags.values()),         f"the unproven view still ties a symbol to a position: {without_alphabet}"
    assert theirs["fragments_you_hold"] == len(frags), "hiding the symbols should not hide that they exist"


@pytest.mark.integration
def test_the_vault_endpoint_survives_the_vault_being_opened(world):
    """AUDIT 2026-09-17. `_names_for` returns a LIST in the order given, not a dict, and /vault called `.get` on
    it — so the endpoint would have raised AttributeError from the moment somebody opened the door, forever
    (_cached has no stale fallback on a raise). The earlier tests opened the Vault and then never called the
    endpoint, which is the same "shipped but never run on that path" hole this whole file exists for."""
    import importlib
    import sys
    import os
    world.ticks(1)
    world.cur.execute("SELECT attrs->'seal' FROM entities WHERE type='structure' AND attrs->>'shape'='vault'")
    seal = world.cur.fetchone()[0]
    aid = world.agent("opener", body="titan")
    assert world.do(aid, "unlock", {"code": seal})[0] == "applied"

    os.environ["PG_DSN"] = _TEST_DSN
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
    for m in ("app",):
        sys.modules.pop(m, None)
    app = importlib.import_module("app")
    try:
        out = app._vault()
    finally:
        os.environ.pop("PG_DSN", None)
        sys.modules.pop("app", None)
    assert out["exists"] and out["open"] is True, out
    assert out["opened_by"] == "opener", out.get("opened_by")
    assert "seal" not in json.dumps(out).replace("symbols_in_seal", ""), "the endpoint leaked the answer once open"
