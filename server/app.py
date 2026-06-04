#!/usr/bin/env python3
"""NHA-MMO server — long-running game daemon + REST API.

Runs the authoritative tick loop continuously (the world advances on its own) and exposes a small
REST surface so autonomous agents can plug in: register, observe, and submit intents. Postgres is
the single source of truth; the tick loop is the ONLY writer of world progression — agents merely
enqueue intents, which are applied (or rejected by the loop guard) on the next tick.
"""
import os
import sys
import threading
import time

# the engine package lives next door — make engine.py / vehicles.py / worldgen.py / play.py importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))
import engine          # noqa: E402  — tick / SCHEMA / seed_demo / state_hash / apply_intent
import worldgen        # noqa: E402  — procedural deposit map
from play import observe  # noqa: E402  — curated per-agent observation

import psycopg2                                       # noqa: E402
from psycopg2.extras import RealDictCursor, Json      # noqa: E402
from fastapi import FastAPI, HTTPException            # noqa: E402
from pydantic import BaseModel                        # noqa: E402

DSN          = os.environ.get("PG_DSN", "host=127.0.0.1 dbname=nhamoo user=nhamoo")
TICK_SECONDS = float(os.environ.get("TICK_SECONDS", "2"))
WORLD_W      = int(os.environ.get("WORLD_W", "48"))
WORLD_H      = int(os.environ.get("WORLD_H", "18"))
WORLD_SEED   = int(os.environ.get("WORLD_SEED", "42"))

app = FastAPI(title="NHA-MMO", summary="No-Human-Allowed MMO — a world only AI agents play in.")
_state = {"tick": 0, "running": False, "tick_seconds": TICK_SECONDS}


def _connect(retries=30):
    """Connect to Postgres, tolerating a not-yet-ready database on first boot."""
    last = None
    for _ in range(retries):
        try:
            return psycopg2.connect(DSN)
        except psycopg2.OperationalError as e:
            last = e; time.sleep(2)
    raise last


def _ensure_world():
    conn = _connect()
    cur = conn.cursor()
    cur.execute(engine.SCHEMA); conn.commit()
    engine.seed_demo(conn)                            # base power+mining rig + a starter agent
    cur.execute("SELECT count(*) FROM entities WHERE type='deposit'")
    if cur.fetchone()[0] == 0:
        _, deposits = worldgen.generate(WORLD_W, WORLD_H, WORLD_SEED)
        worldgen.write_deposits(conn, deposits, WORLD_SEED)
        print(f"worldgen: {len(deposits)} deposits placed (seed={WORLD_SEED})")
    conn.close()


def _tick_loop():
    conn = _connect()
    while True:
        try:
            t, _ = engine.tick(conn)
            _state["tick"] = t
        except Exception as e:                        # never let the world stop on a transient error
            print(f"tick error: {e}")
            try:
                conn.rollback()
            except Exception:
                conn = _connect()
        time.sleep(TICK_SECONDS)


@app.on_event("startup")
def _startup():
    _ensure_world()
    threading.Thread(target=_tick_loop, daemon=True).start()
    _state["running"] = True


@app.get("/healthz")
def healthz():
    return _state


@app.get("/world")
def world():
    conn = _connect(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT tick FROM world WHERE id=1"); t = cur.fetchone()["tick"]
    cur.execute("SELECT type, count(*) c FROM entities GROUP BY type ORDER BY type")
    counts = {r["type"]: r["c"] for r in cur.fetchall()}
    cur.execute("SELECT tick, hash FROM tick_hashes ORDER BY tick DESC LIMIT 1")
    h = cur.fetchone(); conn.close()
    return {"tick": t, "tick_seconds": TICK_SECONDS, "entities": counts,
            "last_state_hash": h["hash"] if h else None}


@app.get("/map")
def world_map():
    """The generated biome map with deposits overlaid (deterministic from the world seed)."""
    conn = _connect(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT x, y, attrs->>'resource' res FROM entities "
                "WHERE type='deposit' AND attrs->>'gen_seed'=%s", (str(WORLD_SEED),))
    deps = [(r["x"], r["y"], r["res"], 0, "") for r in cur.fetchall()]; conn.close()
    grid, _ = worldgen.generate(WORLD_W, WORLD_H, WORLD_SEED)
    return {"seed": WORLD_SEED, "w": WORLD_W, "h": WORLD_H, "ascii": worldgen.ascii_map(grid, deps)}


@app.get("/observe/{agent_id}")
def observe_ep(agent_id: int):
    conn = _connect(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT 1 FROM entities WHERE id=%s AND type='agent'", (agent_id,))
    if not cur.fetchone():
        conn.close(); raise HTTPException(404, "no such agent")
    obs = observe(cur, agent_id); conn.close()
    return obs


class AgentIn(BaseModel):
    name: str = "agent"
    materials: dict = {"metal": 60, "crystal": 4}


@app.post("/agents")
def register_agent(a: AgentIn):
    """Spawn a fresh agent with starting materials → returns its id (use it for observe/intent)."""
    conn = _connect(); cur = conn.cursor()
    cur.execute("INSERT INTO entities(type,x,y,buffers,attrs) VALUES('agent',0,0,%s,%s) RETURNING id",
                (Json(a.materials), Json({"name": a.name})))
    aid = cur.fetchone()[0]; conn.commit(); conn.close()
    return {"agent_id": aid, "materials": a.materials}


class IntentIn(BaseModel):
    agent: int
    verb: str                                         # grab | deposit | transfer | build | finalize
    args: dict = {}


@app.post("/intent")
def submit_intent(it: IntentIn):
    """Enqueue an agent action. Applied (or loop-guarded) on the next tick — the world is authoritative."""
    conn = _connect(); cur = conn.cursor()
    cur.execute("SELECT 1 FROM entities WHERE id=%s AND type='agent'", (it.agent,))
    if not cur.fetchone():
        conn.close(); raise HTTPException(404, "no such agent")
    cur.execute("INSERT INTO intents(agent, verb, args) VALUES(%s,%s,%s) RETURNING id",
                (it.agent, it.verb, Json(it.args)))
    iid = cur.fetchone()[0]; conn.commit(); conn.close()
    return {"queued_intent": iid, "note": "applied on next tick"}
