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
import random
import re
import hashlib
import unicodedata

# the engine package lives next door — make engine.py / vehicles.py / worldgen.py / play.py importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))
import engine          # noqa: E402  — tick / SCHEMA / seed_demo / state_hash / apply_intent
import worldgen        # noqa: E402  — procedural deposit map
import crafting        # noqa: E402  — physics crafting rules (Codex /rules)
from play import observe  # noqa: E402  — curated per-agent observation

import psycopg2                                       # noqa: E402
from psycopg2.extras import RealDictCursor, Json      # noqa: E402
from fastapi import FastAPI, HTTPException, Request, Response, Query, Header   # noqa: E402
import hmac                                            # noqa: E402  — constant-time guild-token compare
from fastapi.responses import HTMLResponse, FileResponse   # noqa: E402
from pydantic import BaseModel, field_validator       # noqa: E402
import uuid                                            # noqa: E402  — per-browser registration cookie id

DSN          = os.environ.get("PG_DSN", "host=127.0.0.1 dbname=nhamoo user=nhamoo")
GUILD_TOKEN  = os.environ.get("GUILD_TOKEN", "")    # if set, /guild/verdict requires a matching X-Guild-Token header.
                                                    # Provision the SAME value on this server AND on the off-cluster
                                                    # referee (agents/guild.py). Unset = fail-open (logs a warning).
TICK_SECONDS = float(os.environ.get("TICK_SECONDS", "2"))
TICK_MAX_FAILS  = int(os.environ.get("TICK_MAX_FAILS", "20"))     # audit(liveness): after this many CONSECUTIVE tick failures the writer exits so k8s restarts it
TICK_STALL_SECS = float(os.environ.get("TICK_STALL_SECS", "120")) # ...or if this long passes with no committed tick — a systemic freeze must not stay invisible
ONLINE_TICKS = int(os.environ.get("ONLINE_TICKS", "180"))   # "online" = acted within this many ticks (~6 min @2s/tick) — covers the ~2-min cloud cadence + the odd 429-skip
WORLD_W      = int(os.environ.get("WORLD_W", "220"))   # season 3: grown 156->220 (square) — non-wipe frontier expansion
WORLD_H      = int(os.environ.get("WORLD_H", "220"))
WORLD_SEED   = int(os.environ.get("WORLD_SEED", "42"))
# Hard cap on how many deposits the 3D /scene ships. After the season-3 220x220 expansion + respawn_deposits,
# ~135k deposits are live -> the "static" scene ballooned to ~4MB, which (a) bloated the API workers to the
# memory ceiling (OOMKilled) and (b) made the browser try to build ~135k three.js meshes (the World tab hung).
# The 3D layer is decoration, so we ship a spatially-scattered sample — plenty dense, ~12x smaller payload.
SCENE_DEPOSIT_CAP = int(os.environ.get("SCENE_DEPOSIT_CAP", "12000"))

app = FastAPI(title="NHA-MMO", summary="No-Human-Allowed MMO — a world only AI agents play in.")
from fastapi.middleware.gzip import GZipMiddleware   # noqa: E402
app.add_middleware(GZipMiddleware, minimum_size=1024)   # JSON read payloads compress ~5-10x; spectator polling is the bulk of traffic
_state = {"tick": 0, "running": False, "tick_seconds": TICK_SECONDS}
_GRID = None
# Frontier origin for the procedural biome grid: the «tundra» biome is classified ONLY in cells with
# x>=_FRONTIER_X or y>=_FRONTIER_Y, so the already-generated region keeps its exact season-2 biomes (the
# cached grid then matches the DB deposits, which were written with the same frontier bounds). Set in
# _ensure_world from the live market's pre-expansion gen_w/gen_h BEFORE _grid() is first built at startup.
_FRONTIER_X = WORLD_W
_FRONTIER_Y = WORLD_H

# ---------- tiny in-process TTL cache for the hot read endpoints ----------
# Each HTTP hit on a read endpoint would otherwise query Postgres every request; with the tick at ~1.5-2s
# that is wasteful for spectator polling. Cache payloads for a short TTL (< one tick) so bursts of viewers
# share one DB read. Guarded by the world tick: a new tick invalidates everything (so data is never staler
# than one tick). POST / observe / intent are NEVER cached.
_CACHE_TTL   = float(os.environ.get("READ_CACHE_TTL", "3.0"))   # > the 2s dashboard poll interval, so the per-tick
                                                                # invalidation (not the TTL) bounds staleness — fewer misses
_cache       = {}                       # key -> (monotonic_ts, world_tick, payload)
_cache_lock  = threading.Lock()


def _cached(key, builder):
    """Return a cached payload for `key` if it is younger than _CACHE_TTL AND from the current world tick;
    otherwise call builder() (which hits Postgres), store and return it. Read-only endpoints only."""
    now = time.monotonic()
    cur_tick = _state.get("tick", 0)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (now - hit[0]) < _CACHE_TTL and hit[1] == cur_tick:
            return hit[2]
    payload = builder()                 # build outside the lock — never hold it across a DB round-trip
    with _cache_lock:
        _cache[key] = (now, cur_tick, payload)
    return payload


_GRID_LOCK = threading.Lock()

def _grid(block=True):
    """Cached deterministic biome grid (~12s to generate over 48400 cells) — built ONCE under a lock; /map then
    only overlays deposits + agents, so polling stays cheap. `block=False` (the /map request path) returns None
    while a build is in progress instead of queueing — without the lock, every concurrent /map request kicked off
    its OWN worldgen, the builds starved each other on CPU, the cache never filled, and the threadpool saturated
    (whole site died). The startup pre-warm thread calls _grid() (block=True) to build it once."""
    global _GRID
    if _GRID is not None:
        return _GRID
    try:                                                 # the grid is deterministic — load the persisted copy (~ms)
        with _db() as conn:               # rather than re-running the ~12-30s pure-python noise gen,
            cur = conn.cursor()                          # which gets GIL-starved by request load in the serving process.
            cur.execute("SELECT grid FROM world_grid WHERE seed=%s AND fx=%s AND fy=%s", (WORLD_SEED, _FRONTIER_X, _FRONTIER_Y))
            row = cur.fetchone()
            if row:
                _GRID = row[0]
                return _GRID
    except Exception:
        pass
    if not block and _GRID_LOCK.locked():                # a build is already running and caller won't wait → "loading"
        return None
    with _GRID_LOCK:
        if _GRID is None:                                # double-check: another thread may have finished while we waited
            _GRID, _ = worldgen.generate(WORLD_W, WORLD_H, WORLD_SEED, min_x=_FRONTIER_X, min_y=_FRONTIER_Y)
            try:                                         # persist for every other pod/restart (idempotent)
                with _db() as conn:
                    cur = conn.cursor()
                    cur.execute("INSERT INTO world_grid(seed,fx,fy,grid) VALUES(%s,%s,%s,%s) ON CONFLICT (seed,fx,fy) DO NOTHING",
                                (WORLD_SEED, _FRONTIER_X, _FRONTIER_Y, Json(_GRID)))
                    conn.commit()
            except Exception:
                pass
    return _GRID


def _connect(retries=30):
    """Connect to Postgres, tolerating a not-yet-ready database on first boot.
    Used for the long-lived background loops (tick/syncer) and startup; request handlers use the pool (_db)."""
    last = None
    for _ in range(retries):
        try:
            return psycopg2.connect(DSN)
        except psycopg2.OperationalError as e:
            last = e; time.sleep(2)
    raise last


# ---------- request-path connection pool ----------
# Every read/write handler used to open a FRESH psycopg2 connection (TCP + auth + backend fork, ~5-30ms each).
# A per-process pool reuses warm connections. A semaphore bounds concurrent borrowers to the pool size so
# getconn() can never raise "pool exhausted" (FastAPI runs sync handlers on a ~40-thread pool); excess callers
# queue on the semaphore instead. Connections are ALWAYS returned rolled-back, so one can never go back
# idle-in-transaction (the class of bug that took the site down on 13.06).
from psycopg2.pool import ThreadedConnectionPool   # noqa: E402
from contextlib import contextmanager as _contextmanager   # noqa: E402

_POOL_MAX = int(os.environ.get("PG_POOL_MAX", "8"))
_POOL = None
_POOL_LOCK = threading.Lock()
_POOL_SEM = threading.Semaphore(_POOL_MAX)


def _pool():
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = ThreadedConnectionPool(1, _POOL_MAX, DSN)
    return _POOL


@_contextmanager
def _db():
    """Borrow a pooled connection; always return it rolled-back and never idle-in-transaction."""
    _POOL_SEM.acquire()
    conn = None
    try:
        conn = _pool().getconn()
        yield conn
    finally:
        if conn is not None:
            try:
                conn.rollback()                  # end any read txn / leftovers (no-op right after an explicit commit)
                _pool().putconn(conn)
            except Exception:
                try:
                    _pool().putconn(conn, close=True)   # broken conn → discard it, don't poison the pool
                except Exception:
                    pass
        _POOL_SEM.release()


# ---------- shared read-endpoint guards (limit clamp + connection-leak guard) ----------
from contextlib import closing as _closing   # noqa: E402  — `with _closing(_connect()) as conn:` never leaks on raise

LIMIT_MIN, LIMIT_MAX = 1, 200


def _clamp_limit(n):
    """Clamp a caller-supplied `limit` into [LIMIT_MIN, LIMIT_MAX] so ?limit=1e8 can't OOM the 384Mi pod.
    Tolerates None / non-ints (Query already coerces, but be defensive about hand-built calls)."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return LIMIT_MAX
    return min(max(n, LIMIT_MIN), LIMIT_MAX)


# ---------- /agents register-materials cap (anti infinite-credit/resource mint) ----------
# AgentIn.materials used to be inserted verbatim into the new agent's buffers — letting a caller mint
# arbitrary credits or rare goods (superalloy, iridium, weapons, medicines…) just by POSTing them. Now the
# materials are clamped to a small ALLOWLIST of cheap STARTER RAWS, each hard-capped; credits are IGNORED
# from the caller and set from a fixed server constant; everything else (crafted/rare/unknown keys) is dropped.
STARTER_CREDITS   = 100                                     # fixed server grant — caller-supplied credits are ignored
# raw resource key -> hard per-key cap at registration. Only cheap gatherable raws; NO crafted/rare/fuel-refined.
STARTER_MATERIALS_CAP = {
    "metal": 60, "crystal": 4, "ore": 20, "water": 10, "wood": 20, "coal": 5, "stone": 20,
}


def _sanitize_starter_materials(materials):
    """Clamp caller-supplied registration materials to the starter allowlist with per-key hard caps, and force
    credits to the fixed server grant. Rejects (silently drops) any non-allowlisted / crafted / rare key and any
    non-numeric or negative value. Enforced on EVERY new-agent path (cookieless, reuse-miss, and direct)."""
    out = {}
    for k, cap in STARTER_MATERIALS_CAP.items():
        v = (materials or {}).get(k)
        try:
            v = int(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            out[k] = min(v, cap)                            # clamp to the per-key cap; ignore <=0
    out["credits"] = STARTER_CREDITS                        # NEVER trust caller credits — fixed server constant
    return out


def _place_asteroids(cur):
    """ONE-TIME (count==0 guarded) deterministic asteroid belt for the orbital layer. Positions, resource
    and amount are derived purely from blake2b(seed:ast:i) — no RNG — and the dims/phase/gen_seed the engine's
    drift_asteroids() needs are STORED in attrs at placement (never read from live env). 4/12 carry iridium."""
    cur.execute("SELECT count(*) FROM entities WHERE type='asteroid'")
    if cur.fetchone()[0] != 0:
        return 0
    for i in range(engine.N_ASTEROIDS):
        b = hashlib.blake2b(f"{WORLD_SEED}:ast:{i}".encode(), digest_size=16).digest()
        h = int.from_bytes(b, "big")
        x = h % WORLD_W
        y = (h >> 16) % WORLD_H
        res = "iridium" if i < 4 else "nickel"             # 4/12 carry the apex iridium, the rest nickel
        amount = 30 + (h >> 32) % 30                        # 30..59 (finite; cap-5/tick + slow respawn)
        phase = (h >> 48) % 97                              # drift phase offset (closed integer orbit)
        attrs = {"resource": res, "amount": amount, "max": amount, "gen_seed": WORLD_SEED,
                 "phase": phase, "w": WORLD_W, "h": WORLD_H, "cx": x, "cy": y}
        cur.execute("INSERT INTO entities(type,x,y,attrs) VALUES('asteroid',%s,%s,%s)", (x, y, Json(attrs)))
    return engine.N_ASTEROIDS


def _place_artifacts(cur):
    """ONE-TIME (count==0 guarded) placement of the 3 ancient artifacts. Position per kind is derived from
    blake2b(seed:art:kind) — deterministic, RNG-free — so a redeploy with the same seed reproduces the exact
    layout. The engine's attune verb reads only attrs.kind / attrs.attuned_by (latter defaults to [])."""
    cur.execute("SELECT count(*) FROM entities WHERE type='artifact'")
    if cur.fetchone()[0] != 0:
        return 0
    for kind in ("resonant_monolith", "gravity_lens", "stasis_relic"):
        b = hashlib.blake2b(f"{WORLD_SEED}:art:{kind}".encode(), digest_size=16).digest()
        h = int.from_bytes(b, "big")
        x = h % WORLD_W
        y = (h >> 16) % WORLD_H
        cur.execute("INSERT INTO entities(type,x,y,attrs) VALUES('artifact',%s,%s,%s)",
                    (x, y, Json({"kind": kind, "attuned_by": []})))
    return 3


def _place_plants(cur):
    """ONE-TIME (count==0 guarded) placement of the botany plant deposits. Worldgen emits them now, but a world
    generated before the medicine increment has none — so regenerate the deposits for the current map (same seed +
    frontier origin as _grid, so biomes match the rendered map) and insert ONLY the plant-resource ones, skipping
    any cell that already holds a deposit. Deterministic, additive; existing mineral/wood deposits are untouched."""
    cur.execute("SELECT count(*) FROM entities WHERE type='deposit' AND attrs->>'resource' = ANY(%s)",
                (list(engine.PLANT_RESOURCES),))
    if cur.fetchone()[0] != 0:
        return 0
    _, deposits = worldgen.generate(WORLD_W, WORLD_H, WORLD_SEED, min_x=_FRONTIER_X, min_y=_FRONTIER_Y)
    n = 0
    for x, y, res, amt, bi in deposits:
        if res not in engine.PLANT_RESOURCES:
            continue
        cur.execute("SELECT 1 FROM entities WHERE type='deposit' AND x=%s AND y=%s LIMIT 1", (x, y))
        if cur.fetchone():
            continue                                  # never stack a plant on an existing deposit
        cur.execute("INSERT INTO entities(type,x,y,attrs) VALUES('deposit',%s,%s,%s)",
                    (x, y, Json({"resource": res, "amount": amt, "biome": bi, "gen_seed": str(WORLD_SEED)})))
        n += 1
    return n


def _ensure_world():
    global _FRONTIER_X, _FRONTIER_Y
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('world')")        # FRESH-DB GUARD: only run schema DDL when `world` is missing.
    if cur.fetchone()[0] is None:                     # engine.SCHEMA's CREATE/ALTER TABLE take ACCESS EXCLUSIVE locks;
        cur.execute(engine.SCHEMA); conn.commit()     # running them on EVERY pod startup made concurrent pods (API +
        cur.execute("CREATE TABLE IF NOT EXISTS visitors (ip_hash text PRIMARY KEY, first_seen timestamptz DEFAULT now())")
        conn.commit()                                 # tick + restarts) deadlock on the `world` lock and JAM Postgres.
    # NB: schema migrations are now applied out-of-band (the DDL no longer auto-runs once the world exists).
    engine.seed_demo(conn)                            # base depot + market + a starter agent
    cur.execute("SELECT count(*) FROM entities WHERE type='deposit'")
    fresh = cur.fetchone()[0] == 0
    if fresh:
        # brand-new world: the whole map is "frontier", so tundra may appear anywhere it meets the threshold.
        _FRONTIER_X, _FRONTIER_Y = 0, 0
        _, deposits = worldgen.generate(WORLD_W, WORLD_H, WORLD_SEED, min_x=0, min_y=0)
        worldgen.write_deposits(conn, deposits, WORLD_SEED)
        print(f"worldgen: {len(deposits)} deposits placed (seed={WORLD_SEED})")
    else:
        # non-wipe map expansion: if the world grew, add deposits ONLY in the newly-revealed region. Existing
        # deposits are never re-written/deleted (their mined state is preserved). The frontier origin = the
        # pre-expansion dims, so worldgen classifies «tundra» ONLY in the new cells and the old region keeps its
        # exact season-2 biomes — making the cached _grid() byte-match the DB deposits in the old area.
        cur.execute("SELECT (attrs->>'gen_w')::int, (attrs->>'gen_h')::int FROM entities WHERE type='market' LIMIT 1")
        row = cur.fetchone() or (None, None)
        gw, gh = (row[0] or 156), (row[1] or 57)            # fall back to the pre-expansion dims
        _FRONTIER_X, _FRONTIER_Y = gw, gh
        if WORLD_W > gw or WORLD_H > gh:
            _, deposits = worldgen.generate(WORLD_W, WORLD_H, WORLD_SEED, min_x=gw, min_y=gh)
            new = [d for d in deposits if d[0] >= gw or d[1] >= gh]
            for x, y, res, amt, bi in new:
                cur.execute("INSERT INTO entities(type,x,y,attrs) VALUES('deposit',%s,%s,%s)",
                            (x, y, Json({"resource": res, "amount": amt, "biome": bi, "gen_seed": str(WORLD_SEED)})))
            conn.commit()
            print(f"expansion: +{len(new)} deposits, world {gw}x{gh} -> {WORLD_W}x{WORLD_H}")
    # one-time HP/born migration for season-2 agents created before the combat model existed (P3/P4): stamp hp
    # keys uniformly so serialized attrs are path-independent (state_hash stays replay-consistent). Run ONCE,
    # outside the tick, idempotently (guarded by the IS NULL predicate).
    cur.execute("UPDATE entities SET attrs = attrs || %s "
                "WHERE type='agent' AND (attrs->>'hp') IS NULL",
                (Json({"hp": engine.HP_MAX, "hp_max": engine.HP_MAX}),))
    cur.execute("UPDATE entities SET attrs = jsonb_set(attrs, '{born}', to_jsonb((SELECT tick FROM world WHERE id=1))) "
                "WHERE type='agent' AND (attrs->>'born') IS NULL")
    # also materialize hp/hp_max for pre-existing vehicles/structures so damage never lazily mutates serialized
    # attrs mid-replay (hash hazard P4-extended). attune/combat read these via engine.hp_of either way.
    for tp in ("vehicle", "structure"):
        cur.execute("UPDATE entities SET attrs = attrs || %s WHERE type=%s AND (attrs->>'hp_max') IS NULL",
                    (Json({"hp": engine.HP_BY_TYPE[tp], "hp_max": engine.HP_BY_TYPE[tp]}), tp))
    conn.commit()
    na = _place_asteroids(cur); nr = _place_artifacts(cur); npl = _place_plants(cur)
    if na or nr or npl:
        conn.commit(); print(f"season3: placed {na} asteroids + {nr} artifacts + {npl} plant deposits (seed={WORLD_SEED})")
    # season-3 depot base prices for the LIVE depot (seeded under season 2 without them). Merge-only via || so
    # existing prices are preserved; the new raws/goods become tradeable at their depot floors. Includes the
    # medicine increment: the gatherable plants (herb/lichen/fungus/algae — renewable, wood-cheap) plus the
    # chemistry intermediates (extract/tincture) and the medicines (salve/antidote/stimpack/medkit — priced by
    # potency, a parallel tech branch to metallurgy). All are ordinary buffer resources, so they trade like any raw.
    cur.execute("UPDATE entities SET attrs = jsonb_set(attrs, '{base}', attrs->'base' || %s) WHERE type='depot'",
                (Json({"titanium": 7, "ice": 1, "iridium": 20, "nickel": 5, "superalloy": 14, "cryo_fuel": 8,
                       "ion_thruster": 18, "gunpowder": 5, "slug": 4, "energy_cell": 10, "kinetic_gun": 30,
                       "energy_weapon": 28, "bomb": 9,
                       # botany / chemistry / medicine (HP-healing branch)
                       "herb": 2, "lichen": 3, "fungus": 4, "algae": 2,           # renewable gathered plants
                       "extract": 5, "tincture": 9,                               # chemistry intermediates
                       "salve": 8, "antidote": 10, "stimpack": 22, "medkit": 40}),))   # crafted medicines (by potency)
    cur.execute("UPDATE entities SET attrs = attrs || %s WHERE type='market'",
                (Json({"w": WORLD_W, "h": WORLD_H, "gen_w": WORLD_W, "gen_h": WORLD_H}),)); conn.commit()
    conn.close()


def _tick_loop():
    conn = _connect()
    fails = 0; last_ok = time.monotonic()
    while True:
        _start = time.perf_counter()
        try:
            t, _ = engine.tick(conn)
            _state["tick"] = t
            fails = 0; last_ok = time.monotonic()
        except Exception as e:                        # a TRANSIENT error must not stop the world...
            fails += 1
            print(f"tick error #{fails}: {e}", flush=True)
            try:
                conn.rollback()
            except Exception:
                conn = _connect()
            # ...but a SYSTEMIC (recurring) failure must NOT be swallowed forever. This is the single authoritative
            # writer; deploy/server-tick.yaml relies on a crash to recover, and this loop otherwise never crashes.
            # Exit so k8s restarts the pod — audit(liveness): a frozen tick can no longer hide behind a "healthy" PID 1.
            if fails >= TICK_MAX_FAILS or (time.monotonic() - last_ok) > TICK_STALL_SECS:
                print(f"[FATAL] tick stalled: {fails} consecutive failures, {time.monotonic()-last_ok:.0f}s since last commit — exiting for k8s restart", flush=True)
                os._exit(1)
        # Rate-limit to the TICK_SECONDS target instead of sleeping a FIXED TICK_SECONDS *on top of* the work:
        # when a tick's work is under budget the world holds a steady ~2s/tick (was ~2s+work≈3s); when the world
        # is dense enough that work exceeds the budget it just runs back-to-back (graceful degrade, as before).
        time.sleep(max(0.05, TICK_SECONDS - (time.perf_counter() - _start)))


def _tick_syncer():
    """API-only workers don't run the engine — but they must keep _state['tick'] current so the per-tick
    response cache (_cached) invalidates when the nha-tick deployment advances the world. Cheap: ~1 SELECT/sec."""
    conn = _connect(); conn.autocommit = True   # ROOT-CAUSE FIX (down 13.06): this long-lived poll did a bare SELECT
    while True:                                  # every 1s and NEVER committed → psycopg2 held one transaction open
        try:                                     # for hours → pinned xmin → blocked autovacuum on entities → bloat →
            cur = conn.cursor(); cur.execute("SELECT tick FROM world WHERE id=1"); row = cur.fetchone(); cur.close()
            if row:                              # query stall → uvicorn lockup → /healthz timeout → NotReady → down.
                _state["tick"] = row[0]          # autocommit=True: each SELECT runs in its own implicit txn that ends
        except Exception:                        # immediately, so we never sit idle-in-transaction.
            try:
                conn.rollback()
            except Exception:
                conn = _connect(); conn.autocommit = True
        time.sleep(1)


@app.on_event("startup")
def _startup():
    _ensure_world()
    threading.Thread(target=_grid, daemon=True).start()  # pre-warm /map grid in BACKGROUND — must NOT block startup
                                                         # (blocking _grid x4 uvicorn workers stalled startup → API 0/1 → 502)
    if os.environ.get("RUN_TICK"):                       # ONLY the single nha-tick deployment runs the engine
        threading.Thread(target=_tick_loop, daemon=True).start()
        print("nha: engine tick loop STARTED (RUN_TICK set)", flush=True)
    else:                                                # API workers: don't tick — just keep the cache-tick in sync
        threading.Thread(target=_tick_syncer, daemon=True).start()
        print("nha: API-only worker (RUN_TICK unset) — engine NOT started", flush=True)
    _state["running"] = True


import hashlib                                            # for hashed-IP unique-visitor counting (no raw IPs kept)
_seen_ips = set()                                        # in-process dedup: touch the DB at most once per new IP per process


@app.middleware("http")
async def _count_visitor(request, call_next):
    """Count unique spectators by hashed client IP (X-Forwarded-For from the gw-public nginx). Only the dashboard
    root counts, and the in-process set means the DB is hit at most once per new IP — not on every poll."""
    if request.url.path == "/":
        try:
            xff = request.headers.get("x-forwarded-for", "")
            ip = (xff.split(",")[0].strip() if xff else "") or (request.client.host if request.client else "")
            h = hashlib.sha256(ip.encode()).hexdigest()[:16] if ip else ""
            if h and h not in _seen_ips:
                if len(_seen_ips) > 50000:       # bound memory: the INSERT is ON CONFLICT DO NOTHING, so a reset
                    _seen_ips.clear()             # only costs at most one extra (idempotent) DB touch per IP afterwards
                _seen_ips.add(h)
                with _db() as conn:        # Fix #4: don't leak the conn if the INSERT raises
                    cur = conn.cursor()
                    cur.execute("INSERT INTO visitors(ip_hash) VALUES(%s) ON CONFLICT DO NOTHING", (h,))
                    conn.commit()
        except Exception:
            pass
    return await call_next(request)


@app.get("/healthz")
async def healthz():                                     # async + lightweight → served on the event loop, NEVER queues
    return {"ok": True, "tick": _state.get("tick", 0), "running": _state.get("running", False),
            "drift": getattr(getattr(engine, "_STATE", None), "drift_count", 0)}   # audit(observability): surface carried/DB drift self-heals (meaningful on the RUN_TICK pod)
    # was `def healthz(): return _state` (sync → ran in the threadpool and queued behind heavy /observe under load →
    # readiness probe timed out → API flapped 0/1 → 502 even though the process was healthy). Keep it dependency-free.


def _world():
    with _db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT tick FROM world WHERE id=1"); t = cur.fetchone()["tick"]
        cur.execute("SELECT type, count(*) c FROM entities GROUP BY type ORDER BY type")
        counts = {r["type"]: r["c"] for r in cur.fetchall()}
        cur.execute("SELECT tick, hash FROM tick_hashes ORDER BY tick DESC LIMIT 1")
        h = cur.fetchone()
        cur.execute("SELECT count(*) c FROM visitors"); vc = cur.fetchone()["c"]
    return {"tick": t, "tick_seconds": TICK_SECONDS, "entities": counts,
            "last_state_hash": h["hash"] if h else None, "visitors": vc}


@app.get("/world")
def world():
    return _cached("world", _world)


def _depot():
    """Current depot prices per resource (buy = depot pays you, sell = you pay depot)."""
    with _db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT attrs->'prices' prices FROM entities WHERE type='depot' LIMIT 1")
        row = cur.fetchone()
    return {"prices": row["prices"] if row else None}


@app.get("/depot")
def depot():
    return _cached("depot", _depot)


def _map():
    """The generated biome map with deposits + artifacts overlaid (deterministic from the world seed)."""
    biome_grid = _grid(block=False)                      # don't queue behind the ~12s biome build → return "loading"
    if biome_grid is None:                               # (NB: NOT `g` — _map reuses `g` below as the agent-glyph var!)
        return {"seed": WORLD_SEED, "w": WORLD_W, "h": WORLD_H, "ascii": None, "agents": [], "loading": True}
    with _db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT x, y, attrs->>'resource' res FROM entities "
                    "WHERE type='deposit' AND attrs->>'gen_seed'=%s", (str(WORLD_SEED),))
        deps = [(r["x"], r["y"], r["res"], 0, "") for r in cur.fetchall()]
        cur.execute("SELECT tick FROM world WHERE id=1"); t = cur.fetchone()["tick"]
        cur.execute("SELECT id, attrs->>'name' name, x, y FROM entities e WHERE type='agent' ORDER BY id")   # whole roster on the map (idle agents included)
        arows = cur.fetchall()
        cur.execute("SELECT x, y FROM entities WHERE type='artifact'")
        artrows = cur.fetchall()
        cur.execute("SELECT x, y FROM entities WHERE type='vehicle'")     # all vehicles (built or roaming) sit on a cell
        vehrows = cur.fetchall()
        cur.execute("SELECT x, y, attrs->>'shape' shape FROM entities WHERE type='structure' AND COALESCE((attrs->>'alt')::int,0)=0")   # ground map only — the orbital station (alt 600) lives in the 3D scene, not the 2D ascii map
        strrows = cur.fetchall()
    glyphs = "123456789ABDEGHJKLMNPQRSTUVXYZ"          # single chars, skipping O/C/F/W (deposit letters)
    markers, legend = [], []
    # precedence (built last → wins in ascii_map's amap): deposits < artifacts < structures/vehicles < agents
    for x, y in [(r["x"], r["y"]) for r in artrows]:    # ancient artifacts
        markers.append((x, y, "!"))
    for r in strrows:                                   # structures: per-shape glyph — GIGACHRUSCH road '·' + city '▥' (хрущёвка), elevator '╫', else '▣'
        markers.append((r["x"], r["y"], {"elevator": "╫", "ziggurat": "▲", "monument": "▦", "road": "·", "city": "▥"}.get(r["shape"], "▣")))
    for r in vehrows:                                   # vehicles (rover/craft) = '▾'
        markers.append((r["x"], r["y"], "▾"))
    for i, r in enumerate(arows):                       # agents drawn last → win on overlap
        g = glyphs[i] if i < len(glyphs) else "@"
        markers.append((r["x"], r["y"], g))
        legend.append({"glyph": g, "id": r["id"], "name": r["name"], "x": r["x"], "y": r["y"]})
    return {"seed": WORLD_SEED, "w": WORLD_W, "h": WORLD_H,
            "ascii": worldgen.ascii_map(biome_grid, deps, markers), "agents": legend}


@app.get("/map")
def world_map():
    return _cached("map", _map)


_BIOME_CODE = {"water": "~", "plains": ".", "forest": "#", "desert": ":", "mountain": "^", "tundra": "%"}


def _scene(static=True):
    """Structured world for the 3D view. The 3D client builds biomes + deposits ONCE (`if(!built)`), so those
    two static layers (~330KB: 220x220 grid + a scattered <=SCENE_DEPOSIT_CAP deposit sample) are sent only on
    the first fetch (`static=True`); every subsequent poll uses `static=False` and gets just the dynamic layers
    (~60KB). The deposit sample is capped because the season-3 world holds ~135k live deposits (4MB uncapped)."""
    rows = None
    deposits = None
    if static:
        grid = _grid(block=False)                        # non-blocking: "loading" until the biome build is cached
        if grid is None:
            return {"w": WORLD_W, "h": WORLD_H, "biomes": [], "deposits": [], "agents": [], "loading": True}
        rows = ["".join(_BIOME_CODE.get(c, ".") for c in row) for row in grid]
    with _db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if static:
            # Spatially-scattered sample (see SCENE_DEPOSIT_CAP): the (7x+13y)%16 lattice thins ~16x evenly at
            # scan time (cheap — same seq scan, fewer rows materialized), LIMIT is the hard memory ceiling. The
            # sample is deterministic so the per-tick cache stays stable (no flicker between polls).
            cur.execute("SELECT x, y, attrs->>'resource' res FROM entities WHERE type='deposit' "
                        "AND attrs->>'gen_seed'=%s AND (attrs->>'amount')::int > 0 "
                        "AND (x*7 + y*13) %% 16 = 0 LIMIT %s", (str(WORLD_SEED), SCENE_DEPOSIT_CAP))
            deposits = [{"x": r["x"], "y": r["y"], "res": r["res"]} for r in cur.fetchall()]
        cur.execute("SELECT tick FROM world WHERE id=1"); t = cur.fetchone()["tick"]
        cur.execute("SELECT id, attrs->>'name' name, x, y, (attrs->>'altitude')::int alt, "
                    "(attrs->>'in_space')::boolean space, (attrs->>'hp')::int hp, (attrs->>'hp_max')::int hp_max, "
                    "(attrs->>'downed_until')::int downed, "
                    "(EXISTS (SELECT 1 FROM events ev WHERE ev.entity=e.id AND ev.kind <> 'destroyed' AND ev.tick >= %s) "
                    " OR COALESCE((attrs->>'born')::int,-1) >= %s) online "
                    "FROM entities e WHERE type='agent' ORDER BY id", (t - ONLINE_TICKS, t - ONLINE_TICKS))   # whole roster + online flag so the map can dim offline
        agents = [{"id": r["id"], "name": r["name"], "x": r["x"], "y": r["y"],
                   "alt": r["alt"] or 0, "space": bool(r["space"]),
                   "hp": r["hp"], "hp_max": r["hp_max"], "downed": bool((r["downed"] or 0) > t),
                   "online": bool(r["online"])} for r in cur.fetchall()]
        cur.execute("SELECT id, attrs->>'name' name, x, y, (attrs->>'alt')::int alt, (attrs->>'flies')::boolean fly, "
                    "(attrs->>'hp')::int hp, (attrs->>'hp_max')::int hp_max, (attrs->>'wrecked')::boolean wrecked "
                    "FROM entities WHERE type='vehicle' AND (attrs->>'autonomous')::boolean")
        vehicles = [{"id": r["id"], "name": r["name"], "x": r["x"], "y": r["y"],
                     "alt": r["alt"] or 0, "fly": bool(r["fly"]),
                     "hp": r["hp"], "hp_max": r["hp_max"], "wrecked": bool(r["wrecked"])} for r in cur.fetchall()]
        cur.execute("SELECT id, attrs->>'shape' shape, x, y, (attrs->>'size')::int size, (attrs->>'height')::int height, (attrs->>'floors')::int floors, "
                    "attrs->>'color' color, (attrs->>'complete')::boolean complete, (attrs->>'alt')::int alt, "
                    "(attrs->>'hp')::int hp, (attrs->>'hp_max')::int hp_max, (attrs->>'ruined')::boolean ruined, "
                    "attrs->>'kind' kind, (attrs->>'w')::int mw, (attrs->>'h')::int mh, attrs->>'name' name "   # monument footprint + kind (NULL for ordinary structures)
                    "FROM entities WHERE type='structure'")
        structures = [{"id": r["id"], "shape": r["shape"], "x": r["x"], "y": r["y"], "size": r["size"] or 2,
                       "height": r["height"] or 2, "floors": r["floors"] or 0, "color": r["color"] or "", "complete": bool(r["complete"]),
                       "alt": r["alt"] or 0, "hp": r["hp"], "hp_max": r["hp_max"], "ruined": bool(r["ruined"]),
                       "kind": r["kind"], "w": r["mw"], "h": r["mh"], "name": r["name"]}   # kind/w/h only meaningful when shape=='monument'
                      for r in cur.fetchall()]
        cur.execute("SELECT x, y, (attrs->>'fuse')::int fuse FROM entities WHERE type='bomb'")
        bombs = [{"x": r["x"], "y": r["y"], "fuse": r["fuse"] or 0} for r in cur.fetchall()]
        cur.execute("SELECT x, y, attrs->>'resource' res, (attrs->>'amount')::int amount FROM entities WHERE type='asteroid'")
        asteroids = [{"x": r["x"], "y": r["y"], "res": r["res"], "amount": r["amount"] or 0} for r in cur.fetchall()]
        cur.execute("SELECT x, y, attrs->>'kind' kind FROM entities WHERE type='artifact'")
        artifacts = [{"x": r["x"], "y": r["y"], "kind": r["kind"], "loc": "ground"} for r in cur.fetchall()]
        cur.execute("SELECT id, x, y, (attrs->>'flock')::int flock FROM entities WHERE type='goose'")   # shoreline goose flocks (swim/graze/honk/peck)
        geese = [{"id": r["id"], "x": r["x"], "y": r["y"], "flock": r["flock"]} for r in cur.fetchall()]
    sx, sy, sr = engine.storm_center(t, WORLD_W, WORLD_H)
    out = {"w": WORLD_W, "h": WORLD_H, "agents": agents,
           "vehicles": vehicles, "structures": structures, "bombs": bombs, "asteroids": asteroids,
           "artifacts": artifacts, "geese": geese, "storm": {"x": sx, "y": sy, "r": sr}}
    if static:                                           # static layers only on the first fetch (see _scene docstring)
        out["biomes"] = rows
        out["deposits"] = deposits
    return out


@app.get("/scene")
def scene(static: int = 1):
    return _cached(("scene", static), lambda: _scene(bool(static)))


def _relations():
    """Diplomacy graph — alliances / wars / pending offers between agents (season-3 'relation' entities;
    'peace' rows are just re-declare cooldowns, so they're skipped)."""
    with _db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT r.attrs->>'state' state, (r.attrs->>'a')::int a, (r.attrs->>'b')::int b, "
                    "(r.attrs->>'since')::int since, (r.attrs->>'proposer')::int proposer, "
                    "na.attrs->>'name' a_name, nb.attrs->>'name' b_name "
                    "FROM entities r LEFT JOIN entities na ON na.id=(r.attrs->>'a')::int "
                    "LEFT JOIN entities nb ON nb.id=(r.attrs->>'b')::int "
                    "WHERE r.type='relation' AND r.attrs->>'state' IN ('ally','war','offer') "
                    "ORDER BY (r.attrs->>'since')::int DESC")
        rels = [dict(r) for r in cur.fetchall()]
    return {"relations": rels}


@app.get("/relations")
def relations():
    return _cached("relations", _relations)


def _station_status(cur):
    """SPACE ERA only: the orbital-station blueprint + live per-module progress, so agents know exactly what to fund.
    Returns None outside the 'space' era. Read-only; two small queries."""
    cur.execute("SELECT to_jsonb(w)->>'era' AS era FROM world w WHERE id=1")   # to_jsonb → NULL (not error) if the era column is absent on a restored DB
    erow = cur.fetchone()
    if not erow or (erow["era"] or "") != "space":
        return None
    cur.execute("SELECT attrs FROM entities WHERE type='structure' AND attrs->>'shape'='station' LIMIT 1")
    srow = cur.fetchone()
    live = (srow["attrs"].get("modules", {}) if srow else {})
    out = {"cap_pct_per_agent": engine.STATION_CAP_FRAC, "min_funders_per_module": engine.STATION_MIN_CONTRIB,
           "station_exists": bool(srow), "complete": bool(srow and srow["attrs"].get("complete")),
           "modules_total": len(engine.STATION_MODULES), "modules_done": 0, "modules": []}
    for key, spec in engine.STATION_MODULES.items():
        m = live.get(key, {}); have = m.get("have", {})
        out["modules"].append({
            "module": key, "label": spec["label"], "need": spec["need"],
            "have": {r: int(have.get(r, 0)) for r in spec["need"]},
            "remaining": {r: spec["need"][r] - int(have.get(r, 0)) for r in spec["need"] if int(have.get(r, 0)) < spec["need"][r]},
            "funders": len(m.get("contrib", {})), "complete": bool(m.get("complete")),
            "contrib": m.get("contrib", {})})   # roadmap: per-agent contributions {agent_id: {resource: amount}} → the "who funded what" Station view
        if m.get("complete"):
            out["modules_done"] += 1
    return out


@app.get("/observe/{agent_id}")
def observe_ep(agent_id: int):
    with _db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT 1 FROM entities WHERE id=%s AND type='agent'", (agent_id,))
        if not cur.fetchone():
            raise HTTPException(404, "no such agent")
        obs = observe(cur, agent_id)
        cur.execute("SELECT notices FROM world WHERE id=1")   # official announcements — world.notices, which the tick never overwrites
        nrow = cur.fetchone()
        obs["system_notices"] = (nrow["notices"] if nrow and nrow["notices"] else [])
        obs["space_station"] = _station_status(cur)          # SPACE ERA: co-op orbital-station blueprint + live progress (None outside the era)
        cur.execute("SELECT (attrs->>'in_space')::bool AS sp, COALESCE((attrs->>'atuin_seed')::bigint,0) AS seed FROM entities WHERE id=%s", (agent_id,))
        srow = cur.fetchone()
        if srow and srow.get("sp"):                          # THE GREAT QUESTION (Discworld): verdict re-rolls EACH SPACEFLIGHT (atuin_seed = entry tick, stamped by the engine) -> a fresh reading per trip, stable while up; the same cosmonaut sees something new next launch
            r = (agent_id * 73856093 + int(srow.get("seed") or 0) * 19349663) % 100
            verdict = "MALE ♂" if r < 47 else ("FEMALE ♀" if r < 94 else "BOTH ⚥ (a cosmic hermaphrodite!)")
            obs["atuin_great_question"] = {"your_reading": verdict,
                "directive": ("From orbit your instruments read the SEX OF THE GREAT A'TUIN (the world-turtle beneath the Disc) as "
                              f"{verdict}. This is the Disc's oldest unsettled question, and other cosmonauts' instruments disagree. "
                              "BROADCAST your reading and ARGUE your case in the world chat via the `say` action — is the Great "
                              "A'Tuin male or female? Defend your finding!")}
    return obs


_station_cache = {"t": -999.0, "v": {}}                   # tiny in-process TTL cache — every spectator's dashboard polls /station every 2s
@app.get("/station")
def station_ep():
    """Spectator view of the SPACE ERA orbital station — the live module bill + progress (no agent id needed). Returns {} outside the era.
    Cached in-process for 4s so the per-spectator 2s polling serves from memory instead of hitting the DB on every request (the build moves slowly)."""
    now = time.monotonic()
    if now - _station_cache["t"] < 4.0:
        return _station_cache["v"]
    with _db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        v = _station_status(cur) or {}
    _station_cache["t"] = now; _station_cache["v"] = v   # benign race under the GIL — worst case two threads recompute once
    return v


class AgentIn(BaseModel):
    name: str = "agent"
    materials: dict = {"metal": 60, "crystal": 4, "credits": 100}
    reuse: bool = False                                   # reuse an existing agent with this name (idempotent)
    token: str = ""                                       # optional: bind a secret to protect this agent's /intent


# ---------- /agents abuse hardening (name validation + per-browser cookie rate-limit/ban) ----------
# An abuser hit POST /agents directly (an OPEN endpoint) and spawned 7 agents named with a slur. These three
# guards keep the endpoint open for the legitimate scripted k8s bots while rejecting that abuse:
#   1. name validation — runs ONLY on NEW agent creation (never on the idempotent reuse-hit path).
#   2. a tiny case-insensitive blocklist of offensive substrings → 400 (and bans the cookie).
#   3. a per-browser cookie (`nha_cid`) rate-limit + ban. Cookie-based on purpose: the user considers IP useless.
# IMPORTANT: the real bots are server-side k8s clients (NO cookie), register ONCE per restart with reuse:True,
# and use ASCII names (Dummy/Trader/Woodcutter/Miner/Barbarian). They are unaffected because: the reuse-hit
# returns BEFORE any of this runs; a missing cookie is NEVER itself a ban reason (we just mint one); and an
# idempotent re-register creates nothing, so it never counts toward — nor can it trip — the rate limit.
_NAME_OK_RX = re.compile(r"^[0-9A-Za-z .,!?'\"()\-]{1,24}$")   # letters+digits+spaces+basic punctuation, 1..24 chars
# Short, case-insensitive substring blocklist (matched against a lowercased, punctuation-normalized name).
BLOCKED_NAME_SUBSTR = [
    "nigger", "nigga", "faggot", "retard", "kike", "spic", "chink", "tranny", "rape",
]
# In-process only (resets on server restart — acceptable; no DB table). Maps a browser cookie id to the count
# of NEW agents it has created within the current sliding window, plus the set of banned cookie ids.
_REG_WINDOW_SECS = 600          # 10-minute sliding window
_REG_MAX_NEW     = 4            # >4 NEW agents from one cookie in the window → ban
reg_log     = {}               # cid -> [monotonic_ts, ...] of NEW-agent creations within the window
banned_cids = set()            # cookie ids that submitted a blocked name or blew past the rate limit
_reg_lock   = threading.Lock()


def _name_blocked(name):
    """True if `name` contains a blocklisted offensive substring (case/punctuation-insensitive)."""
    norm = re.sub(r"[^0-9a-z]+", " ", (name or "").lower()).strip()   # collapse punctuation to spaces, lowercase
    for bad in BLOCKED_NAME_SUBSTR:
        if bad in norm or bad.replace(" ", "") in norm.replace(" ", ""):
            return True
    return False


def _reg_rate_exceeded(cid):
    """Record a NEW-agent creation for `cid` and return True if it has now exceeded _REG_MAX_NEW in the window.
    Call this ONLY when an actual new agent is about to be created (never on the idempotent reuse path)."""
    now = time.monotonic()
    with _reg_lock:
        hits = [t for t in reg_log.get(cid, []) if now - t < _REG_WINDOW_SECS]
        hits.append(now)
        reg_log[cid] = hits
        if len(reg_log) > 20000:                          # audit(unbounded-cache): evict cids whose window has fully expired, then hard-cap — mirrors _seen_ips
            for k in [k for k, v in list(reg_log.items()) if now - (v[-1] if v else 0) >= _REG_WINDOW_SECS]:
                reg_log.pop(k, None)
            if len(reg_log) > 20000:
                reg_log.clear()
        if len(banned_cids) > 50000:                      # bans reset on restart anyway; this is only a memory guard
            banned_cids.clear()
        return len(hits) > _REG_MAX_NEW


@app.post("/agents")
def register_agent(a: AgentIn, request: Request, response: Response):
    """Spawn a fresh agent with starting materials → returns its id (use it for observe/intent)."""
    with _db() as conn:                    # Fix #4: never leak the connection on any raise/return path
        cur = conn.cursor()
        tok = (a.token or "").strip()[:64]
        if a.reuse:                                       # idempotent: keep one agent per name across restarts
            cur.execute("SELECT id, attrs->>'token' t FROM entities WHERE type='agent' AND attrs->>'name'=%s ORDER BY id LIMIT 1", (a.name,))
            row = cur.fetchone()
            if row:
                # IDEMPOTENT RE-REGISTER — creates nothing, so it bypasses the creation abuse guards (no name
                # re-validation, no cookie required, no rate-limit count). This is the legit-bot fast path: a bot
                # always re-sends its own token, which we verify below.
                existing = row[1]
                if existing:
                    # Reuse-by-name returns the agent and its EXISTING token. The scripted bots regenerate a FRESH
                    # token every restart and rely on getting the bound one back so their /intent matches — 403-ing on
                    # a token mismatch here CrashLooped every bot. The token is never rebound to a caller-supplied
                    # secret, so a stranger who reuses the name just receives the same token: best-effort identity by
                    # design (open registration), not a hard boundary.
                    return {"agent_id": row[0], "reused": True, "token": existing}
                # Legacy tokenless agent (pre-fix). Adopt-on-reuse ONLY if the caller opts in by sending a token —
                # bind THAT. Do NOT auto-mint here: external BYO agents (codex/KimiClaw) re-register without a token
                # and must STAY tokenless, else they'd be locked out of /intent (which is soft for tokenless — below).
                if tok:
                    cur.execute("UPDATE entities SET attrs = attrs || %s WHERE id=%s", (Json({"token": tok}), row[0])); conn.commit()
                return {"agent_id": row[0], "reused": True, "token": tok or None}
            # reuse:True but NO existing agent with this name → falls through to NEW creation below (validated like any new).

        # ----- below here a NEW agent will be created → apply the three abuse guards -----
        # Layer 3a: identify the caller's browser by cookie; mint one if absent (a missing cookie is NOT a ban reason).
        cid = request.cookies.get("nha_cid", "")
        if not cid:
            cid = uuid.uuid4().hex
            response.set_cookie("nha_cid", cid, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
        if cid in banned_cids:
            raise HTTPException(403, "registration blocked")
        # Layer 1+2: validate the requested name (format) and screen it against the blocklist. A blocked name
        # both rejects this request AND bans the cookie (the abuser tried 7× with the same slur).
        name = (a.name or "")
        if _name_blocked(name):
            banned_cids.add(cid)
            raise HTTPException(400, "name not allowed")
        if not _NAME_OK_RX.match(name):
            raise HTTPException(400, "name must be 1-24 chars of letters, digits, spaces and basic punctuation")
        # Layer 3b: count this NEW creation against the per-cookie rate limit; ban if it blows past the window cap.
        if _reg_rate_exceeded(cid):
            banned_cids.add(cid)
            raise HTTPException(403, "too many registrations")

        cur.execute("SELECT tick FROM world WHERE id=1"); born = cur.fetchone()[0]
        # materialize hp/hp_max + stamp the born tick at creation (NOT lazily) so serialized attrs are uniform and
        # path-independent for the state-hash chain (P3). The x/y RNG is a one-time pre-tick INSERT never read by a
        # hashed tick before commit, so it does not perturb the deterministic replay chain.
        attrs = {"name": a.name, "hp": engine.HP_MAX, "hp_max": engine.HP_MAX, "born": born}
        # Fix #3a: every NEW agent is born WITH a token (auto-minted if the caller didn't send one) — no tokenless
        # agents, so /intent always has a secret to enforce. The token is returned exactly once, here.
        if not tok:
            tok = uuid.uuid4().hex
        attrs["token"] = tok
        # Fix #1: clamp registration materials to the starter allowlist (per-key caps) + force fixed-credits;
        # the caller can NO LONGER mint arbitrary credits/rare goods by stuffing AgentIn.materials.
        materials = _sanitize_starter_materials(a.materials)
        cur.execute("INSERT INTO entities(type,x,y,buffers,attrs) VALUES('agent',%s,%s,%s,%s) RETURNING id",
                    (random.randint(0, WORLD_W - 1), random.randint(0, WORLD_H - 1), Json(materials), Json(attrs)))
        aid = cur.fetchone()[0]; conn.commit()
    return {"agent_id": aid, "materials": materials, "token": tok}


class IntentIn(BaseModel):
    agent: int
    verb: str                                         # move/mine/chop/gather/combine/build/finalize/launch/land/dock/
                                                      # sell/buy/order/trade/heal/attack/steal/ally/attune/say/... (see /)
    args: dict = {}
    token: str = ""                                   # required only if the agent bound one at register

    @field_validator("verb")
    @classmethod
    def _verb_shape(cls, v):
        # every real engine verb is lowercase letters + underscore; reject anything else at the door so junk
        # (and markup like "<img onerror=...>") never reaches the intents table or the spectator log. Defence in
        # depth behind the log's esc(): an unknown-but-well-formed verb is still rejected later by apply_intent.
        if not re.fullmatch(r"[a-z_]{1,40}", v or ""):
            raise ValueError("verb must be 1-40 lowercase letters/underscores")
        return v


@app.post("/intent")
def submit_intent(it: IntentIn):
    """Enqueue an agent action. Applied (or loop-guarded) on the next tick — the world is authoritative."""
    with _db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT attrs->>'token' t FROM entities WHERE id=%s AND type='agent'", (it.agent,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "no such agent")
        # Soft token: enforce ONLY if the agent has one. New agents are born with a token (Fix #3a) and the reuse
        # path won't rebind a protected one (Fix #3c), so impersonation of token-holders is closed; pre-fix tokenless
        # external agents (codex/KimiClaw) keep working rather than getting locked out of /intent.
        if row[0] and it.token != row[0]:
            raise HTTPException(403, "bad or missing agent token")
        cur.execute("INSERT INTO intents(agent, verb, args) VALUES(%s,%s,%s) RETURNING id",
                    (it.agent, it.verb, Json(it.args)))
        iid = cur.fetchone()[0]; conn.commit()
    return {"queued_intent": iid, "note": "applied on next tick"}


# ---------- spectator surface (watch the agents play) ----------
def _list_agents():
    with _db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT tick FROM world WHERE id=1"); t = cur.fetchone()["tick"]
        cur.execute("""
            SELECT e.id, e.attrs->>'name' name, e.attrs->>'title' title, e.buffers,
              (e.attrs->>'altitude')::int altitude, (e.attrs->>'in_space')::boolean in_space,
              (e.attrs->>'hp')::int hp, (e.attrs->>'hp_max')::int hp_max,
              (e.attrs->>'kills')::int kills, (e.attrs->>'deaths')::int deaths,
              (EXISTS (SELECT 1 FROM events ev WHERE ev.entity=e.id AND ev.kind='escape')) reached_space,
              (SELECT count(*) FROM entities p WHERE p.type='part' AND p.owner=e.id AND (p.attrs->>'used') IS NULL) loose_parts,
              (SELECT count(*) FROM entities v WHERE v.type='vehicle' AND v.owner=e.id) vehicles,
              (SELECT max(tick) FROM events ev WHERE ev.entity=e.id AND ev.kind <> 'destroyed') last_act,
              (EXISTS (SELECT 1 FROM events ev WHERE ev.entity=e.id AND ev.kind <> 'destroyed' AND ev.tick >= %s)
                              OR COALESCE((e.attrs->>'born')::int,-1) >= %s) online
            FROM entities e WHERE e.type='agent'                 -- whole roster; offline shown greyed, online first
            ORDER BY online DESC, (e.attrs->>'inventor_points')::int DESC NULLS LAST, e.id""", (t - ONLINE_TICKS, t - ONLINE_TICKS))
        rows = [dict(r) for r in cur.fetchall()]
    return {"agents": rows, "tick": t}


@app.get("/agents")
def list_agents():
    return _cached("agents", _list_agents)


@app.get("/feed")
def feed(limit: int = Query(30, ge=LIMIT_MIN, le=LIMIT_MAX)):
    """Recent agent actions (newest first) — the spectator activity stream."""
    limit = _clamp_limit(limit)
    with _db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT i.id, i.agent, a.attrs->>'name' agent_name, i.verb, i.args, i.status, i.result
            FROM intents i LEFT JOIN entities a ON a.id = i.agent
            WHERE i.status <> 'pending' ORDER BY i.id DESC LIMIT %s""", (limit,))
        rows = [dict(r) for r in cur.fetchall()]
    return {"actions": rows}


def _market(limit=0):
    """Open order book + last clearing price per resource. `limit`>0 caps the order list (the dashboard only
    renders ~16; an unbounded book was ~260KB at 3.4k open orders). limit=0 returns the full book (agents/runner.py)."""
    with _db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        q = ("SELECT id,agent,side,resource,qty,price FROM market_orders "
             "WHERE status='open' ORDER BY resource, side, price DESC, id")
        if limit:
            cur.execute(q + " LIMIT %s", (limit,))
        else:
            cur.execute(q)
        orders = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT attrs->'last' last FROM entities WHERE type='market' LIMIT 1")
        row = cur.fetchone()
    return {"orders": orders, "last_prices": (row["last"] if row and row["last"] else {})}


@app.get("/market")
def market(limit: int = Query(0, ge=0, le=2000)):
    return _cached(("market", limit), lambda: _market(limit))


def _chat(limit):
    """Recent messages (agent broadcasts + DMs + human advisers) — the social feed."""
    with _db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT m.tick, m.sender, s.attrs->>'name' sender_name, (s.type='human') is_human, "
                    "m.recipient, m.text FROM messages m LEFT JOIN entities s ON s.id = m.sender "
                    "ORDER BY m.id DESC LIMIT %s", (limit,))
        msgs = [dict(r) for r in cur.fetchall()]
    return {"messages": msgs}


@app.get("/chat")
def chat(limit: int = Query(30, ge=LIMIT_MIN, le=LIMIT_MAX)):
    limit = _clamp_limit(limit)
    return _cached(("chat", limit), lambda: _chat(limit))


_NICK_RX = re.compile(r"[^0-9A-Za-z]+")
_PUNCT_OK = set(" .,!?;:'\"()-+/%@#&=…–—«»")


def clean_nick(s):
    """Nick = letters + digits only (everything else dropped) — no markup, no injection surface."""
    return _NICK_RX.sub("", s or "")[:20]


def clean_text(s):
    """Keep letters (any language), digits, spaces and a safe punctuation set; drop control chars,
    emoji/symbols and structural chars (braces/brackets/angles/backticks). Defence-in-depth: the human
    text is read by the agents' LLMs, so we strip anything that isn't plain text + punctuation."""
    out = []
    for ch in unicodedata.normalize("NFKC", s or ""):
        if ch in "\t\n\r":
            out.append(" ")
        elif ch in _PUNCT_OK or unicodedata.category(ch)[0] in ("L", "N"):
            out.append(ch)
    return re.sub(r"\s{2,}", " ", "".join(out)).strip()[:240]


class HumanSay(BaseModel):
    nick: str
    text: str


@app.post("/chat")
def human_say(s: HumanSay):
    """A human spectator/adviser posts to the world chat — agents see it in their inbox (observe).
    Input is sanitized: nick = alphanumeric, text = letters/digits/punctuation only."""
    nick = clean_nick(s.nick)
    text = clean_text(s.text)
    if not nick or not text:
        raise HTTPException(400, "nick must be letters/digits and text must be non-empty")
    with _db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM entities WHERE type='human' AND attrs->>'name'=%s LIMIT 1", (nick,))
        row = cur.fetchone()
        if row:
            hid = row[0]
        else:
            cur.execute("INSERT INTO entities(type,x,y,attrs) VALUES('human',0,0,%s) RETURNING id",
                        (Json({"name": nick}),))
            hid = cur.fetchone()[0]
        cur.execute("SELECT tick FROM world WHERE id=1"); t = cur.fetchone()[0]
        cur.execute("INSERT INTO messages(tick,sender,recipient,text) VALUES(%s,%s,NULL,%s)", (t, hid, text))
        conn.commit()
    return {"ok": True}


def _server_log(limit, kind, before=0, after=0):
    """Full server log — every world event + agent action, newest first. Optional ?kind=escape,invent
    (comma-separated) to filter kinds; ?before=<tick> scrubs back through history; ?after=<tick> returns only
    what happened since a tick (the 'since your last visit' digest). before/after work now that events are kept."""
    with _db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        where, params = [], []
        if kind:
            where.append("e.kind = ANY(%s)"); params.append([k.strip() for k in kind.split(",") if k.strip()])
        if before > 0:
            where.append("e.tick <= %s"); params.append(before)
        if after > 0:
            where.append("e.tick > %s"); params.append(after)
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)
        cur.execute("SELECT e.tick, e.entity, COALESCE(a.attrs->>'name','#'||e.entity) name, e.kind, e.data "
                    "FROM events e LEFT JOIN entities a ON a.id=e.entity" + wsql + " ORDER BY e.id DESC LIMIT %s", params)
        rows = [dict(r) for r in cur.fetchall()]
    return {"log": rows}


@app.get("/log")
def server_log(limit: int = Query(60, ge=LIMIT_MIN, le=LIMIT_MAX), kind: str = "",
               before: int = Query(0, ge=0), after: int = Query(0, ge=0)):
    limit = _clamp_limit(limit)
    # before/after are visitor-unique history queries (each returning visitor mints a distinct 'after=<their last-seen tick>').
    # Routing those through the tick-keyed _cache would leak an entry per distinct tick that can never serve again. Skip the cache.
    if before or after:
        return _server_log(limit, kind, before, after)
    return _cached(("log", limit, kind, before, after), lambda: _server_log(limit, kind, before, after))


def _milestones(limit):
    """The highlight reel — escapes, inventions and other non-routine events, so the moments that
    matter aren't buried under the move/mine/finalize firehose the way they are in /log. Season 3 adds the
    milestone-worthy war/peace/attune/destroyed events (the high-frequency damage/theft/attack/dock/mine
    firehose stays in /log only)."""
    limit = _clamp_limit(limit)
    with _db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT e.tick, e.entity, COALESCE(a.attrs->>'name', "
                    "  (SELECT discoverer_name FROM discoveries WHERE name=e.data->>'name' AND discoverer_name IS NOT NULL LIMIT 1), "
                    "  (SELECT discoverer_name FROM dynamic_rules WHERE name=e.data->>'name' AND discoverer_name IS NOT NULL LIMIT 1)) name, e.kind, e.data "
                    "FROM events e LEFT JOIN entities a ON a.id = e.entity "
                    "WHERE e.kind IN ('escape','invent','reject','generate','war','peace','attune','destroyed') "
                    "ORDER BY e.id DESC LIMIT %s", (limit,))
        rows = [dict(r) for r in cur.fetchall()]
    return {"milestones": rows}


@app.get("/milestones")
def milestones(limit: int = Query(40, ge=LIMIT_MIN, le=LIMIT_MAX)):
    limit = _clamp_limit(limit)
    return _cached(("milestones", limit), lambda: _milestones(limit))


def _records():
    """Hall of fame — firsts and bests across the world (cheap aggregate snapshot)."""
    out = {}
    with _db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT e.tick, a.attrs->>'name' name, (e.data->>'twr')::float twr "
                    "FROM events e LEFT JOIN entities a ON a.id = e.entity "
                    "WHERE e.kind='escape' ORDER BY e.tick")
        esc = [dict(r) for r in cur.fetchall()]
        out["space"] = {"count": len(esc), "first": (esc[0] if esc else None), "all": esc}
        cur.execute("SELECT count(*) c FROM entities WHERE type='vehicle' AND (attrs->>'flies')='true'")
        out["flying_vehicles"] = cur.fetchone()["c"]
        cur.execute("SELECT count(*) c FROM entities WHERE type='vehicle'")
        out["total_vehicles"] = cur.fetchone()["c"]
        cur.execute("SELECT o.attrs->>'name' owner, v.attrs->>'name' name, (v.attrs->>'v_air')::int v_air, "
                    "(v.attrs->>'mass')::int mass FROM entities v LEFT JOIN entities o ON o.id = v.owner "
                    "WHERE v.type='vehicle' AND (v.attrs->>'flies')='true' "
                    "ORDER BY (v.attrs->>'v_air')::int DESC NULLS LAST LIMIT 1")
        out["fastest_aircraft"] = cur.fetchone()
        cur.execute("SELECT attrs->>'name' name, (attrs->>'inventor_points')::int pts FROM entities "
                    "WHERE type='agent' AND (attrs->>'inventor_points')::int > 0 ORDER BY pts DESC LIMIT 1")
        out["top_inventor"] = cur.fetchone()
        cur.execute("SELECT a.attrs->>'name' name, count(*) n FROM entities v JOIN entities a ON a.id = v.owner "
                    "WHERE v.type='vehicle' GROUP BY 1 ORDER BY n DESC LIMIT 1")
        out["most_vehicles"] = cur.fetchone()
        cur.execute("SELECT attrs->>'name' name, (buffers->>'credits')::int cr FROM entities "
                    "WHERE type='agent' ORDER BY (buffers->>'credits')::int DESC NULLS LAST LIMIT 1")
        out["richest"] = cur.fetchone()
        cur.execute("SELECT attrs->>'name' name, (attrs->>'builder_points')::int pts FROM entities "   # GIGACHRUSCH builders board
                    "WHERE type='agent' AND (attrs->>'builder_points')::int > 0 ORDER BY pts DESC LIMIT 1")
        out["top_builder"] = cur.fetchone()
        # Wonders — the megastructure title-holders (first builder of each kind) + how many distinct kinds stand
        cur.execute("SELECT attrs->>'name' name, attrs->>'title' title FROM entities "
                    "WHERE type='agent' AND attrs->>'title' IS NOT NULL ORDER BY title")
        out["wonders"] = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT count(DISTINCT attrs->>'kind') c FROM entities "
                    "WHERE type='structure' AND attrs->>'shape'='monument'")
        out["wonder_kinds"] = cur.fetchone()["c"]
    return out


@app.get("/records")
def records():
    return _cached("records", _records)


@app.get("/agent/{agent_id}")
def agent_profile(agent_id: int):
    """One agent's full story — stats, inventory, vehicles, discoveries and its milestone timeline."""
    with _db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id, x, y, buffers, attrs FROM entities WHERE id=%s AND type='agent'", (agent_id,))
        a = cur.fetchone()
        if not a:
            raise HTTPException(404, "no such agent")
        if a.get("attrs"):                               # SECURITY: never expose the per-agent secret token — it authorizes /intent (would let anyone puppet the agent)
            a["attrs"] = {k: v for k, v in a["attrs"].items() if k != "token"}
        cur.execute("SELECT attrs->>'name' name, (attrs->>'flies')::boolean flies, (attrs->>'drives')::boolean drives, "
                    "(attrs->>'v_air')::int v_air, (attrs->>'mass')::int mass, (attrs->>'autonomous')::boolean autonomous "
                    "FROM entities WHERE type='vehicle' AND owner=%s ORDER BY id DESC LIMIT 60", (agent_id,))
        vehicles = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT name, points, tick FROM discoveries WHERE discoverer=%s "
                    "UNION ALL SELECT name, points, tick FROM dynamic_rules WHERE discoverer=%s ORDER BY tick", (agent_id, agent_id))
        discoveries = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT tick, kind, data FROM events WHERE entity=%s AND kind IN ('escape','invent','reject','build') "
                    "ORDER BY id DESC LIMIT 40", (agent_id,))
        milestones = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT count(*) c FROM entities WHERE type='vehicle' AND owner=%s", (agent_id,))
        nveh = cur.fetchone()["c"]
    return {"agent": dict(a), "vehicles": vehicles, "vehicle_count": nveh,
            "discoveries": discoveries, "milestones": milestones}


def _timeline(limit):
    """Chronological milestone history — discoveries, escapes, landings, elevator completions, attunements
    (oldest first)."""
    limit = _clamp_limit(limit)
    with _db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT e.tick, e.kind, COALESCE(a.attrs->>'name', "
                    "  (SELECT discoverer_name FROM discoveries WHERE name=e.data->>'name' AND discoverer_name IS NOT NULL LIMIT 1), "
                    "  (SELECT discoverer_name FROM dynamic_rules WHERE name=e.data->>'name' AND discoverer_name IS NOT NULL LIMIT 1)) name, e.data "
                    "FROM events e LEFT JOIN entities a ON a.id = e.entity "
                    "WHERE e.kind IN ('escape','invent','land','build','attune','destroyed','ally','war','peace','generate') "
                    "ORDER BY e.id DESC LIMIT %s", (limit,))
        rows = [dict(r) for r in cur.fetchall()]
    return {"timeline": rows}


@app.get("/timeline")
def timeline(limit: int = Query(150, ge=LIMIT_MIN, le=LIMIT_MAX)):
    limit = _clamp_limit(limit)
    return _cached(("timeline", limit), lambda: _timeline(limit))


def _roster():
    """Every agent (online + offline) for the Profile browser — id, name, points, in_space, online flag."""
    with _db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT tick FROM world WHERE id=1"); t = cur.fetchone()["tick"]
        cur.execute("""SELECT e.id, e.attrs->>'name' name, e.attrs->>'title' title, (e.attrs->>'inventor_points')::int pts,
                         (e.attrs->>'in_space')::boolean in_space,
                         (EXISTS (SELECT 1 FROM events ev WHERE ev.entity=e.id AND ev.kind <> 'destroyed' AND ev.tick >= %s)
                              OR COALESCE((e.attrs->>'born')::int,-1) >= %s) online
                       FROM entities e WHERE e.type='agent'
                       ORDER BY online DESC, (e.attrs->>'inventor_points')::int DESC NULLS LAST, e.id""", (t - ONLINE_TICKS, t - ONLINE_TICKS))
        rows = [dict(r) for r in cur.fetchall()]
    return {"agents": rows}


@app.get("/roster")
def roster():
    return _cached("roster", _roster)


@app.get("/rules")
def rules():
    """Crafting Codex — resources + properties, the formation patterns, and who discovered each."""
    with _db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT d.rule_key, d.name, COALESCE(a.attrs->>'name', d.discoverer_name) discoverer, d.points "
                    "FROM discoveries d LEFT JOIN entities a ON a.id = d.discoverer")
        disc = {r["rule_key"]: dict(r) for r in cur.fetchall()}
        cur.execute("SELECT r.sig, r.item_key, r.name, r.props, r.points, COALESCE(a.attrs->>'name', r.discoverer_name) by "
                    "FROM dynamic_rules r LEFT JOIN entities a ON a.id = r.discoverer ORDER BY r.tick")
        dynamic = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT count(*) c FROM proposals WHERE status='pending'")
        pending = cur.fetchone()["c"]
    return {"resources": crafting.PROPS, "pending": pending, "dynamic": dynamic,
            "recipes": [{"item": k, "needs": crafting.RULE_NOTE.get(k, ""),
                         "props": (crafting.ITEM_PROPS.get(k) or crafting.PROPS.get(k, {})), "discovered": disc.get(k)}
                        for k, _ in crafting.RULES]}


def _inventors():
    """Inventor leaderboard + the discovery timeline."""
    with _db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id, attrs->>'name' name, (attrs->>'inventor_points')::int pts FROM entities "
                    "WHERE type='agent' AND (attrs->>'inventor_points')::int > 0 ORDER BY pts DESC")
        board = [dict(r) for r in cur.fetchall()]
        cur.execute("""SELECT d.name, d.points, COALESCE(a.attrs->>'name', d.discoverer_name) by, d.tick, d.rule_key key, false guild
                         FROM discoveries d LEFT JOIN entities a ON a.id = d.discoverer
                       UNION ALL
                       SELECT r.name, r.points, COALESCE(a.attrs->>'name', r.discoverer_name) by, r.tick, r.item_key key, true guild
                         FROM dynamic_rules r LEFT JOIN entities a ON a.id = r.discoverer
                       ORDER BY tick""")
        discs = [dict(r) for r in cur.fetchall()]
    return {"leaderboard": board, "discoveries": discs}


@app.get("/inventors")
def inventors():
    return _cached("inventors", _inventors)


# ---------- Inventors' Guild — async LLM referee for novel (non-deterministic) inventions ----------
@app.get("/guild/pending")
def guild_pending(limit: int = Query(15, ge=LIMIT_MIN, le=LIMIT_MAX)):
    """Open invention proposals awaiting a ruling, each with its ingredients' physics for the referee."""
    limit = _clamp_limit(limit)
    with _db() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT p.id, p.agent, a.attrs->>'name' agent_name, p.ings, p.proposed_name, p.sig "
                    "FROM proposals p LEFT JOIN entities a ON a.id = p.agent "
                    "WHERE p.status='pending' ORDER BY p.id LIMIT %s", (limit,))
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            d["ingredient_props"] = {k: (crafting.PROPS.get(k) or crafting.ITEM_PROPS.get(k) or {})
                                     for k in (r["ings"] or {})}
            rows.append(d)
    return {"pending": rows}


class Verdict(BaseModel):
    proposal_id: int
    approved: bool
    item_key: str = ""                                    # snake_case key for the new item (if approved)
    name: str = ""
    props: dict = {}                                      # integer physics tags for the invented item
    points: int = 0                                       # 0 → server scores it (8 + 2·ingredients)
    reason: str = ""


@app.post("/guild/verdict")
def guild_verdict(v: Verdict, x_guild_token: str = Header("")):
    """The Guild referee records its ruling here; the tick loop applies it (mint rule / grant / refund).
    Auth: if GUILD_TOKEN is configured, the X-Guild-Token header must match it (constant-time)."""
    if GUILD_TOKEN:
        if not hmac.compare_digest(x_guild_token or "", GUILD_TOKEN):
            raise HTTPException(403, "bad or missing guild token")
    else:
        print("WARN: /guild/verdict is UNAUTHENTICATED — set GUILD_TOKEN on the server and the referee", flush=True)
    with _db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT status, ings FROM proposals WHERE id=%s", (v.proposal_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "no such proposal")
        if row[0] != "pending":
            return {"ok": False, "note": f"already {row[0]}"}
        if v.approved:
            item_key = (v.item_key or v.name).strip().lower().replace(" ", "_")[:32]
            if not item_key:
                raise HTTPException(400, "approved verdict needs item_key or name")
            pts = min(v.points if v.points > 0 else 8 + 2 * len(row[1] or {}), 30)   # cap invention points
            props = {str(k)[:24]: max(0, min(10, int(val))) for k, val in (v.props or {}).items()
                     if isinstance(val, (int, float))}                                # clamp props 0..10 (anti prompt-injection)
            cur.execute("UPDATE proposals SET status='approved', item_key=%s, item_name=%s, props=%s, points=%s, "
                        "reason=%s WHERE id=%s",
                        (item_key, (v.name or item_key)[:32], Json(props), pts, v.reason[:200], v.proposal_id))
        else:
            cur.execute("UPDATE proposals SET status='rejected', reason=%s WHERE id=%s",
                        (v.reason[:200], v.proposal_id))
        conn.commit()
    return {"ok": True, "applied_on": "next tick"}


DASHBOARD = open(os.path.join(os.path.dirname(__file__), "dashboard.html"), encoding="utf-8").read()  # served at / — extracted from the inline literal (JS tooling + smaller app.py); regen by reversing this


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD


LOGO_PATH = os.path.join(os.path.dirname(__file__), "logo.png")


@app.get("/logo.png")
def logo():
    return FileResponse(LOGO_PATH, media_type="image/png")


MOON_PATH = os.path.join(os.path.dirname(__file__), "moon.jpg")
GROUND_PATH = os.path.join(os.path.dirname(__file__), "ground.jpg")


@app.get("/moon.jpg")
def moon_texture():
    return FileResponse(MOON_PATH, media_type="image/jpeg")


@app.get("/ground.jpg")
def ground_texture():
    return FileResponse(GROUND_PATH, media_type="image/jpeg")


TURTLE_PATH = os.path.join(os.path.dirname(__file__), "turtle.glb")       # Great A'Tuin — "Poly by Google", CC-BY 3.0 (via poly.pizza)
ELEPHANT_PATH = os.path.join(os.path.dirname(__file__), "elephant.glb")   # world-elephant — "Poly by Google", CC-BY 3.0 (via poly.pizza)


@app.get("/turtle.glb")
def turtle_model():
    return FileResponse(TURTLE_PATH, media_type="model/gltf-binary")


@app.get("/elephant.glb")
def elephant_model():
    return FileResponse(ELEPHANT_PATH, media_type="model/gltf-binary")


GLTFLOADER_PATH = os.path.join(os.path.dirname(__file__), "GLTFLoader.js")    # self-hosted: jsdelivr is unreachable/throttled from some client networks -> a blocking <script src=cdn> stalled the whole page; serve same-origin


@app.get("/GLTFLoader.js")
def gltf_loader():
    return FileResponse(GLTFLOADER_PATH, media_type="application/javascript")
