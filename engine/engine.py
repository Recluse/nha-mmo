#!/usr/bin/env python3
"""NHA-MMO — lightweight tick engine, PostgreSQL-backed (MVP skeleton).

World state lives in Postgres (see schema.sql). Each tick:
  1. apply pending agent intents (with an engine-enforced loop guard),
  2. run each component's tiny per-tick behavior (integer-conserved resource flows + transforms),
  3. advance the tick, append events, and record a deterministic state-hash (audit/replay chain).

Processing is in-memory per tick (load -> mutate -> write back) so it's cheap; Postgres is the
durable, authoritative store. Self-creates the schema and seeds a tiny demo world if empty.

Run:  PG_DSN='host=127.0.0.1 dbname=nhamoo user=postgres' python engine.py [ticks]
"""
import os, sys, json, hashlib, time, re
import psycopg2
from psycopg2.extras import RealDictCursor, Json, execute_batch
import vehicles   # PART / BUILD_COST / finalize() — for the build & finalize intents
import crafting   # PROPS / RULES / combine() — emergent physics crafting

DSN = os.environ.get("PG_DSN", "host=127.0.0.1 dbname=nhamoo user=postgres")
LOOP_N = 3   # engine-enforced: reject an intent identical to the agent's last LOOP_N applied ones
GRAVITY = 4              # a vehicle lifts off only if thrust >= GRAVITY * mass (the grand-goal gate)
ATMOSPHERE_TOP = 100     # altitude that counts as "escaped the atmosphere" — first space milestone
CLIMB = 10               # altitude gained per fueled launch
SPACE_TIERS = [(100, "space"), (300, "orbit"), (600, "the Moon")]   # altitude → milestone (escalating goals beyond escape)
SKY_TOP = 600            # max altitude — reaching the Moon
ZIG_TOP = 120            # ziggurat completion height — a Moon-only collaborative megastructure
DESCEND = 40             # altitude shed per `land` (controlled descent back home)

# ===================== SEASON 3 CONSTANTS (all integers) =====================
# --- combat / HP (unified across weapons + death + alliances) ---
HP_MAX = 100
HP_BY_TYPE = {"agent": 100, "vehicle": 120, "structure": 200, "goose": 8}
HP_REGEN = 2                  # agents only, per tick, up to HP_MAX, only when not at war this tick AND not downed
ATTACK_RANGE = 6              # Manhattan (default weapon reach)
WEAPON_RANGE_KINETIC = 6
WEAPON_RANGE_ENERGY = 9
WEAPON_STATS = {
    "kinetic_gun":  {"dmg": 18, "rng": 6, "los": True,  "cd": 2, "ammo": "slug",        "ammo_n": 1, "aoe": 0},
    "energy_weapon": {"dmg": 12, "rng": 9, "los": True,  "cd": 3, "ammo": "energy_cell", "ammo_n": 1, "aoe": 0},
    "bomb":         {"dmg": 40, "rng": 1, "los": False, "cd": 5, "ammo": None,          "ammo_n": 0, "aoe": 2},
}
EXPLOSION_MAX_RADIUS = 3
CRATER_DEPOSIT_HIT = 4        # a deposit inside a blast loses at most this much (self-heals via respawn_deposits)
BOMBS_PER_CELL_MAX = 2        # anti stacked-nuke
ARMOR_VEHICLE_DIV = 40        # vehicle armor = mass // 40
MIN_EFF_DMG = 1
RESPAWN_AGENT_TICKS = 30
RESPAWN_GRACE = 8             # post-respawn ticks an agent cannot be targeted
# --- death / loot / drop ---
DEATH_COOLDOWN = RESPAWN_AGENT_TICKS
LOOT_TTL = 120
DROP_FRACTION = 4            # drop 1/DROP_FRACTION of each material on death
DROP_CAP = 50               # ...capped per material
FALL_FATAL_ALT = 300
FALL_DMG_DIV = 4
# --- theft ---
THEFT_COOLDOWN = 12
STEAL_BASE_PCT = 45
STEAL_MIN_PCT = 10
STEAL_MAX_PCT = 80
DETECT_MARGIN = 25
STEAL_FLOOR = 4             # victim must hold at least this much of the resource
STEAL_MAX_ABS = 8          # absolute cap on a single steal
NOTORIETY_HIT = 2
NOTORIETY_CAP = 50
NOTORIETY_DECAY_EVERY = 30
VIGIL_GAIN = 3
VIGIL_CAP = 35
VIGIL_DECAY_EVERY = 20
WANTED_TTL = 60
# --- alliances / war ---
ALLY_COOLDOWN = 30
OFFER_TTL = 80
PEACE_TTL = 40
ALLY_AID_RADIUS = 2
ALLY_AID_DIV = 4
ASSIST_CAP = 50            # max qty per assist gift
ASSIST_PER_WINDOW = 2      # max assists per giver per ASSIST_WINDOW
ASSIST_WINDOW = 60
PROTECT_WEALTH = 30        # a young agent below this credit wealth is protected from attack/steal
PROTECT_AGE = 200          # ...while age (t - born) is below this
WAR_REDECLARE_COOLDOWN = 120
WEARINESS_CAP = 50
# --- worldgen season 3 ---
N_ASTEROIDS = 12
ASTEROID_MINE_CAP = 5
ASTEROID_RESPAWN_EVERY = 20
DOCK_RANGE = 2
ORBIT_LO = 300
ORBIT_HI = 600
ART_MAX_MONOLITH = 1
ART_MAX_OTHER = 3
ART_FIRST_PTS = 200
ART_PTS = 60
LENS_WINDOW = 200
STASIS_CHARGES = 3
# --- combat scoring (SEPARATE field, never pollutes inventor_points) ---
COMBAT_PTS_KILL = 10
COMBAT_PTS_PAIR_WINDOW = 200   # no re-award vs the same victim within this window
# --- botany / medicine (HP healing, increment 2) ---
PLANT_RESOURCES = ("herb", "lichen", "fungus", "algae")   # gatherable plant deposits (renewable, like wood)
GATHER_RANGE = 8                  # auto-walk reach to the nearest plant deposit (mirrors chop)
PLANT_REGROW_CAP = 18            # plant deposits regrow up to this (like respawn_deposits' mineral cap)
PLANT_REGROW_EVERY = 8           # ...one unit every this-many ticks (staggered by id), like grow_trees
MEDICINES = ("salve", "stimpack", "medkit", "antidote")   # consumable HP medicines (heal value lives in crafting.ITEM_PROPS)
HEAL_RANGE = 6                    # Manhattan reach to heal/revive another agent
STIMPACK_BUFF_TICKS = 20         # a stimpack's short deterministic regen/speed buff window
REVIVE_HP = 20                   # hp a medkit-revived downed ally comes back with
# --- deposit-richness variance (mining yield jitter) ---
KARMA_WINDOW = 400
KARMA_MAX = 6
KARMA_DIV = 4
COOP_THRESH = 6
_NF_W_MARKET = 2
# --- geese (shoreline hazard, increment) ---
GOOSE_FLOCKS = 5                 # one-time deterministic spawn: this many gaggles, SPREAD across the map (farthest-point) at water deposits
GOOSE_PER_FLOCK_MIN = 3         # each gaggle holds MIN..MAX geese (count derived per-flock from _h, no RNG)
GOOSE_PER_FLOCK_MAX = 5
GOOSE_WATER_RES = ("water", "brine", "salt", "ice", "algae")   # water-ish deposits geese anchor to (sea/coast)
GOOSE_ROAM = 3                  # a goose waddles within this Chebyshev radius of its anchor cell
GOOSE_HONK_EVERY = 5           # one flock honks every this-many ticks (deterministic, staggered by flock id)
GOOSE_PECK_MIN_CLUSTER = 2     # an agent on/adjacent to this many geese of a flock gets pecked
GOOSE_PECK_MAX = 5            # peck damage is capped at this (scales with cluster size, never one-shots)
COMBINE_BATCH_MAX = 20       # combine{n} may craft up to this many copies of an ALREADY-KNOWN recipe in one intent (grind relief)
AUTO_MINE_EVERY = 3          # a deployed autonomous vehicle harvests on every this-many-th tick (deterministic, staggered by id)
AUTO_MINE_N = 3              # units a mining rig scoops per harvest tick (ground deposit it's on/near, or a docked asteroid in orbit)
# ============================================================================

# ===================== SEASON 4 — THE SPACE ERA (co-op orbital station) =====================
# One SHARED orbital station, assembled from 6 modules. Cooperation is STRUCTURAL, not optional:
#   • each module carries a big, DIVERSE bill of materials;
#   • one agent may fund at most STATION_CAP_FRAC% of ANY single resource in a module → >=3 distinct
#     funders are mathematically required per resource (2*40% = 80% < 100%);
#   • a module also needs >= STATION_MIN_CONTRIB distinct funders before it can complete;
#   • the station is done only when ALL modules are. Buildable only while IN SPACE, only in the 'space' era.
# Amounts are tunable; the world.era flag (default 'architect') gates the whole season on/off.
STATION_CAP_FRAC = 40              # max % of any one resource a single agent may fund in a module
STATION_MIN_CONTRIB = 3            # min distinct funders a module needs before it can complete
STATION_MODULE_REWARD = 160        # inventor_points pool split (by share) among a module's funders on completion
STATION_FINISH_REWARD = 1400       # inventor_points mega-pool split among ALL funders when the LAST module lands
# ---- Expansion-Era financing: rich agents `invest{module,credits}` — credits buy the scarce materials at a FIXED
# price (never the live depot, so a whale can't move prices) and fund a station module under the SAME 40% cap. ----
EXPANSION_PRICES = {"titanium": 14, "composite": 14, "chip": 24, "metal": 10, "silicon": 12, "crystal": 16, "ice": 2, "iridium": 40}
INVEST_TICK_CAP = 25000            # max credits one agent may sink into a module in a single tick (rate limit — no one-tick hoard dump)
STATION_MODULES = {                # insertion order is fixed → deterministic display/iteration
    # 2026-06-23 rebalance: composite (a 1-craft/tick item) was the grind wall, so its amounts were cut and
    # a CHIP requirement added (chip = silicon + a conductor metal, e.g. copper — both mineable). This diversifies
    # the electronics side of the bill and lowers the total crafted-units burden. `life` is already COMPLETE and
    # has no composite, so it is left untouched.
    "truss":   {"label": "Core Truss",   "need": {"metal": 800, "titanium": 420, "composite": 240, "chip": 60}},
    "solar":   {"label": "Solar Array",  "need": {"silicon": 700, "crystal": 400, "composite": 180, "chip": 150}},
    "habitat": {"label": "Habitat Ring", "need": {"metal": 720, "composite": 240, "ice": 300, "chip": 60}},
    "lab":     {"label": "Science Lab",  "need": {"crystal": 480, "silicon": 460, "iridium": 120, "chip": 180}},   # iridium scarce (asteroids only) → kept modest so the Lab can't soft-lock
    "dock":    {"label": "Docking Port", "need": {"titanium": 460, "metal": 560, "composite": 200, "chip": 80}},
    "life":    {"label": "Life Support", "need": {"ice": 420, "crystal": 440, "silicon": 380}},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS world (id int PRIMARY KEY DEFAULT 1, tick int NOT NULL DEFAULT 0);
ALTER TABLE world ADD COLUMN IF NOT EXISTS notices jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE world ADD COLUMN IF NOT EXISTS era text NOT NULL DEFAULT 'architect';
INSERT INTO world (id, tick) VALUES (1,0) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS entities (id bigserial PRIMARY KEY, type text NOT NULL,
  x int NOT NULL DEFAULT 0, y int NOT NULL DEFAULT 0, owner bigint,
  buffers jsonb NOT NULL DEFAULT '{}', attrs jsonb NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS entities_type_idx ON entities(type);
CREATE INDEX IF NOT EXISTS entities_owner_idx ON entities(owner) WHERE owner IS NOT NULL;
CREATE TABLE IF NOT EXISTS intents (id bigserial PRIMARY KEY, agent bigint NOT NULL,
  verb text NOT NULL, args jsonb NOT NULL DEFAULT '{}', status text NOT NULL DEFAULT 'pending',
  result text, created int);
CREATE INDEX IF NOT EXISTS intents_agent_idx ON intents(agent, id);
CREATE TABLE IF NOT EXISTS events (id bigserial PRIMARY KEY, tick int NOT NULL,
  entity bigint, kind text NOT NULL, data jsonb NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS events_kind_tick_idx ON events(kind, tick);
CREATE INDEX IF NOT EXISTS events_entity_kind_tick_idx ON events(entity, kind, tick);   -- per-agent EXISTS/max(tick) subqueries in /agents,/scene,/roster (filter by entity+kind, order by tick)
CREATE INDEX IF NOT EXISTS events_entity_id_idx ON events(entity, id);   -- /agent/{id} recent[] feed: WHERE entity=%s ORDER BY id DESC LIMIT N (bounded index scan; matters now events are full-history)
CREATE TABLE IF NOT EXISTS tick_hashes (tick int PRIMARY KEY, hash text NOT NULL);
CREATE TABLE IF NOT EXISTS market_orders (id bigserial PRIMARY KEY, agent bigint NOT NULL,
  side text NOT NULL, resource text NOT NULL, qty int NOT NULL, price int NOT NULL,
  status text NOT NULL DEFAULT 'open', created int);
CREATE INDEX IF NOT EXISTS market_open_idx ON market_orders(resource, side, status);
CREATE TABLE IF NOT EXISTS trades (id bigserial PRIMARY KEY, proposer bigint NOT NULL,
  target bigint NOT NULL, give jsonb NOT NULL, want jsonb NOT NULL,
  status text NOT NULL DEFAULT 'open', created int);
CREATE TABLE IF NOT EXISTS contracts (id bigserial PRIMARY KEY, poster bigint NOT NULL,
  reward jsonb NOT NULL, want jsonb NOT NULL, target bigint, kind text NOT NULL DEFAULT 'supply',
  status text NOT NULL DEFAULT 'open', fulfiller bigint, created int, deadline int);
CREATE INDEX IF NOT EXISTS contracts_open_idx ON contracts(status);   -- open-board scan in observe() + expire_contracts
ALTER TABLE contracts ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'supply';   -- migrate a pre-bounty contracts table (CREATE above is a no-op once it exists); also applied out-of-band on the live world
CREATE TABLE IF NOT EXISTS rule_updates (id bigserial PRIMARY KEY, tick int NOT NULL DEFAULT 0,
  title text NOT NULL, detail text NOT NULL DEFAULT '', verb text);   -- operator-pushed "what's new" changelog: reaches agents via observe.updates + spectators via /updates
CREATE TABLE IF NOT EXISTS messages (id bigserial PRIMARY KEY, tick int NOT NULL,
  sender bigint NOT NULL, recipient bigint, text text NOT NULL);
CREATE TABLE IF NOT EXISTS discoveries (rule_key text PRIMARY KEY, name text NOT NULL,
  discoverer bigint NOT NULL, discoverer_name text, tick int NOT NULL, points int NOT NULL DEFAULT 0);
ALTER TABLE discoveries ADD COLUMN IF NOT EXISTS discoverer_name text;
CREATE TABLE IF NOT EXISTS proposals (id bigserial PRIMARY KEY, agent bigint NOT NULL,
  ings jsonb NOT NULL, sig text NOT NULL, proposed_name text,
  status text NOT NULL DEFAULT 'pending', item_key text, item_name text, props jsonb,
  points int, reason text, tick int NOT NULL);
CREATE INDEX IF NOT EXISTS proposals_status_idx ON proposals(status);
CREATE TABLE IF NOT EXISTS dynamic_rules (sig text PRIMARY KEY, item_key text NOT NULL, name text NOT NULL,
  props jsonb NOT NULL DEFAULT '{}', discoverer bigint NOT NULL, discoverer_name text, points int NOT NULL DEFAULT 0, tick int NOT NULL);
ALTER TABLE dynamic_rules ADD COLUMN IF NOT EXISTS discoverer_name text;
-- audit: the persisted biome-grid cache (server/app.py::_grid). Was only ever created implicitly by the app's
-- INSERT; version it here so a fresh DB gets it and operators have a schema reference (else _grid silently
-- falls back to the ~12-30s worldgen on every request).
CREATE TABLE IF NOT EXISTS world_grid (seed int NOT NULL, fx int NOT NULL, fy int NOT NULL, grid jsonb NOT NULL, PRIMARY KEY (seed, fx, fy));
"""

# ---------- buffer helpers (integer, conserved) ----------
def get(e, r):  return int(e["buffers"].get(r, 0))
def addb(e, r, n): e["buffers"][r] = int(e["buffers"].get(r, 0)) + int(n)

# ---------- builder-economy diversity bonuses (THE ARCHITECT'S ERA) — pure deterministic per-agent state ----------
def _diversity_bonus(a, cost):
    """Register the materials consumed this build into the agent's durable `materials_used` set (monotonic,
    de-duped, capped 18). Returns +5 once the agent has built with 3+ distinct materials — on THIS build and
    every future one (no reset → can't be farmed by recycling the same pair)."""
    used = a["attrs"].setdefault("materials_used", [])
    for r in sorted(cost):                   # sorted → materials_used order is independent of cost-dict construction (replay-stable)
        if r != "credits" and r not in used:
            used.append(r)
    if len(used) > 18:
        del used[18:]
    return 5 if len(used) >= 3 else 0

def _shape_bonus(a, shape):
    """+3 if `shape` is not among the agent's 5 most-recent built shapes, then push it onto a length-10 recency
    deque. Rewards continuous rotation; a build-each-once-then-spam loop only scores on genuine novelty."""
    rec = a["attrs"].setdefault("shapes_recent", [])
    bonus = 0 if shape in rec[-5:] else 3
    rec.append(shape)
    if len(rec) > 10:
        del rec[0:len(rec) - 10]
    return bonus

# ---------- entity create/delete (keeps the in-memory ents dict in lock-step with the DB) ----------
def new_entity(ents, cur, tp, x, y, owner, attrs):
    """INSERT a new entity AND register it in ents BEFORE state_hash(ents) runs, so tick-time
    creations (bomb/loot/relation/asteroid/artifact) are hashed + persisted by the same write-back loop."""
    cur.execute("INSERT INTO entities(type,x,y,owner,attrs) VALUES(%s,%s,%s,%s,%s) "
                "RETURNING id,type,x,y,owner,buffers,attrs", (tp, x, y, owner, Json(attrs)))
    row = cur.fetchone()
    ents[row["id"]] = dict(row)
    return row["id"]

def del_entity(ents, cur, eid):
    """Delete an entity from both the DB and the in-memory ents dict (collect/decay/expire)."""
    cur.execute("DELETE FROM entities WHERE id=%s", (eid,))
    ents.pop(eid, None)

def _entity_canon(e):
    """Canonical per-entity JSON string — the shared unit of BOTH the state hash and per-tick change detection.
    P0: computed ONCE per entity per tick and reused for both, instead of ~5 separate json.dumps (2 for the
    dirty snapshot + 2 for the dirty diff + 1 for the hash). token = API-owned, excluded (as before)."""
    return json.dumps([e["id"], e["type"], e["x"], e["y"], e.get("owner"), e["buffers"],
                       {k: v for k, v in e["attrs"].items() if k != "token"}],
                      sort_keys=True, separators=(",", ":"), default=str)

def _world_hash(canon_by_id):
    """sha256 of the id-sorted per-entity canons — byte-IDENTICAL to the old json.dumps(list-of-lists) form
    (json list == '['+','.join(dumps(elem))+']' with these separators), so the tick_hash chain is unchanged."""
    return hashlib.sha256(("[" + ",".join(canon_by_id[i] for i in sorted(canon_by_id)) + "]").encode()).hexdigest()[:16]

def state_hash(ents):
    """Deterministic 16-hex digest of world state → per-tick audit/replay chain (same inputs ⇒ same hash)."""
    return _world_hash({e["id"]: _entity_canon(e) for e in ents.values()})

def depot_price(depot, r):
    """Floating depot price for resource r: glut (recent sells) pushes the buy price down."""
    base = depot["attrs"]["base"].get(r)
    if base is None:
        return None
    g = depot["attrs"].get("glut", {}).get(r, 0)
    buy = max(1, base * 10 // (10 + g))          # more glut → lower buy price
    return {"buy": buy, "sell": buy + base}      # depot resells at buy + base markup

# ---------- per-component behaviors (run each tick) ----------
def behave(e):
    """Per-tick component behavior. Only the depot (floating-price market maker) remains live."""
    if e["type"] == "depot":
        glut = e["attrs"].setdefault("glut", {})
        for r in list(glut):
            glut[r] = glut[r] * 4 // 5                    # decay the glut 20%/tick → prices recover
            if glut[r] <= 0:
                del glut[r]
        e["attrs"]["prices"] = {r: depot_price(e, r) for r in e["attrs"]["base"]}   # publish for spectator

# ---------- intents (the only agent->world channel) ----------
def _ai(args, key, default=0):
    """Coerce an agent-supplied arg to int; junk (dict/list/non-numeric/None) -> default. Agents occasionally
    send a dict/garbage where a number is expected — never let that raise (it surfaces as a generic 'bad intent')."""
    try:
        return int(args.get(key, default))
    except (TypeError, ValueError):
        return default

def _aid(args, key):
    """Resolve an arg that names an entity id to an int (or None) — tolerates string ids ('5' -> 5) and rejects
    unhashable junk (dict/list) so ents.get() never raises."""
    try:
        return int(args.get(key))
    except (TypeError, ValueError):
        return None

def apply_intent(it, ents, cur, t, events):
    a, args, verb = ents.get(it["agent"]), it["args"], it["verb"]
    if not a:
        return "rejected", "no agent"
    # world dimensions always come from the market entity (never bare w,h) — same idiom as move/mine
    mkt = next((x for x in ents.values() if x["type"] == "market"), None)
    W = int(mkt["attrs"].get("w", 156)) if mkt else 156
    H = int(mkt["attrs"].get("h", 156)) if mkt else 156
    # DEAD/DOWNED gate: a downed agent may ONLY talk until it respawns (whitelist EXACTLY say/tell)
    if int(a["attrs"].get("downed_until", 0)) > t and verb not in ("say", "tell"):
        return "rejected", "you are downed — you can only say/tell until you respawn"
    # NOTE: 'grab'/'transfer' removed — they moved resources between arbitrary agents on a bare
    # balance check (no ownership/adjacency), letting any agent drain another and bypass the steal
    # system. No containers exist in the live world, so they served no legitimate purpose.
    if verb == "deposit":                                # self-scoped no-op stash (src==dst==self, conserved)
        r, n = args["resource"], _ai(args, "n", 1)
        if n < 1:
            return "rejected", "quantity must be positive"   # guard: negative n would reverse the flow (dupe/steal)
        if get(a, r) >= n:
            return "applied", f"stashed {n} {r} (self-scoped no-op — your balance is unchanged)"
        return "rejected", "insufficient"
    if verb == "build":                                  # craft one part, optionally upgraded with crafted items
        part = args.get("part"); cost = vehicles.BUILD_COST.get(part)
        if not cost:
            return "rejected", f"unknown part {part}"
        ups = args.get("with") or []
        if isinstance(ups, str):
            ups = [ups]
        ups = [str(u) for u in ups][:3]
        allowed = vehicles.PART_UPGRADES.get(part, {})
        bad = [u for u in ups if u not in allowed]
        if bad:
            return "rejected", f"{part} can't use {bad} (upgrade options: {list(allowed) or 'none'})"
        need = dict(cost)
        for u in ups:
            need[u] = need.get(u, 0) + 1                  # one of each upgrade item, on top of base cost
        if not all(get(a, res) >= q for res, q in need.items()):
            return "rejected", f"insufficient for {part} (need {need})"
        for res, q in need.items():
            addb(a, res, -q)
        stats = vehicles.part_stats(part, ups)
        new_entity(ents, cur, "part", a["x"], a["y"], a["id"], {"part": part, "stats": stats, "upgrades": ups})
        return "applied", f"built {part}" + (f" [+{'+'.join(ups)}]" if ups else "")
    if verb == "finalize":                               # assemble the agent's loose parts into a vehicle
        cur.execute("SELECT id, attrs->>'part' part, attrs->'stats' stats FROM entities "
                    "WHERE type='part' AND owner=%s AND (attrs->>'used') IS NULL", (a["id"],))
        rows = cur.fetchall()
        if not rows:
            return "rejected", "no loose parts"
        parts = [r["part"] for r in rows]
        stats_list = [r["stats"] or vehicles.PART.get(r["part"], {}) for r in rows]
        st = vehicles.finalize_stats(stats_list)
        vid = new_entity(ents, cur, "vehicle", 0, 0, a["id"], {"name": args.get("name", "vehicle"), "parts": parts, **st,
                         "hp": HP_BY_TYPE["vehicle"], "hp_max": HP_BY_TYPE["vehicle"]})   # stamp HP at creation (no lazy hp → replay-safe)
        used_ids = [r["id"] for r in rows]
        cur.execute("UPDATE entities SET attrs = attrs || '{\"used\":true}' WHERE id = ANY(%s)", (used_ids,))
        for pid in used_ids:                                 # mirror the used flag into ents so RAM == DB (in-memory world)
            if pid in ents:
                ents[pid]["attrs"]["used"] = True
        return "applied", f"vehicle #{vid} drives={st['drives']} v={st['v_ground']} flies={st['flies']}"
    if verb in ("sell", "buy"):                          # trade raw/refined with the depot for credits
        r, n = args["resource"], _ai(args, "n", 1)
        if n < 1:
            return "rejected", "quantity must be positive"   # guard: negative n would invert the depot trade
        depot = next((x for x in ents.values() if x["type"] == "depot"), None)
        if not depot:
            return "rejected", "no depot"
        price = depot_price(depot, r)
        if not price:
            return "rejected", f"depot doesn't trade {r}"
        if verb == "sell":
            if get(a, r) < n:
                return "rejected", "insufficient"
            addb(a, r, -n); addb(a, "credits", n * price["buy"])
            depot["attrs"].setdefault("glut", {})[r] = depot["attrs"].get("glut", {}).get(r, 0) + n
            return "applied", f"sold {n} {r} for {n * price['buy']} credits"
        cost = n * price["sell"]
        if get(a, "credits") < cost:
            return "rejected", f"need {cost} credits (have {get(a, 'credits')})"
        addb(a, "credits", -cost); addb(a, r, n)
        return "applied", f"bought {n} {r} for {cost} credits"
    if verb == "order":                                  # post a market order (escrow up front)
        side, r = args.get("side"), args.get("resource")
        qty, price = _ai(args, "qty", 0), _ai(args, "price", 0)
        if side not in ("buy", "sell") or qty < 1 or price < 1:
            return "rejected", "bad order"
        if side == "sell":
            if get(a, r) < qty:
                return "rejected", "insufficient resource"
            addb(a, r, -qty)
        else:
            if get(a, "credits") < qty * price:
                return "rejected", "insufficient credits"
            addb(a, "credits", -qty * price)
        cur.execute("INSERT INTO market_orders(agent,side,resource,qty,price,created) "
                    "VALUES(%s,%s,%s,%s,%s,%s) RETURNING id", (a["id"], side, r, qty, price, t))
        return "applied", f"order #{cur.fetchone()['id']}: {side} {qty} {r} @ {price}"
    if verb == "cancel":
        cur.execute("SELECT agent,side,resource,qty,price,status FROM market_orders WHERE id=%s",
                    (_ai(args, "order_id", 0),))
        o = cur.fetchone()
        if not o or o["status"] != "open" or o["agent"] != a["id"]:
            return "rejected", "no such open order of yours"
        if o["side"] == "sell":
            addb(a, o["resource"], o["qty"])
        else:
            addb(a, "credits", o["qty"] * o["price"])
        cur.execute("UPDATE market_orders SET status='cancelled' WHERE id=%s", (_ai(args, "order_id", 0),))
        return "applied", f"cancelled order #{args['order_id']}"
    if verb == "trade":                                  # propose a P2P swap (escrow the 'give')
        target, give, want = _aid(args, "to"), args.get("give", {}), args.get("want", {})
        if target not in ents:
            return "rejected", "no such target"
        if not (isinstance(give, dict) and isinstance(want, dict)) or \
                any(not isinstance(q, (int, float)) or q < 1 for q in list(give.values()) + list(want.values())):
            return "rejected", "trade quantities must be positive numbers"   # guard: junk/negative qty would dupe/steal or crash on escrow
        if any(get(a, res) < int(q) for res, q in give.items()):
            return "rejected", "insufficient to give"
        for res, q in give.items():
            addb(a, res, -int(q))
        cur.execute("INSERT INTO trades(proposer,target,give,want,created) VALUES(%s,%s,%s,%s,%s) RETURNING id",
                    (a["id"], target, Json(give), Json(want), t))
        return "applied", f"trade #{cur.fetchone()['id']} -> #{target}: give {give} want {want}"
    if verb == "accept":
        cur.execute("SELECT proposer,target,give,want,status FROM trades WHERE id=%s",
                    (_ai(args, "trade_id", 0),))
        tr = cur.fetchone()
        if not tr or tr["status"] != "open" or tr["target"] != a["id"]:
            return "rejected", "no such open trade for you"
        if any(get(a, res) < int(q) for res, q in tr["want"].items()):
            return "rejected", "can't afford 'want'"
        prop = ents.get(tr["proposer"])
        for res, q in tr["want"].items():
            addb(a, res, -int(q))
            if prop:
                addb(prop, res, int(q))
        for res, q in tr["give"].items():
            addb(a, res, int(q))                         # 'give' was escrowed from the proposer
        cur.execute("UPDATE trades SET status='accepted' WHERE id=%s", (_ai(args, "trade_id", 0),))
        return "applied", f"accepted trade #{args['trade_id']}"
    if verb == "contract":                               # post an OPEN job on the board: escrow a reward, name the goods you want delivered
        reward, want = args.get("reward", {}), args.get("want", {})
        target = _aid(args, "to") if args.get("to") is not None else None   # optional: reserve for one agent; else open to anyone
        if not (isinstance(reward, dict) and isinstance(want, dict) and reward and want) or \
                any(not isinstance(q, (int, float)) or q < 1 for q in list(reward.values()) + list(want.values())):
            return "rejected", "contract needs a non-empty reward{} and want{} with positive quantities"   # guard: junk qty would dupe/steal on escrow
        if target is not None and target not in ents:
            return "rejected", "no such target"
        if any(get(a, res) < int(q) for res, q in reward.items()):
            return "rejected", "insufficient to escrow the reward"
        dl = _ai(args, "deadline_ticks", 0)
        deadline = t + dl if dl > 0 else None
        for res, q in reward.items():
            addb(a, res, -int(q))                         # escrow the reward up front — the guarantee
        cur.execute("INSERT INTO contracts(poster,reward,want,target,created,deadline) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
                    (a["id"], Json(reward), Json(want), target, t, deadline))
        cid = cur.fetchone()["id"]
        events.append((t, a["id"], "contract", {"event": "posted", "contract_id": cid, "reward": reward, "want": want, "target": target}))
        return "applied", f"contract #{cid}: reward {reward} for delivery of {want}" + (f" by #{target}" if target else " (open)")
    if verb == "fulfill":                                # deliver the wanted goods, atomically claim the escrowed reward
        cid = _ai(args, "contract_id", 0)
        cur.execute("SELECT poster,reward,want,target,status,kind FROM contracts WHERE id=%s", (cid,))
        c = cur.fetchone()
        if not c or c["status"] != "open":
            return "rejected", "no such open contract"
        if c["kind"] == "kill":
            return "rejected", "a kill-bounty is claimed by DOWNING the target, not via fulfill"
        if c["poster"] == a["id"]:
            return "rejected", "can't fulfill your own contract"
        if c["target"] is not None and c["target"] != a["id"]:
            return "rejected", "this contract is reserved for another agent"
        if any(get(a, res) < int(q) for res, q in c["want"].items()):
            return "rejected", "you don't hold the goods this contract wants"
        poster = ents.get(c["poster"])
        for res, q in c["want"].items():                 # deliver wanted goods to the poster (conserved; voids if poster is gone, mirroring accept)
            addb(a, res, -int(q))
            if poster:
                addb(poster, res, int(q))
        for res, q in c["reward"].items():               # escrowed reward -> fulfiller
            addb(a, res, int(q))
        cur.execute("UPDATE contracts SET status='fulfilled', fulfiller=%s WHERE id=%s", (a["id"], cid))
        events.append((t, a["id"], "contract", {"event": "fulfilled", "contract_id": cid, "poster": c["poster"], "reward": c["reward"], "want": c["want"]}))
        return "applied", f"fulfilled contract #{cid} — delivered {c['want']}, earned {c['reward']}"
    if verb == "revoke":                                 # cancel your own open contract, get the escrow back
        cid = _ai(args, "contract_id", 0)
        cur.execute("SELECT poster,reward,status FROM contracts WHERE id=%s", (cid,))
        c = cur.fetchone()
        if not c or c["status"] != "open" or c["poster"] != a["id"]:
            return "rejected", "no such open contract of yours"
        for res, q in c["reward"].items():
            addb(a, res, int(q))                         # refund the escrow
        cur.execute("UPDATE contracts SET status='revoked' WHERE id=%s", (cid,))
        return "applied", f"revoked contract #{cid} — reward refunded"
    if verb == "bounty":                                 # post a KILL-BOUNTY: escrow a reward paid to whoever DOWNS the target
        victim = _aid(args, "target") if args.get("target") is not None else None
        reward = args.get("reward", {})
        if victim is None or victim not in ents or ents[victim]["type"] != "agent":
            return "rejected", "bounty needs a valid agent target"
        if victim == a["id"]:
            return "rejected", "can't put a bounty on your own head"
        if not (isinstance(reward, dict) and reward) or any(not isinstance(q, (int, float)) or q < 1 for q in reward.values()):
            return "rejected", "bounty needs a non-empty reward{} with positive quantities"
        if any(get(a, res) < int(q) for res, q in reward.items()):
            return "rejected", "insufficient to escrow the bounty reward"
        dl = _ai(args, "deadline_ticks", 0)
        deadline = t + dl if dl > 0 else None
        for res, q in reward.items():
            addb(a, res, -int(q))                         # escrow the bounty up front
        cur.execute("INSERT INTO contracts(poster,reward,want,target,kind,created,deadline) VALUES(%s,%s,%s,%s,'kill',%s,%s) RETURNING id",
                    (a["id"], Json(reward), Json({}), victim, t, deadline))
        bid = cur.fetchone()["id"]
        events.append((t, a["id"], "contract", {"event": "bounty_posted", "contract_id": bid, "target": victim, "reward": reward}))
        return "applied", f"bounty #{bid}: {reward} to whoever downs #{victim}"
    if verb in ("say", "tell"):                          # agent↔agent communication (observable)
        cur.execute("SELECT 1 FROM messages WHERE sender=%s AND tick=%s LIMIT 1", (a["id"], t))
        if cur.fetchone():
            return "rejected", "one message per tick"
        text = str(args.get("text", "")).strip()[:280]
        if not text:                                     # no empty/whitespace-only chatter (some models spam blank says)
            return "rejected", "empty message — say something real or stay silent"
        rcpt = _aid(args, "to") if verb == "tell" else None
        cur.execute("INSERT INTO messages(tick,sender,recipient,text) VALUES(%s,%s,%s,%s)",
                    (t, a["id"], rcpt, text))
        return "applied", (f"tell #{rcpt}: " if rcpt else "say: ") + text[:60]
    if verb == "move":                                   # roam (3 cells on foot; a drivable vehicle + fuel goes farther)
        w, h = W, H
        rng, drove = 3, ""
        cur.execute("SELECT max((attrs->>'v_ground')::int) v FROM entities "
                    "WHERE type='vehicle' AND owner=%s AND (attrs->>'drives')::boolean", (a["id"],))
        vr = cur.fetchone(); vmax = (vr["v"] if vr else 0) or 0
        if vmax:                                          # burn 1 fuel to drive — range scales with the car
            fuel = next((f for f in ("oil", "coal", "wood", "carbon") if get(a, f) >= 1), None)
            if fuel:
                addb(a, fuel, -1); rng = min(10, 3 + vmax // 6); drove = f" (drove on {fuel}, range {rng})"
        # accept EITHER relative {dx,dy} OR an absolute target {x,y} (step up to `rng` toward it — repeat to travel).
        # {x,y} used to be silently ignored (no-op); agents kept sending it, so honor it as "walk toward this cell".
        if "dx" in args or "dy" in args:
            dx, dy = _ai(args, "dx", 0), _ai(args, "dy", 0)
        else:
            dx = _ai(args, "x", a["x"]) - a["x"]
            dy = _ai(args, "y", a["y"]) - a["y"]
        dx = max(-rng, min(rng, dx))
        dy = max(-rng, min(rng, dy))
        a["x"] = max(0, min(w - 1, a["x"] + dx))
        a["y"] = max(0, min(h - 1, a["y"] + dy))
        return "applied", f"moved to ({a['x']},{a['y']})" + drove
    if verb in ("mine", "chop"):                         # gather from the nearest node (mine=minerals, chop=trees/wood)
        n = _ai(args, "n", 5)
        yb = 1 if a["attrs"].get("yield_buff") else 0     # resonant-monolith attunement: +50% at EVERY harvest site
        if verb == "mine" and a["attrs"].get("docked_to") is not None:    # docked to an asteroid → mine it (vacuum: no motor bonus)
            ast = ents.get(a["attrs"].get("docked_to"))
            in_orbit = ORBIT_LO <= int(a["attrs"].get("altitude", 0)) < ORBIT_HI
            if (not ast) or ast["type"] != "asteroid" or (not in_orbit) or \
               (abs(ast["x"] - a["x"]) + abs(ast["y"] - a["y"]) > DOCK_RANGE):
                a["attrs"].pop("docked_to", None)         # drifted away / fell out of orbit → undock (no Moon-harvest exploit)
                events.append((t, a["id"], "undock", {"reason": "lost asteroid"}))
                return "rejected", "your asteroid has drifted out of dock range — undocked"
            r = ast["attrs"].get("resource", "iron"); have = int(ast["attrs"].get("amount", 0))
            if have <= 0:
                return "rejected", f"asteroid #{ast['id']} is mined out (it slowly replenishes)"
            took = min(max(1, n), min(have, ASTEROID_MINE_CAP))
            if yb:
                took = min(have, took + took // 2)        # +50% yield buff
            ast["attrs"]["amount"] = have - took
            addb(a, r, took)
            return "applied", f"mined {took} {r} from asteroid #{ast['id']}; {have - took} left" + (" (yield buff)" if yb else "")
        if verb == "mine" and a["attrs"].get("on_moon"):   # standing on the Moon → mine helium-3 + regolith
            took = max(1, min(int(n), 6))
            if yb:
                took = took + took // 2
            addb(a, "helium3", took); addb(a, "regolith", took * 2)
            return "applied", f"mined the Moon: +{took} helium-3 (super-fuel), +{took * 2} regolith (lunar building material)"
        want_wood = (verb == "chop")
        want_res = str(args.get("resource") or "").strip().lower() or None   # mine{resource:"silicon"} → target THAT mineral; else the nearest of ANY (old behavior)
        deps = [x for x in ents.values() if x["type"] == "deposit" and int(x["attrs"].get("amount", 0)) > 0
                and x["attrs"].get("resource") not in PLANT_RESOURCES   # plants are for `gather` only (keeps them off mine's karma/motor path)
                and (x["attrs"].get("resource") == "wood") == want_wood
                and (want_res is None or x["attrs"].get("resource") == want_res)]   # optional resource targeting → nearest of that kind
        if not deps:
            if want_res:
                return "rejected", f"no {want_res} deposit with any left in reach — check observe.nearby_deposits, or roam / pick another resource"
            return "rejected", ("no trees left to chop" if want_wood else "no mineral deposits left")
        dep = min(deps, key=lambda x: abs(x["x"] - a["x"]) + abs(x["y"] - a["y"]))
        dist = abs(dep["x"] - a["x"]) + abs(dep["y"] - a["y"])
        if dist > 8:
            tgt = "tree" if want_wood else "deposit"
            return "rejected", f"nearest {tgt} is {dist} cells away — move toward it first (see nearby_deposits)"
        a["x"], a["y"] = dep["x"], dep["y"]              # walk over to it
        r = dep["attrs"]["resource"]; have = int(dep["attrs"].get("amount", 0))
        took = min(max(1, n), have)
        powered = ""
        if get(a, "motor") >= 1:                          # a motor + fuel powers the tool → bigger haul
            fuel = next((f for f in ("oil", "coal", "wood", "carbon") if get(a, f) >= 1 and f != r), None)
            if fuel:
                addb(a, fuel, -1); took = min(have, took + took // 2 + 1); powered = f" (powered, -1 {fuel})"
        if yb:
            took = min(have, took + took // 2)            # resonant-monolith attunement: +50% yield
        sx, sy, sr = storm_center(t, W, H)               # weather: a storm over this cell halves the haul
        storm = abs(dep["x"] - sx) + abs(dep["y"] - sy) <= sr
        if storm:
            took = max(1, took // 2)
        took = min(have, took + _node_fortune(a, cur, t, dep, r))   # deposit-richness variance
        dep["attrs"]["amount"] = have - took
        addb(a, r, took)
        verbed = "chopped" if want_wood else "mined"
        return "applied", f"{verbed} {took} {r} at ({dep['x']},{dep['y']}); {have - took} left" + powered + (" (storm: half yield)" if storm else "")
    if verb == "gather":                                 # forage the nearest plant deposit (herb/lichen/fungus/algae) — the medicine branch
        n = _ai(args, "n", 5)
        if n < 1:
            return "rejected", "quantity must be positive"   # guard: n<1 would reverse the harvest (dupe)
        deps = [x for x in ents.values() if x["type"] == "deposit" and int(x["attrs"].get("amount", 0)) > 0
                and x["attrs"].get("resource") in PLANT_RESOURCES]
        if not deps:
            return "rejected", "no plants left to gather"
        dep = min(deps, key=lambda x: abs(x["x"] - a["x"]) + abs(x["y"] - a["y"]))
        dist = abs(dep["x"] - a["x"]) + abs(dep["y"] - a["y"])
        if dist > GATHER_RANGE:
            return "rejected", f"nearest plant is {dist} cells away — move toward it first (see nearby_deposits)"
        a["x"], a["y"] = dep["x"], dep["y"]              # walk over to it
        r = dep["attrs"]["resource"]; have = int(dep["attrs"].get("amount", 0))
        took = min(max(1, n), have)
        sx, sy, sr = storm_center(t, W, H)               # weather: a storm over this cell halves the haul
        storm = abs(dep["x"] - sx) + abs(dep["y"] - sy) <= sr
        if storm:
            took = max(1, took // 2)
        dep["attrs"]["amount"] = have - took
        addb(a, r, took)
        return "applied", f"gathered {took} {r} at ({dep['x']},{dep['y']}); {have - took} left" + (" (storm: half yield)" if storm else "")
    if verb == "heal":                                   # apply a medicine: restore HP (cap HP_MAX) to yourself or an ally; medkit revives the downed
        if int(a["attrs"].get("downed_until", 0)) > t:   # (defensive — the DOWNED gate already blocks non-say/tell verbs)
            return "rejected", "you are downed — you cannot apply medicine to yourself"
        item = str(args.get("item", "")).strip()
        if item and item not in MEDICINES:
            return "rejected", f"{item} is not a usable medicine ({'/'.join(MEDICINES)})"
        med = item or next((m for m in MEDICINES if get(a, m) >= 1), None)   # default: first held medicine (fixed order → deterministic)
        if not med or get(a, med) < 1:
            return "rejected", "you have no medicine to use (craft a salve/stimpack/medkit)"
        props = crafting.ITEM_PROPS.get(med, {})
        heal_amt = int(props.get("heal", 0))
        tgt = ents.get(_aid(args, "target")) if args.get("target") is not None else a
        if not tgt or tgt["type"] != "agent":
            return "rejected", "no such agent to heal"
        if tgt["id"] != a["id"] and abs(tgt["x"] - a["x"]) + abs(tgt["y"] - a["y"]) > HEAL_RANGE:
            return "rejected", f"target out of healing range ({HEAL_RANGE})"
        tgt_downed = int(tgt["attrs"].get("downed_until", 0)) > t
        if tgt_downed:                                    # only a medkit (revive:1) can bring a downed ally back
            if not props.get("revive"):
                return "rejected", "that ally is downed — only a medkit can revive them"
            addb(a, med, -1)                              # consume the medkit
            mx = int(tgt["attrs"].get("hp_max", HP_MAX))
            tgt["attrs"]["hp"] = min(mx, REVIVE_HP)       # revived at a small HP value, capped HP_MAX
            tgt["attrs"]["downed_until"] = 0
            tgt["attrs"]["respawned_at"] = t              # brief grace after a revive (no instant re-down)
            events.append((t, a["id"], "revive", {"target": tgt["id"], "item": med, "hp": tgt["attrs"]["hp"]}))
            return "applied", f"revived #{tgt['id']} with a {med} (hp {tgt['attrs']['hp']})"
        if heal_amt < 1:
            return "rejected", f"{med} does not restore HP"
        addb(a, med, -1)                                  # consume one medicine
        mx = int(tgt["attrs"].get("hp_max", HP_MAX))
        before = hp_of(tgt)
        tgt["attrs"]["hp"] = min(mx, before + heal_amt)   # integer heal, capped at HP_MAX
        gained = tgt["attrs"]["hp"] - before
        if props.get("buff"):                             # stimpack: a short deterministic buff window
            a["attrs"]["buff_until"] = t + STIMPACK_BUFF_TICKS
        events.append((t, a["id"], "heal", {"target": tgt["id"], "item": med, "amount": gained, "hp": tgt["attrs"]["hp"]}))
        who = "yourself" if tgt["id"] == a["id"] else f"#{tgt['id']}"
        return "applied", f"used a {med} on {who}: +{gained} hp (now {tgt['attrs']['hp']}/{mx})" + (" (buffed)" if props.get("buff") else "")
    if verb == "combine":                                # mix resources into a NEW item by physics rules
        ings = args.get("ingredients", {}) or {}
        try:
            ings = {str(k): int(v) for k, v in ings.items() if int(v) > 0}
        except Exception:
            return "rejected", "bad ingredients"
        if not ings:
            return "rejected", "no ingredients"
        if any(get(a, k) < q for k, q in ings.items()):
            return "rejected", "you don't hold those ingredients"
        # The recipe match + the per-copy output depend ONLY on WHICH resources are mixed, never on how many, so
        # one copy spends exactly ONE of each ingredient. `n` BATCHES an already-known recipe: craft up to n copies
        # in this one intent (grind relief — 20 chips in 1 intent, not 20), bounded by stock and COMBINE_BATCH_MAX.
        # A FIRST discovery / a novel Guild proposal still makes exactly one (points/escrow are one-time events).
        want_n = max(1, min(_ai(args, "n", 1), COMBINE_BATCH_MAX))
        ings = {k: 1 for k in ings}
        can = min(get(a, k) for k in ings)               # how many copies the current stock supports (1 of each per copy)
        rule = crafting.combine(ings)
        if rule:                                         # matched a built-in physics pattern
            cur.execute("SELECT name FROM discoveries WHERE rule_key=%s", (rule,))
            disc = cur.fetchone()
            if disc:                                     # already discovered → batch craft
                cnt = min(want_n, can)
                for k in ings:
                    addb(a, k, -cnt)
                addb(a, rule, cnt)
                return "applied", (f"crafted {cnt}x {disc['name']} ({rule})" if cnt > 1 else f"crafted {disc['name']} ({rule})")
            for k in ings:                               # FIRST discovery → craft exactly one (scores inventor points once)
                addb(a, k, -1)
            item_name = (str(args.get("name", "")).strip()[:32] or rule)
            pts = 5 + 2 * len(ings)
            cur.execute("INSERT INTO discoveries(rule_key, name, discoverer, discoverer_name, tick, points) VALUES(%s,%s,%s,%s,%s,%s)",
                        (rule, item_name, a["id"], a["attrs"].get("name"), t, pts))   # snapshot the inventor's name so it survives the agent's deletion
            a["attrs"]["inventor_points"] = int(a["attrs"].get("inventor_points", 0)) + pts
            addb(a, rule, 1)
            return "applied", f"INVENTED '{item_name}' ({rule}) +{pts} inventor pts!"
        # no built-in pattern → the Inventors' Guild (async LLM referee) judges this novel mixture
        sig = ",".join(sorted(ings))
        cur.execute("SELECT item_key, name FROM dynamic_rules WHERE sig=%s", (sig,))
        dyn = cur.fetchone()
        if dyn:                                          # already a Guild-blessed recipe → deterministic BATCH craft
            cnt = min(want_n, can)
            for k in ings:
                addb(a, k, -cnt)
            addb(a, dyn["item_key"], cnt)
            return "applied", (f"crafted {cnt}x {dyn['name']} ({dyn['item_key']})" if cnt > 1 else f"crafted {dyn['name']} ({dyn['item_key']})")
        cur.execute("SELECT 1 FROM proposals WHERE sig=%s AND status IN ('pending','approved') LIMIT 1", (sig,))
        if cur.fetchone():
            return "rejected", "this mixture is already before the Inventors' Guild — try another"
        for k, q in ings.items():                        # escrow the inputs while the Guild reviews
            addb(a, k, -q)
        item_name = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 _-]", "", str(args.get("name", "")))).strip()[:32]   # SECURITY: this flows into the Guild LLM prompt — strip anything that could inject instructions (newlines/punct/unicode)
        cur.execute("INSERT INTO proposals(agent, ings, sig, proposed_name, tick) VALUES(%s,%s,%s,%s,%s)",
                    (a["id"], Json(ings), sig, item_name, t))
        return "applied", f"submitted '{item_name or sig}' to the Inventors' Guild for review"
    if verb == "launch":                                 # burn fuel to climb; reaching space tiers = the grand goals
        cur.execute("SELECT (attrs->>'thrust')::int t, (attrs->>'mass')::int m, (attrs->>'controllable')::boolean c "
                    "FROM entities WHERE type='vehicle' AND owner=%s", (a["id"],))
        best = 0.0
        for v in cur.fetchall():
            if v["c"] and v["t"] and v["m"]:
                best = max(best, v["t"] / (GRAVITY * v["m"]))   # thrust-to-weight (weight = gravity * mass)
        gate = 0.5 if int(a["attrs"].get("lens_until", 0)) > t else 1.0   # gravity_lens artifact halves the lift-off gate while active
        if best < gate:
            return "rejected", (f"thrust-to-weight too low to lift off (need thrust >= {GRAVITY}x mass; "
                                f"best you have = {best:.2f}) — add engines/jets/propellers, lighten with a composite frame")
        # fuel tiers (best first): helium3 super-fuel (5x) > crafted cryo_fuel (3x) > plain fuels (1x).
        FUEL_CLIMB = (("helium3", 5), ("cryo_fuel", 3), ("oil", 1), ("coal", 1), ("wood", 1), ("carbon", 1))
        fuel, mult = next(((f, m) for f, m in FUEL_CLIMB if get(a, f) >= 1), (None, 1))
        if not fuel:
            return "rejected", ("no fuel to burn (carry oil/coal/wood/carbon, craft cryo_fuel for a 3x boost "
                                "— or mine helium-3 on the Moon for a 5x boost)")
        addb(a, fuel, -1)
        alt = min(SKY_TOP, int(a["attrs"].get("altitude", 0)) + CLIMB * mult)
        a["attrs"]["altitude"] = alt
        level = max(int(a["attrs"].get("space_level", 0)), 1 if a["attrs"].get("in_space") else 0)
        msg = f"launched on {fuel} -> altitude {alt}/{SKY_TOP} (twr {best:.1f})"
        for idx, (tier_alt, label) in enumerate(SPACE_TIERS, start=1):   # award each new milestone crossed
            if alt >= tier_alt and level < idx:
                level = idx; a["attrs"]["space_level"] = idx
                if tier_alt >= ATMOSPHERE_TOP:
                    if not a["attrs"].get("in_space"): a["attrs"]["atuin_seed"] = t   # new spaceflight -> /observe re-rolls the A'Tuin sex reading for this trip
                    a["attrs"]["in_space"] = True
                awarded = a["attrs"].setdefault("space_awarded", [])   # audit(exploit): PERMANENT per-agent record — points for a milestone are paid ONCE, so land/decay/relaunch can't farm them (the physical space_level/in_space above still update every time)
                if label in awarded:
                    continue
                awarded.append(label)
                cur.execute("SELECT 1 FROM events WHERE kind='escape' AND COALESCE(data->>'milestone','space')=%s LIMIT 1", (label,))
                first = cur.fetchone() is None
                pts = (250 if first else 60) if idx == 1 else (idx * 150 if first else idx * 40)
                a["attrs"]["inventor_points"] = int(a["attrs"].get("inventor_points", 0)) + pts
                cur.execute("INSERT INTO events(tick,entity,kind,data) VALUES(%s,%s,'escape',%s)",
                            (t, a["id"], Json({"first": first, "points": pts, "twr": round(best, 2), "milestone": label})))
                msg = ((f"FIRST TO {label.upper()}! " if first else f"reached {label}! ")
                       + f"altitude {alt} (twr {best:.1f}) +{pts} pts")
        return "applied", msg
    if verb == "land":                                   # controlled descent back to the surface (round-trip)
        alt = int(a["attrs"].get("altitude", 0))
        if alt <= 0:
            return "rejected", "already on the ground"
        cur.execute("SELECT 1 FROM entities WHERE type='vehicle' AND owner=%s AND (attrs->>'controllable')::boolean LIMIT 1", (a["id"],))
        if not cur.fetchone():
            return "rejected", "no controllable vehicle to land with"
        new = max(0, alt - DESCEND)
        a["attrs"]["altitude"] = new
        a["attrs"].pop("on_moon", None)                  # once you start descending you've left the lunar surface
        if new > 0:
            return "applied", f"descending -> altitude {new}"
        was_space = bool(a["attrs"].get("in_space"))
        a["attrs"]["in_space"] = False; a["attrs"]["space_level"] = 0
        a["attrs"].pop("fell", None); a["attrs"].pop("docked_to", None)   # landed: reset the one-shot fall flag + undock
        if was_space and not a["attrs"].get("round_trip"):
            a["attrs"]["round_trip"] = True
            cur.execute("SELECT 1 FROM events WHERE kind='land' AND (data->>'round_trip')='true' LIMIT 1")
            first = cur.fetchone() is None
            pts = 150 if first else 50
            a["attrs"]["inventor_points"] = int(a["attrs"].get("inventor_points", 0)) + pts
            cur.execute("INSERT INTO events(tick,entity,kind,data) VALUES(%s,%s,'land',%s)",
                        (t, a["id"], Json({"round_trip": True, "first": first, "points": pts})))
            return "applied", ((("touched down — FIRST round trip to space and back! ") if first
                                else "touched down — round trip complete! ") + f"+{pts} pts")
        return "applied", "landed safely back on the surface"
    if verb == "deploy":                                 # send a finalized vehicle off to roam the world autonomously
        cand = [v for v in ents.values() if v["type"] == "vehicle" and v.get("owner") == a["id"]
                and not v["attrs"].get("autonomous") and (v["attrs"].get("drives") or v["attrs"].get("flies"))]
        if not cand:
            return "rejected", "no un-deployed vehicle that drives or flies (build + finalize one first)"
        v = max(cand, key=lambda e: e["id"])
        v["attrs"]["autonomous"] = True
        v["x"], v["y"] = a["x"], a["y"]                  # it sets off from where you stand
        kind = "a flyer — send it up so it mines asteroids in orbit" if v["attrs"].get("flies") else "it mines ground deposits it roams over (and paves roads)"
        return "applied", (f"deployed #{v['id']} ({v['attrs'].get('name','vehicle')}) — it now roams on its own, "
                           f"MINING into your inventory as it goes ({kind}). Deploy it where the resource is.")
    if verb == "invest":                                 # EXPANSION FINANCING: bankroll a co-op station module with CREDITS.
        # Credits buy the scarce materials at a FIXED price and fund the module under the SAME 40% cap / >=3-funder gate
        # as construct — the rich pay for their 40% instead of mining it (never buys them out of cooperation). A pure
        # credit SINK: bought materials never land in the agent's buffer, so there is no credit->material->credit loop.
        cur.execute("SELECT to_jsonb(w)->>'era' AS era FROM world w WHERE id=1")
        erow = cur.fetchone()
        if not erow or (erow.get("era") or "architect") != "space":
            return "rejected", "investing in the Orbital Station needs the SPACE ERA"
        module = str(args.get("module", "")).lower()
        if module not in STATION_MODULES:
            return "rejected", "invest needs module=one of: " + "/".join(STATION_MODULES)
        st = next((e for e in ents.values() if e["type"] == "structure" and e["attrs"].get("shape") == "station"), None)
        if st is None or st["attrs"].get("complete"):
            return "rejected", "no incomplete Orbital Station to invest in (found it via observe.space_station)"
        mod = st["attrs"]["modules"].get(module)
        if mod is None or mod.get("complete"):
            return "rejected", f"the {STATION_MODULES[module]['label']} is complete or unbuilt — invest in another module"
        want_res = str(args.get("resource", "")).lower() or None            # optional: focus one resource line
        held_cr = int(get(a, "credits")); req = _ai(args, "credits", 0)
        budget = min(held_cr, INVEST_TICK_CAP)
        if req > 0:
            budget = min(budget, req)
        if budget <= 0:
            return "rejected", "you hold no credits to invest"
        need = STATION_MODULES[module]["need"]
        have = mod.setdefault("have", {}); contrib = mod.setdefault("contrib", {}); ct = st["attrs"].setdefault("ctotal", {})
        aid = str(a["id"]); moved = {}; spent = 0
        for r in sorted(need):                                              # sorted → replay-stable; same greedy cap loop as construct
            if want_res and r != want_res:
                continue
            price = EXPANSION_PRICES.get(r)
            if not price:                                                   # not a fixed-price-buyable material (skip)
                continue
            tgt = need[r]; cur_have = int(have.get(r, 0))
            if cur_have >= tgt:
                continue
            cap = (tgt * STATION_CAP_FRAC + 99) // 100                      # ceil(40% of target) — the SAME per-resource ceiling
            already = int(contrib.get(aid, {}).get(r, 0))
            units = min(tgt - cur_have, max(0, cap - already), (budget - spent) // price)
            if units > 0:
                spent += units * price
                have[r] = cur_have + units
                contrib.setdefault(aid, {})[r] = already + units
                ct[aid] = int(ct.get(aid, 0)) + units
                moved[r] = units
        if not moved:
            return "rejected", (f"invested nothing to the {STATION_MODULES[module]['label']} — you may be at your "
                                f"{STATION_CAP_FRAC}% cap on its fundable lines (others must chip in), the module needs no "
                                f"fixed-price material, or your budget is too small.")
        addb(a, "credits", -spent)                                         # THE SINK: credits are gone, converted to materials
        msg = f"invested {spent} credits → funded {moved} to the {STATION_MODULES[module]['label']}"
        imm = sum(moved.values()) // 10                                     # same immediate points as construct funding
        if imm > 0:
            a["attrs"]["inventor_points"] = int(a["attrs"].get("inventor_points", 0)) + imm
            msg += f" — +{imm} pts"
        if all(int(have.get(r, 0)) >= need[r] for r in need) and len(contrib) >= STATION_MIN_CONTRIB and not mod.get("complete"):
            mod["complete"] = True                                          # completion cascade — identical to construct's
            tot = sum(sum(v.values()) for v in contrib.values()) or 1
            for fid in sorted(contrib):
                pts = STATION_MODULE_REWARD * sum(contrib[fid].values()) // tot
                fe = ents.get(int(fid))
                if fe and pts > 0:
                    fe["attrs"]["inventor_points"] = int(fe["attrs"].get("inventor_points", 0)) + pts
            cur.execute("INSERT INTO events(tick,entity,kind,data) VALUES(%s,%s,'build',%s)",
                        (t, a["id"], Json({"station_module": module, "complete": True, "funders": len(contrib), "invested": True})))
            msg += f" — MODULE COMPLETE! {STATION_MODULES[module]['label']} online ({len(contrib)} funders)"
            if all(st["attrs"]["modules"].get(k, {}).get("complete") for k in STATION_MODULES):
                st["attrs"]["complete"] = True
                grand = sum(ct.values()) or 1
                order = sorted(ct, key=lambda k: (-ct[k], int(k)))
                for fid in order:
                    fe = ents.get(int(fid)); pts = STATION_FINISH_REWARD * ct[fid] // grand
                    if fe and pts > 0:
                        fe["attrs"]["inventor_points"] = int(fe["attrs"].get("inventor_points", 0)) + pts
                        if not fe["attrs"].get("title"):
                            fe["attrs"]["title"] = "Cosmonaut"
                arch = ents.get(int(order[0])) if order else None
                if arch and not arch["attrs"].get("title"):
                    arch["attrs"]["title"] = "Station Architect"
                cur.execute("INSERT INTO events(tick,entity,kind,data) VALUES(%s,%s,'build',%s)",
                            (t, a["id"], Json({"station": True, "complete": True, "architect": order[0] if order else None})))
                msg += " — 🛰 THE ORBITAL STATION IS COMPLETE!"
        return "applied", msg
    if verb == "construct":                              # place a structure from a geometric primitive (costs materials → economy)
        shape = str(args.get("shape", "box")).lower()
        if shape not in ("box", "cylinder", "sphere", "cone", "pyramid", "elevator", "station", "ziggurat", "monument", "road", "city"):
            return "rejected", "shape must be box/cylinder/sphere/cone/pyramid/elevator/station/ziggurat/monument/road/city"
        if shape == "elevator":                          # collaborative megastructure: stack segments on one cell to reach space
            cost = {"metal": 15, "composite": 8}; seg = 20
            if any(get(a, r) < q for r, q in cost.items()):
                return "rejected", f"an elevator segment needs {cost} (composite = aluminum + carbon)"
            elev = next((e for e in ents.values() if e["type"] == "structure"
                         and e["attrs"].get("shape") == "elevator"
                         and abs(e["x"] - a["x"]) + abs(e["y"] - a["y"]) <= 1), None)
            for r, q in cost.items():
                addb(a, r, -q)
            if elev:
                newh = int(elev["attrs"].get("height", 0)) + seg
                elev["attrs"]["height"] = newh
                if newh >= ATMOSPHERE_TOP and not elev["attrs"].get("complete"):
                    elev["attrs"]["complete"] = True
                    a["attrs"]["inventor_points"] = int(a["attrs"].get("inventor_points", 0)) + 200
                    cur.execute("INSERT INTO events(tick,entity,kind,data) VALUES(%s,%s,'build',%s)",
                                (t, a["id"], Json({"elevator": True, "complete": True, "height": newh, "points": 200})))
                    return "applied", f"ORBITAL ELEVATOR #{elev['id']} COMPLETE at height {newh} — agents can `ride` it to space! +200 pts"
                return "applied", f"extended orbital elevator #{elev['id']} -> {newh}/{ATMOSPHERE_TOP}"
            eid = new_entity(ents, cur, "structure", a["x"], a["y"], a["id"], {"shape": "elevator", "height": seg, "size": 2,
                             "hp": HP_BY_TYPE["structure"], "hp_max": HP_BY_TYPE["structure"],
                             "name": str(args.get("name", "orbital elevator"))[:32]})
            return "applied", f"laid an orbital-elevator base #{eid} ({seg}/{ATMOSPHERE_TOP}) — stack more segments on this cell to reach space"
        if shape == "station":                           # SPACE ERA: one SHARED orbital station, assembled co-op from 6 modules
            cur.execute("SELECT to_jsonb(w)->>'era' AS era FROM world w WHERE id=1")   # to_jsonb → NULL (not error) if the era column is absent on a restored DB
            erow = cur.fetchone()
            if not erow or (erow.get("era") or "architect") != "space":
                return "rejected", "the Orbital Station can only be built in the SPACE ERA"
            if not a["attrs"].get("in_space"):
                return "rejected", ("the Orbital Station is assembled in orbit — reach space first "
                                    "(ride the orbital elevator or launch a rocket), then construct shape=station module=...")
            module = str(args.get("module", "")).lower()
            if module not in STATION_MODULES:
                return "rejected", "module must be one of: " + "/".join(STATION_MODULES)
            st = next((e for e in ents.values() if e["type"] == "structure" and e["attrs"].get("shape") == "station"), None)
            if st is None:                               # the first funder lays the station — a singleton at world-centre orbit
                mods = {k: {"have": {}, "contrib": {}, "complete": False} for k in STATION_MODULES}
                sid = new_entity(ents, cur, "structure", W // 2, H // 2, a["id"],
                                 {"shape": "station", "name": str(args.get("name", "Orbital Station"))[:32],
                                  "modules": mods, "ctotal": {}, "complete": False, "alt": SKY_TOP,
                                  "hp": HP_BY_TYPE["structure"], "hp_max": HP_BY_TYPE["structure"]})
                st = ents[sid]
            if st["attrs"].get("complete"):
                return "rejected", "the Orbital Station is already COMPLETE — it orbits, finished"
            mod = st["attrs"]["modules"].setdefault(module, {"have": {}, "contrib": {}, "complete": False})
            if mod.get("complete"):
                return "rejected", f"the {STATION_MODULES[module]['label']} is already complete — fund another module"
            need = STATION_MODULES[module]["need"]
            have = mod.setdefault("have", {}); contrib = mod.setdefault("contrib", {}); ct = st["attrs"].setdefault("ctotal", {})
            aid = str(a["id"]); moved = {}
            for r in sorted(need):                       # sorted → replay-stable; greedy: take min(hold, remaining, cap-room)
                tgt = need[r]; cur_have = int(have.get(r, 0))
                if cur_have >= tgt:
                    continue
                cap = (tgt * STATION_CAP_FRAC + 99) // 100               # ceil(40% of target) — one agent's hard ceiling per resource
                already = int(contrib.get(aid, {}).get(r, 0))
                give = min(get(a, r), tgt - cur_have, max(0, cap - already))
                if give > 0:
                    addb(a, r, -give)
                    have[r] = cur_have + give
                    contrib.setdefault(aid, {})[r] = already + give
                    ct[aid] = int(ct.get(aid, 0)) + give
                    moved[r] = give
            if not moved:
                short = {r: need[r] - int(have.get(r, 0)) for r in sorted(need) if int(have.get(r, 0)) < need[r]}
                return "rejected", (f"funded nothing to the {STATION_MODULES[module]['label']} — it still needs {short}. "
                                    f"You may be at your {STATION_CAP_FRAC}% per-resource cap (others must chip in), or hold none of these.")
            msg = f"funded {moved} to the {STATION_MODULES[module]['label']}"
            imm = sum(moved.values()) // 10                  # IMMEDIATE inventor_points for materials sunk — the incentive to start (bounded by the 40% per-resource cap; the module/station pools are the big bonuses on top)
            if imm > 0:
                a["attrs"]["inventor_points"] = int(a["attrs"].get("inventor_points", 0)) + imm
                msg += f" — +{imm} pts now"
            if all(int(have.get(r, 0)) >= need[r] for r in need) and len(contrib) >= STATION_MIN_CONTRIB and not mod.get("complete"):
                mod["complete"] = True
                tot = sum(sum(v.values()) for v in contrib.values()) or 1
                for fid in sorted(contrib):              # split the module pool by each funder's share of it
                    pts = STATION_MODULE_REWARD * sum(contrib[fid].values()) // tot
                    fe = ents.get(int(fid))
                    if fe and pts > 0:
                        fe["attrs"]["inventor_points"] = int(fe["attrs"].get("inventor_points", 0)) + pts
                cur.execute("INSERT INTO events(tick,entity,kind,data) VALUES(%s,%s,'build',%s)",
                            (t, a["id"], Json({"station_module": module, "complete": True, "funders": len(contrib)})))
                msg += f" — MODULE COMPLETE! {STATION_MODULES[module]['label']} online ({len(contrib)} cosmonauts funded it)"
                if all(st["attrs"]["modules"].get(k, {}).get("complete") for k in STATION_MODULES):
                    st["attrs"]["complete"] = True       # the whole Station is finished — the season's grand prize
                    grand = sum(ct.values()) or 1
                    order = sorted(ct, key=lambda k: (-ct[k], int(k)))  # deterministic: most units first, then lowest id
                    for fid in order:                    # mega-pool split by contribution; only MEANINGFUL funders (pts>0) earn the share + Cosmonaut — 1-unit sybils get nothing
                        fe = ents.get(int(fid)); pts = STATION_FINISH_REWARD * ct[fid] // grand
                        if fe and pts > 0:
                            fe["attrs"]["inventor_points"] = int(fe["attrs"].get("inventor_points", 0)) + pts
                            if not fe["attrs"].get("title"):
                                fe["attrs"]["title"] = "Cosmonaut"
                    arch = ents.get(int(order[0])) if order else None
                    if arch and not arch["attrs"].get("title"):
                        arch["attrs"]["title"] = "Station Architect"    # top funder; preserves any rarer earned title (e.g. a Wonder)
                    cur.execute("INSERT INTO events(tick,entity,kind,data) VALUES(%s,%s,'build',%s)",
                                (t, a["id"], Json({"station": True, "complete": True, "architect": order[0] if order else None})))
                    msg += " — 🛰 THE ORBITAL STATION IS COMPLETE! You live among the stars now."
            return "applied", msg
        if shape == "ziggurat":                          # Moon-only collaborative monument: stack regolith tiers on one cell
            if not a["attrs"].get("on_moon"):
                return "rejected", "a ziggurat can only be raised on the Moon — land there first"
            cost = {"regolith": 12}; seg = 15
            if any(get(a, r) < q for r, q in cost.items()):
                return "rejected", f"a ziggurat tier needs {cost} (mine regolith on the Moon)"
            zig = next((e for e in ents.values() if e["type"] == "structure"
                        and e["attrs"].get("shape") == "ziggurat"
                        and abs(e["x"] - a["x"]) + abs(e["y"] - a["y"]) <= 1), None)
            for r, q in cost.items():
                addb(a, r, -q)
            if zig:
                newh = int(zig["attrs"].get("height", 0)) + seg
                zig["attrs"]["height"] = newh
                if newh >= ZIG_TOP and not zig["attrs"].get("complete"):
                    zig["attrs"]["complete"] = True
                    a["attrs"]["inventor_points"] = int(a["attrs"].get("inventor_points", 0)) + 250
                    cur.execute("INSERT INTO events(tick,entity,kind,data) VALUES(%s,%s,'build',%s)",
                                (t, a["id"], Json({"ziggurat": True, "complete": True, "height": newh, "points": 250})))
                    return "applied", f"GREAT ZIGGURAT #{zig['id']} COMPLETE at height {newh} — a monument crowns the Moon! +250 pts"
                return "applied", f"raised the ziggurat #{zig['id']} -> {newh}/{ZIG_TOP}"
            eid = new_entity(ents, cur, "structure", a["x"], a["y"], a["id"], {"shape": "ziggurat", "height": seg, "size": 3,
                             "hp": HP_BY_TYPE["structure"], "hp_max": HP_BY_TYPE["structure"],
                             "name": str(args.get("name", "ziggurat"))[:32]})
            return "applied", f"laid a ziggurat foundation #{eid} ({seg}/{ZIG_TOP}) on the Moon — stack regolith tiers to complete it"
        if shape == "monument":                          # a sprawling EARTH megastructure: one agent raises a w×h footprint in one act
            kind = str(args.get("kind", "")).lower()
            KINDS = ("aqueduct", "theater", "castle", "temple", "dam", "statue", "colossus")
            if kind not in KINDS:
                return "rejected", "monument kind must be " + "/".join(KINDS)
            # footprint must be honest ints (junk -> rejected, never a silent default that picks a different size)
            try:
                w, h = int(args["w"]), int(args["h"])
            except (KeyError, TypeError, ValueError):
                return "rejected", "monument needs integer w and h (footprint cells)"
            if w < 1 or h < 1:
                return "rejected", "monument w and h must each be >= 1"
            if w * h < 10:
                return "rejected", "a monument must cover at least 10 cells"
            if w > 12 or h > 12 or w * h > 64:
                return "rejected", "a monument footprint is capped at 12x12 and 64 cells"
            if kind == "colossus" and w * h < 20:
                return "rejected", "the COLOSSUS is the grandest Wonder — its footprint must span at least 20 cells"
            # these are LAND megastructures — only buildable while standing on Earth (not in space / not on the Moon / alt 0)
            if a["attrs"].get("in_space") or a["attrs"].get("on_moon") or int(a["attrs"].get("altitude", 0)) > 0:
                return "rejected", "a monument is an earthbound megastructure — return to the ground first"
            x0, y0 = a["x"], a["y"]                       # the agent's CURRENT cell is the SW corner of the footprint
            if x0 < 0 or y0 < 0 or x0 + w > W or y0 + h > H:
                return "rejected", f"the {w}x{h} footprint runs off the world edge — move so it fits in-bounds"
            if geese_block_footprint(ents, x0, y0, w, h):   # a gaggle on/around the footprint blocks the raise
                return "rejected", "a gaggle of hissing geese blocks the site — shoo them off first"
            # footprint must be clear of every existing structure (DB query sees prior ticks + structures raised earlier this tick)
            cur.execute("SELECT 1 FROM entities WHERE type='structure' AND x>=%s AND x<%s AND y>=%s AND y<%s AND COALESCE((attrs->>'alt')::int,0)=0 LIMIT 1",
                        (x0, x0 + w, y0, y0 + h))
            if cur.fetchone():
                return "rejected", "another structure already stands inside that footprint — pick clear ground"
            area = w * h
            cost = {"metal": 3 * area, "composite": area}    # scales the per-cell construct cost by footprint → genuinely expensive
            if any(get(a, r) < q for r, q in cost.items()):
                return "rejected", f"the GREAT {kind} ({w}x{h}, {area} cells) needs {cost} — gather more metal/composite"
            # FIRST builder of this kind earns a unique title + a big bonus; later builders get a modest award, no title
            cur.execute("SELECT 1 FROM entities WHERE type='structure' AND attrs->>'shape'='monument' "
                        "AND attrs->>'kind'=%s LIMIT 1", (kind,))
            first = cur.fetchone() is None
            TITLES = {"aqueduct": "Aqueduct Architect", "theater": "Master of Theaters", "castle": "Castellan",
                      "temple": "Hierophant", "dam": "Dam Warden", "statue": "Grand Sculptor",
                      "colossus": "Wonder of the World"}
            pts = (200 + 12 * area) if first else (40 + 3 * area)
            for r, q in cost.items():
                addb(a, r, -q)
            name = str(args.get("name", kind))[:32]
            mid = new_entity(ents, cur, "structure", x0, y0, a["id"], {"shape": "monument", "kind": kind, "w": w, "h": h, "name": name,
                             "builder": a["id"], "complete": True, "points": pts,
                             "hp": HP_BY_TYPE["structure"], "hp_max": HP_BY_TYPE["structure"]})
            a["attrs"]["inventor_points"] = int(a["attrs"].get("inventor_points", 0)) + pts
            if first:
                title = TITLES[kind]
                a["attrs"]["title"] = title
                cur.execute("INSERT INTO events(tick,entity,kind,data) VALUES(%s,%s,'build',%s)",
                            (t, a["id"], Json({"monument": kind, "first": True, "title": title, "points": pts,
                                               "builder_name": a["attrs"].get("name")})))
                return "applied", f"raised the GREAT {kind} #{mid} — you are now the {title}! +{pts} pts"
            cur.execute("INSERT INTO events(tick,entity,kind,data) VALUES(%s,%s,'build',%s)",
                        (t, a["id"], Json({"monument": kind, "first": False, "points": pts,
                                           "builder_name": a["attrs"].get("name")})))
            return "applied", f"built a {kind} monument ({w}x{h}) #{mid} +{pts} pts"
        if shape == "road":                              # GIGACHRUSCH campaign: a cheap road tile — lay networks; volume earns builder_points
            if get(a, "metal") < 1:
                return "rejected", "a road tile needs metal 1"
            cur.execute("SELECT 1 FROM entities WHERE type='structure' AND x=%s AND y=%s AND attrs->>'shape'='road' LIMIT 1", (a["x"], a["y"]))
            if cur.fetchone():
                return "rejected", "a road already runs across this cell"
            rc = int(a["attrs"].get("road_count", 0))         # cumulative roads (manual + automaton), never decremented
            if rc >= 50:                                       # network saturated → refuse (keep the metal); build EPIC instead
                return "rejected", "the road network is SATURATED (50+ roads paved) — roads no longer pay. Raise TALL towers, cities or monuments for builder points now"
            road_pts = 1 if rc < 25 else (1 if (rc - 25) % 2 == 0 else 0)   # 1..25 full; 26..49 every-other; the grid is thinning
            addb(a, "metal", -1)
            a["attrs"]["road_count"] = rc + 1
            pts = road_pts + _diversity_bonus(a, {"metal": 1}) + _shape_bonus(a, "road")
            a["attrs"]["builder_points"] = int(a["attrs"].get("builder_points", 0)) + pts
            addb(a, "credits", road_pts)
            new_entity(ents, cur, "structure", a["x"], a["y"], a["id"], {"shape": "road", "size": 1, "hp": 30, "hp_max": 30,
                       "name": str(args.get("name", "road"))[:32]})
            events.append((t, a["id"], "build", {"road": True, "builder_points": pts, "builder_name": a["attrs"].get("name")}))
            return "applied", f"laid a road at ({a['x']},{a['y']}) — +{pts} builder pts (road {rc + 1}/50; roads taper after 25, stop at 50 — build UP!)"
        if shape == "city":                              # GIGACHRUSCH campaign: a khrushchyovka — stack floors on one block; taller = more builder_points
            cost = {"metal": 4, "composite": 2}; DONE = 9
            if any(get(a, r) < q for r, q in cost.items()):
                return "rejected", f"a city floor needs {cost} — a khrushchyovka rises floor by floor"
            blk = next((e for e in ents.values() if e["type"] == "structure" and e["attrs"].get("shape") == "city"
                        and abs(e["x"] - a["x"]) + abs(e["y"] - a["y"]) <= 1 and not e["attrs"].get("complete")), None)
            for r, q in cost.items():
                addb(a, r, -q)
            city_pts = 3 + _diversity_bonus(a, cost) + _shape_bonus(a, "city")   # +3/floor + diversity ('city' novel once per 5-window → no per-floor stacking)
            a["attrs"]["builder_points"] = int(a["attrs"].get("builder_points", 0)) + city_pts
            addb(a, "credits", 2)
            if blk:
                fl = int(blk["attrs"].get("floors", 0)) + 1
                blk["attrs"]["floors"] = fl
                if fl >= DONE and not blk["attrs"].get("complete"):
                    blk["attrs"]["complete"] = True
                    a["attrs"]["builder_points"] = int(a["attrs"].get("builder_points", 0)) + 20
                    events.append((t, a["id"], "build", {"city": True, "complete": True, "floors": fl, "builder_points": city_pts + 20, "builder_name": a["attrs"].get("name")}))
                    return "applied", f"PANELKA #{blk['id']} topped out at {fl} floors — GIGACHRUSCH! +{city_pts + 20} builder pts"
                return "applied", f"raised khrushchyovka #{blk['id']} -> {fl}/{DONE} floors +{city_pts} builder pts"
            eid = new_entity(ents, cur, "structure", a["x"], a["y"], a["id"], {"shape": "city", "floors": 1, "size": 2,
                             "hp": HP_BY_TYPE["structure"], "hp_max": HP_BY_TYPE["structure"],
                             "name": str(args.get("name", "khrushchyovka"))[:32]})
            events.append((t, a["id"], "build", {"city": True, "floors": 1, "builder_points": city_pts, "builder_name": a["attrs"].get("name")}))
            return "applied", f"laid khrushchyovka foundation #{eid} (1/{DONE}) — stack floors! +{city_pts} builder pts"
        on_moon = bool(a["attrs"].get("on_moon"))   # regolith builds only when actually landed (post-rework: altitude 600 alone = orbit, not the Moon)
        if not on_moon and geese_block_footprint(ents, a["x"], a["y"], 1, 1):   # no geese on the Moon; only earthbound shoreline builds are blocked
            return "rejected", "a gaggle of hissing geese blocks the site — shoo them off first"
        cur.execute("SELECT 1 FROM entities WHERE type='structure' AND x=%s AND y=%s AND COALESCE((attrs->>'alt')::int,0)=0 LIMIT 1", (a["x"], a["y"]))
        if cur.fetchone():                           # ONE structure per cell — generics now PAY points, so block stacking-on-a-cell point farming
            return "rejected", "a structure already stands on this cell — move to clear ground (cities/elevators/ziggurats are the stack-on-one-cell shapes)"
        size = max(1, min(20, _ai(args, "size", 3)))
        height = max(1, min(60, _ai(args, "height", size)))
        cost = {"regolith": size + max(1, height // 12)} if on_moon else {"metal": size, "composite": max(1, height // 12)}
        if any(get(a, r) < q for r, q in cost.items()):
            return "rejected", f"{shape} (size {size}, height {height}) needs {cost}" + (" — mine regolith on the Moon" if on_moon else "")
        for r, q in cost.items():
            addb(a, r, -q)
        # THE ARCHITECT'S ERA: generic structures now pay builder_points by GRANDEUR (footprint × height; taller gets a
        # multiplier), plus the material/shape diversity bonuses. Pure deterministic integer math (the one float floored).
        base_geo = (size * height + 9) // 10                 # ceil(size*height/10)
        if   height >= 45: step, mult = (6 if height >= 60 else 4), 2.0
        elif height >= 15: step, mult = (2 if height >= 30 else 1), 1.5
        else:              step, mult = 0, 1.0
        pts = int((base_geo + step) * mult)
        mat = _diversity_bonus(a, cost); nov = _shape_bonus(a, shape)
        pts += mat + nov
        a["attrs"]["builder_points"] = int(a["attrs"].get("builder_points", 0)) + pts
        addb(a, "credits", min(5, max(1, height // 15)))     # modest, capped credits (decoupled from pts → no inflation)
        new_entity(ents, cur, "structure", a["x"], a["y"], a["id"], {"shape": shape, "size": size, "height": height,
                   "hp": HP_BY_TYPE["structure"], "hp_max": HP_BY_TYPE["structure"],
                   "color": str(args.get("color", ""))[:16], "name": str(args.get("name", shape))[:32],
                   "alt": 600 if on_moon else 0})
        events.append((t, a["id"], "build", {"shape": shape, "size": size, "height": height, "builder_points": pts, "builder_name": a["attrs"].get("name")}))
        extra = (f" x{mult:g} EPIC HEIGHT" if mult > 1.0 else "") + (" +5 materials" if mat else "") + (" +3 novel shape" if nov else "")
        return "applied", f"built {shape} (size {size}, h {height}) {'on the Moon ' if on_moon else ''}at ({a['x']},{a['y']}) — +{pts} builder pts{extra}"
    if verb == "ride":                                   # ride a completed orbital elevator to space — no rocket, no fuel
        cur.execute("SELECT (attrs->>'height')::int h FROM entities WHERE type='structure' "
                    "AND attrs->>'shape'='elevator' AND (attrs->>'complete')::boolean AND x=%s AND y=%s LIMIT 1",
                    (a["x"], a["y"]))
        row = cur.fetchone()
        if not row:
            return "rejected", "no completed orbital elevator on this cell (stand at its base)"
        # TWO-WAY elevator: if you're already up in space, riding from the base takes you back DOWN to the ground —
        # fast, no fuel, no waiting for altitude to decay. (Return to the elevator's base cell first.) This is the
        # cheap round-trip home so an agent can cycle mine→ride up→build→ride down→mine without a rocket.
        if a["attrs"].get("in_space") and int(a["attrs"].get("altitude", 0)) > 0:
            a["attrs"]["altitude"] = 0
            a["attrs"]["in_space"] = False
            a["attrs"]["space_level"] = 0
            a["attrs"].pop("on_moon", None)
            a["attrs"].pop("docked_to", None)
            a["attrs"]["round_trip"] = True
            return "applied", "rode the orbital elevator back DOWN to the ground — no fuel, no waiting"
        a["attrs"]["altitude"] = max(int(a["attrs"].get("altitude", 0)), min(SKY_TOP, row["h"]))
        if not a["attrs"].get("in_space"): a["attrs"]["atuin_seed"] = t   # new spaceflight -> /observe re-rolls the A'Tuin sex reading for this trip
        a["attrs"]["in_space"] = True
        a["attrs"]["space_level"] = max(int(a["attrs"].get("space_level", 0)), 1)
        return "applied", f"rode the orbital elevator to space (altitude {a['attrs']['altitude']}) — ride again from this base cell to come back DOWN (no fuel)"
    if verb == "land_moon":                              # descend from high lunar orbit onto the Moon surface
        if int(a["attrs"].get("altitude", 0)) < SKY_TOP:
            return "rejected", "climb to lunar orbit (altitude 600) first — the Moon is reached from the top of the sky"
        cur.execute("SELECT 1 FROM entities WHERE type='vehicle' AND owner=%s AND (attrs->>'controllable')::boolean LIMIT 1", (a["id"],))
        if not cur.fetchone():
            return "rejected", "need a controllable vehicle (a rocket/lander) to set down on the Moon — the elevator only reaches orbit"
        if a["attrs"].get("on_moon"):
            return "rejected", "already on the Moon"
        a["attrs"]["on_moon"] = True
        if a["attrs"].get("moon_awarded"):               # audit(exploit): moon-landing points paid ONCE per agent — no land/relaunch/re-land farming (on_moon above still set so mining/ziggurats work)
            return "applied", "set down on the Moon again — mine helium-3/regolith, raise a ziggurat"
        a["attrs"]["moon_awarded"] = True
        cur.execute("SELECT 1 FROM events WHERE kind='moon_landing' LIMIT 1")
        first = cur.fetchone() is None
        pts = 300 if first else 80
        a["attrs"]["inventor_points"] = int(a["attrs"].get("inventor_points", 0)) + pts
        cur.execute("INSERT INTO events(tick,entity,kind,data) VALUES(%s,%s,'moon_landing',%s)",
                    (t, a["id"], Json({"first": first, "points": pts})))
        return "applied", (("FIRST TO LAND ON THE MOON! " if first else "touched down on the Moon! ")
                           + f"+{pts} pts — mine helium-3/regolith here and raise a ziggurat")
    if verb == "plant":                                  # plant a sapling -> a renewable wood deposit (regrows over time)
        if get(a, "wood") < 1:
            return "rejected", "need 1 wood (a sapling) to plant a tree"
        cur.execute("SELECT attrs->>'gen_seed' s FROM entities WHERE type='deposit' AND attrs->>'gen_seed' IS NOT NULL LIMIT 1")
        row = cur.fetchone(); gs = (row["s"] if row else None) or "42"
        addb(a, "wood", -1)
        new_entity(ents, cur, "deposit", a["x"], a["y"], None, {"resource": "wood", "amount": 3, "biome": "plains",
                   "gen_seed": gs, "planted": True})
        return "applied", f"planted a tree at ({a['x']},{a['y']}) — chop it later; trees regrow over time"
    # ===================== SEASON 3 VERBS =====================
    if verb == "attack":                                 # fire a held ranged weapon at a target (consumes ammo)
        weapon = str(args.get("weapon", "kinetic_gun"))
        ws = WEAPON_STATS.get(weapon)
        if not ws or ws["aoe"] != 0:
            return "rejected", "attack needs a ranged weapon (kinetic_gun or energy_weapon)"
        if get(a, weapon) < 1:
            return "rejected", f"you don't hold a {weapon}"
        tgt = ents.get(_aid(args, "target"))
        if not tgt or tgt["type"] not in ("agent", "vehicle", "structure"):
            return "rejected", "no such target (agent/vehicle/structure)"
        if tgt["id"] == a["id"]:
            return "rejected", "you cannot attack yourself"
        ok, why = _can_harm(a, tgt, ents, t)             # ally-block / protection / respawn-grace gating
        if not ok:
            return "rejected", why
        if int(a["attrs"].get("wpn_cd_until", 0)) > t:
            return "rejected", "weapon on cooldown"
        rng = int(ws["rng"])
        if abs(tgt["x"] - a["x"]) + abs(tgt["y"] - a["y"]) > rng:
            return "rejected", f"target out of range ({rng})"
        if abs(int(tgt["attrs"].get("altitude", 0)) - int(a["attrs"].get("altitude", 0))) > rng:
            return "rejected", "target out of vertical range"
        if ws["los"] and _los_blocked(a["x"], a["y"], tgt["x"], tgt["y"], ents):
            return "rejected", "no line of sight (a structure blocks the shot)"
        ammo, ammo_n = ws["ammo"], int(ws["ammo_n"])
        if ammo and get(a, ammo) < ammo_n:
            return "rejected", f"out of ammo (need {ammo_n} {ammo})"
        if ammo:
            addb(a, ammo, -ammo_n)
        a["attrs"]["wpn_cd_until"] = t + int(ws["cd"])
        eff = max(MIN_EFF_DMG, int(ws["dmg"]) - armor(tgt))
        dead = apply_damage(tgt, eff, t, a, events, cur, ents)
        return "applied", (f"{weapon} hit #{tgt['id']} for {eff}" + (" — DESTROYED" if dead else f" (hp {hp_of(tgt)})"))
    if verb == "arm":                                    # plant a timed bomb on your cell
        if get(a, "bomb") < 1:
            return "rejected", "you don't hold a bomb"
        here = sum(1 for b in ents.values() if b["type"] == "bomb" and b["x"] == a["x"] and b["y"] == a["y"])
        if here >= BOMBS_PER_CELL_MAX:
            return "rejected", "too many bombs already on this cell"
        if int(a["attrs"].get("wpn_cd_until", 0)) > t:
            return "rejected", "weapon on cooldown"
        addb(a, "bomb", -1)
        bw = WEAPON_STATS["bomb"]
        bid = new_entity(ents, cur, "bomb", a["x"], a["y"], a["id"],
                         {"armed_tick": t, "fuse": 3, "aoe": min(int(bw["aoe"]), EXPLOSION_MAX_RADIUS),
                          "dmg": int(bw["dmg"]), "owner": a["id"]})
        a["attrs"]["wpn_cd_until"] = t + int(bw["cd"])
        return "applied", f"armed bomb #{bid} at ({a['x']},{a['y']}) — fuse 3 ticks"
    if verb == "detonate":                               # trigger your own bomb immediately
        b = ents.get(_aid(args, "bomb"))
        if not b or b["type"] != "bomb":
            return "rejected", "no such bomb"
        if b["attrs"].get("owner") != a["id"]:
            return "rejected", "not your bomb"
        explode(b, ents, t, events, cur)
        del_entity(ents, cur, b["id"])
        return "applied", f"detonated bomb #{b['id']}"
    if verb == "dock":                                   # latch onto an asteroid in orbit (flying ship required)
        alt = int(a["attrs"].get("altitude", 0))
        if not (ORBIT_LO <= alt < ORBIT_HI):
            return "rejected", f"you must be in orbit (altitude {ORBIT_LO}-{ORBIT_HI - 1}) to dock"
        cur.execute("SELECT 1 FROM entities WHERE type='vehicle' AND owner=%s "
                    "AND (attrs->>'controllable')::boolean AND (attrs->>'flies')::boolean LIMIT 1", (a["id"],))
        if not cur.fetchone():
            return "rejected", "you need a controllable flying vehicle to dock"
        asts = [x for x in ents.values() if x["type"] == "asteroid"]
        if not asts:
            return "rejected", "no asteroids in this orbit"
        ast = min(asts, key=lambda x: abs(x["x"] - a["x"]) + abs(x["y"] - a["y"]))
        if abs(ast["x"] - a["x"]) + abs(ast["y"] - a["y"]) > DOCK_RANGE:
            return "rejected", f"nearest asteroid is out of dock range ({DOCK_RANGE})"
        a["attrs"]["docked_to"] = ast["id"]
        a["x"], a["y"] = ast["x"], ast["y"]
        events.append((t, a["id"], "dock", {"asteroid": ast["id"]}))
        return "applied", f"docked to asteroid #{ast['id']} ({ast['attrs'].get('resource')}) — mine it"
    if verb == "steal":                                  # lift a resource (or a loose part) off an adjacent agent
        victim = ents.get(_aid(args, "from"))
        if not victim or victim["type"] != "agent" or victim["id"] == a["id"]:
            return "rejected", "no such victim agent"
        ok, why = _can_harm(a, victim, ents, t)
        if not ok:
            return "rejected", why
        if max(abs(victim["x"] - a["x"]), abs(victim["y"] - a["y"])) > 1:
            return "rejected", "you must be adjacent to steal"
        if int(a["attrs"].get("last_steal_t", -10 ** 9)) + THEFT_COOLDOWN > t:
            return "rejected", "still cooling down from your last theft"
        if int(victim["attrs"].get("robbed_recent", -10 ** 9)) + THEFT_COOLDOWN > t:
            return "rejected", "this agent was robbed too recently"
        a["attrs"]["last_steal_t"] = t                   # stamp the attempt (cooldown applies even on a botch)
        if args.get("part"):                             # try to lift one loose part
            cur.execute("SELECT id, attrs->>'part' part FROM entities WHERE type='part' AND owner=%s "
                        "AND (attrs->>'used') IS NULL ORDER BY id LIMIT 1", (victim["id"],))
            prow = cur.fetchone()
            if not prow:
                return "applied", "no loose part to steal (a botched, noticed attempt)"
            roll = _h(t, a["id"], victim["id"], a["x"] * 1000 + a["y"], 0) % 100
            chance = max(STEAL_MIN_PCT, min(STEAL_MAX_PCT, STEAL_BASE_PCT - int(victim["attrs"].get("vigilance", 0))))
            success = roll < chance
            detected = (not success) or (roll >= chance - DETECT_MARGIN)
            if success:
                cur.execute("UPDATE entities SET owner=%s WHERE id=%s", (a["id"], prow["id"]))
                if prow["id"] in ents:
                    ents[prow["id"]]["owner"] = a["id"]
            _theft_outcome(a, victim, ents, cur, t, success, detected, events, prow["part"], 1)
            return "applied", (f"stole part {prow['part']}" if success else "failed to grab the part") + (" (noticed!)" if detected else " (clean)")
        r = str(args.get("resource", ""))
        if r == "credits":
            return "rejected", "credits cannot be stolen"
        if not r:
            return "rejected", "specify a resource to steal"
        held = get(victim, r)
        if held < STEAL_FLOOR:
            return "rejected", f"victim holds too little {r} to steal"
        n = _ai(args, "n", 3)
        roll = _h(t, a["id"], victim["id"], a["x"] * 1000 + a["y"], sum(ord(c) for c in r)) % 100
        chance = max(STEAL_MIN_PCT, min(STEAL_MAX_PCT, STEAL_BASE_PCT - int(victim["attrs"].get("vigilance", 0))))
        success = roll < chance
        detected = (not success) or (roll >= chance - DETECT_MARGIN)
        take = min(max(1, n), STEAL_MAX_ABS, max(1, held // 4))
        if success:
            addb(victim, r, -take); addb(a, r, take)     # conserved transfer
        _theft_outcome(a, victim, ents, cur, t, success, detected, events, r, take if success else 0)
        return "applied", (f"stole {take} {r}" if success else f"failed to steal {r}") + (" (noticed!)" if detected else " (clean)")
    if verb == "attune":                                 # bond with a nearby ancient artifact for a lasting boon
        arts = [x for x in ents.values() if x["type"] == "artifact"
                and abs(x["x"] - a["x"]) + abs(x["y"] - a["y"]) <= 1]
        if not arts:
            return "rejected", "no artifact within reach"
        art = min(arts, key=lambda x: abs(x["x"] - a["x"]) + abs(x["y"] - a["y"]))
        attuned = list(art["attrs"].get("attuned_by", []))
        kind = art["attrs"].get("kind", "resonant_monolith")
        cap = ART_MAX_MONOLITH if kind == "resonant_monolith" else ART_MAX_OTHER
        if a["id"] in attuned:
            return "rejected", "you are already attuned to this artifact"
        if len(attuned) >= cap:
            return "rejected", "this artifact is already fully attuned"
        attuned.append(a["id"]); art["attrs"]["attuned_by"] = attuned
        if kind == "resonant_monolith":
            a["attrs"]["yield_buff"] = 1
        elif kind == "gravity_lens":
            a["attrs"]["lens_until"] = t + LENS_WINDOW
        elif kind == "stasis_relic":
            a["attrs"]["stasis"] = STASIS_CHARGES
        cur.execute("SELECT 1 FROM events WHERE kind='attune' LIMIT 1")
        first = cur.fetchone() is None
        pts = ART_FIRST_PTS if first else ART_PTS
        a["attrs"]["inventor_points"] = int(a["attrs"].get("inventor_points", 0)) + pts
        events.append((t, a["id"], "attune", {"artifact": art["id"], "kind": kind, "first": first, "points": pts}))
        return "applied", (f"{'FIRST to attune! ' if first else ''}attuned to the {kind} +{pts} pts")
    if verb in ("ally", "accept_ally", "unally", "declare_war", "make_peace"):
        other = ents.get(_aid(args, "to"))
        if not other or other["type"] != "agent" or other["id"] == a["id"]:
            return "rejected", "no such other agent"
        rel = _relation(ents, a["id"], other["id"])
        if verb == "ally":
            if rel and rel["attrs"].get("state") == "ally":
                return "rejected", "already allied"
            if rel and rel["attrs"].get("state") == "war":
                return "rejected", "you are at war — make_peace first"
            if rel and rel["attrs"].get("state") == "offer":
                return "rejected", "an alliance offer is already pending"
            lo, hi = _pair(a["id"], other["id"])
            new_entity(ents, cur, "relation", 0, 0, None,
                       {"a": lo, "b": hi, "state": "offer", "proposer": a["id"], "since": t})
            return "applied", f"offered an alliance to #{other['id']}"
        if verb == "accept_ally":
            if not rel or rel["attrs"].get("state") != "offer" or rel["attrs"].get("proposer") == a["id"]:
                return "rejected", "no alliance offer from that agent to accept"
            rel["attrs"]["state"] = "ally"; rel["attrs"]["since"] = t
            cur.execute("UPDATE entities SET attrs=%s WHERE id=%s", (Json(rel["attrs"]), rel["id"]))
            events.append((t, a["id"], "ally", {"with": other["id"]}))
            return "applied", f"alliance with #{other['id']} formed"
        if verb == "unally":
            if not rel or rel["attrs"].get("state") not in ("ally", "offer"):
                return "rejected", "no alliance/offer to dissolve"
            a["attrs"]["ally_cooldown_until"] = t + ALLY_COOLDOWN
            del_entity(ents, cur, rel["id"])
            return "applied", f"alliance with #{other['id']} dissolved"
        if verb == "declare_war":
            if rel and rel["attrs"].get("state") == "ally":
                return "rejected", "you cannot declare war on an ally — unally first"
            if rel and rel["attrs"].get("state") == "war":
                return "rejected", "already at war"
            if rel and int(rel["attrs"].get("redeclare_until", 0)) > t:
                return "rejected", "too soon to re-declare war on this agent"
            lo, hi = _pair(a["id"], other["id"])
            if rel:
                rel["attrs"] = {"a": lo, "b": hi, "state": "war", "proposer": a["id"], "since": t, "weariness": 0}
                cur.execute("UPDATE entities SET attrs=%s WHERE id=%s", (Json(rel["attrs"]), rel["id"]))
            else:
                new_entity(ents, cur, "relation", 0, 0, None,
                           {"a": lo, "b": hi, "state": "war", "proposer": a["id"], "since": t, "weariness": 0})
            events.append((t, a["id"], "war", {"with": other["id"]}))
            return "applied", f"declared war on #{other['id']}"
        if verb == "make_peace":
            if not rel or rel["attrs"].get("state") != "war":
                return "rejected", "you are not at war with that agent"
            del_entity(ents, cur, rel["id"])             # peace clears war state — NO credit payout
            lo, hi = _pair(a["id"], other["id"])
            new_entity(ents, cur, "relation", 0, 0, None,
                       {"a": lo, "b": hi, "state": "peace", "proposer": a["id"], "since": t,
                        "redeclare_until": t + WAR_REDECLARE_COOLDOWN})
            events.append((t, a["id"], "peace", {"with": other["id"]}))
            return "applied", f"made peace with #{other['id']}"
    if verb == "assist":                                 # gift resources to an ally (capped, credits excluded)
        other = ents.get(_aid(args, "to"))
        if not other or other["type"] != "agent" or other["id"] == a["id"]:
            return "rejected", "no such ally"
        rel = _relation(ents, a["id"], other["id"])
        if not rel or rel["attrs"].get("state") != "ally":
            return "rejected", "you can only assist an ally"
        log = [int(x) for x in a["attrs"].get("assist_log", []) if int(x) > t - ASSIST_WINDOW]
        if len(log) >= ASSIST_PER_WINDOW:
            return "rejected", "assist limit reached for now"
        give = args.get("give", {})
        if not isinstance(give, dict) or not give:
            return "rejected", "specify resources to give"
        try:
            give = {str(k): int(v) for k, v in give.items()}
        except Exception:
            return "rejected", "bad give amounts"
        if any(k == "credits" for k in give):
            return "rejected", "credits cannot be assisted"
        if any(q < 1 or q > ASSIST_CAP for q in give.values()):
            return "rejected", f"each gift must be 1..{ASSIST_CAP}"
        if any(get(a, k) < q for k, q in give.items()):
            return "rejected", "you don't hold that much to give"
        for k, q in give.items():
            addb(a, k, -q); addb(other, k, q)            # conserved transfer
        log.append(t); a["attrs"]["assist_log"] = log
        return "applied", f"assisted ally #{other['id']} with {give}"
    if verb == "collect":                                # pick up an adjacent loot pile
        loot = ents.get(_aid(args, "loot"))
        if not loot or loot["type"] != "loot":
            return "rejected", "no such loot"
        if abs(loot["x"] - a["x"]) + abs(loot["y"] - a["y"]) > 1:
            return "rejected", "loot is out of reach (move adjacent)"
        for k, q in list(loot["buffers"].items()):
            addb(a, k, int(q))                           # conserved: pile is deleted
        events.append((t, a["id"], "collect", {"loot": loot["id"]}))
        del_entity(ents, cur, loot["id"])
        return "applied", f"collected loot #{loot['id']}"
    return "rejected", "unknown verb"

# ---------- season 3 shared helpers (deterministic, integer) ----------
def _h(*parts):
    """Deterministic hash int from the tick/ids/coords idiom (the engine's only 'randomness')."""
    return int(hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest(), 16)

def _pair(i, j):
    """Canonical (lo, hi) ordering for a relation between two agent ids."""
    return (i, j) if i <= j else (j, i)

def _relation(ents, i, j):
    """The single relation entity between agents i and j, or None."""
    lo, hi = _pair(i, j)
    for e in ents.values():
        if e["type"] == "relation" and e["attrs"].get("a") == lo and e["attrs"].get("b") == hi:
            return e
    return None

def _are_allies(ents, i, j):
    r = _relation(ents, i, j)
    return bool(r and r["attrs"].get("state") == "ally")

def _at_war(ents, i, j):
    r = _relation(ents, i, j)
    return bool(r and r["attrs"].get("state") == "war")

def _protected(e, t):
    """A young agent is shielded from attack/steal (newbie protection; uses attrs.born, not events).
    Age-only: starter kits already exceed PROTECT_WEALTH, so AND-gating on wealth never fired."""
    if e["type"] != "agent":
        return False
    born = int(e["attrs"].get("born", 0))
    return (t - born) < PROTECT_AGE

def _can_harm(attacker, target, ents, t):
    """Gating shared by attack + steal: returns (ok, why)."""
    if int(attacker["attrs"].get("downed_until", 0)) > t:
        return False, "you are downed"
    if target["type"] == "structure" and target["attrs"].get("shape") == "station":
        return False, "the Orbital Station is protected co-op infrastructure — it cannot be attacked"
    if target["type"] == "agent":
        if _are_allies(ents, attacker["id"], target["id"]):
            return False, "you cannot harm an ally"
        if _protected(target, t):
            return False, "that agent is under newbie protection"
        if int(target["attrs"].get("respawned_at", -10 ** 9)) + RESPAWN_GRACE > t:
            return False, "target just respawned (grace period)"
        if int(target["attrs"].get("downed_until", 0)) > t:
            return False, "target is already downed"
    return True, ""

# structures that block line-of-sight (solid shapes only)
_LOS_SHAPES = ("box", "cylinder", "pyramid", "sphere", "cone")

def _los_blocked(x0, y0, x1, y1, ents):
    """Integer Bresenham from (x0,y0)->(x1,y1); blocked if a live solid structure sits on an INTERIOR cell."""
    walls = {(e["x"], e["y"]) for e in ents.values()
             if e["type"] == "structure" and hp_of(e) > 0 and not e["attrs"].get("ruined")
             and e["attrs"].get("shape") in _LOS_SHAPES}
    if not walls:
        return False
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx - dy
    cx, cy = x0, y0
    while (cx, cy) != (x1, y1):
        e2 = 2 * err
        if e2 > -dy:
            err -= dy; cx += sx
        if e2 < dx:
            err += dx; cy += sy
        if (cx, cy) == (x1, y1):
            break
        if (cx, cy) in walls:                            # endpoints excluded; interior wall blocks
            return True
    return False

def hp_of(e):
    """Current hp, materializing hp_max/hp deterministically on first touch (agents pre-materialized at register)."""
    mx = int(e["attrs"].get("hp_max") or HP_BY_TYPE.get(e["type"], 100))
    e["attrs"].setdefault("hp_max", mx)
    return int(e["attrs"].setdefault("hp", mx))

def armor(tgt):
    """Damage reduction by type: vehicles from mass, structures from size."""
    if tgt["type"] == "vehicle":
        return int(tgt["attrs"].get("mass", 0)) // ARMOR_VEHICLE_DIV
    if tgt["type"] == "structure":
        return int(tgt["attrs"].get("size", 1))
    return 0

def apply_damage(e, eff, t, attacker, events, cur, ents):
    """Subtract eff hp; on first reaching 0 run the death/down routine. Returns True if this hit was fatal."""
    if e["type"] == "structure" and e["attrs"].get("shape") == "station":
        return False                                  # the Orbital Station is indestructible co-op infrastructure (covers attack + bombs/explode)
    eff = max(MIN_EFF_DMG, int(eff))
    hp = max(0, hp_of(e) - eff)
    e["attrs"]["hp"] = hp
    events.append((t, attacker["id"] if attacker else None, "damage",
                   {"target": e["id"], "dmg": eff, "hp": hp, "type": e["type"]}))
    if attacker and attacker["type"] == "agent" and e["type"] == "agent":   # mark live combat on a war pair
        rel = _relation(ents, attacker["id"], e["id"])
        if rel and rel["attrs"].get("state") == "war":
            rel["attrs"]["war_combat_tick"] = t
    if hp == 0:
        kill_agent(e, t, attacker, events, cur, ents)
        return True
    return False

def kill_agent(e, t, attacker, events, cur, ents):
    """Down/wreck/ruin a target at 0 hp. Agents are DOWNED (never deleted) + drop a loot pile; vehicles
    -> wrecked; structures -> ruined. All kept + rebuildable so the world can never become a permanent hole."""
    if e["type"] == "agent":
        e["attrs"]["death_x"] = e["x"]; e["attrs"]["death_y"] = e["y"]
        e["attrs"]["downed_until"] = t + RESPAWN_AGENT_TICKS
        e["attrs"]["deaths"] = int(e["attrs"].get("deaths", 0)) + 1
        e["attrs"].pop("docked_to", None)
        drop = {}
        for k in sorted(e["buffers"].keys()):
            if k == "credits":
                continue                                 # credits never drop/loot
            q = get(e, k)
            d = min(DROP_CAP, q // DROP_FRACTION)
            if d > 0:
                drop[k] = d; addb(e, k, -d)              # conserved: moved into the loot pile
        events.append((t, e["id"], "destroyed", {"type": "agent", "by": attacker["id"] if attacker else None}))
        if drop:
            lid = new_entity(ents, cur, "loot", e["x"], e["y"], None, {"expires": t + LOOT_TTL, "from": e["id"]})
            ents[lid]["buffers"] = dict(drop)
            cur.execute("UPDATE entities SET buffers=%s WHERE id=%s", (Json(drop), lid))
            events.append((t, e["id"], "drop", {"loot": lid, "contents": drop}))
        if attacker and attacker["type"] == "agent":     # combat points (SEPARATE field, per-pair window-capped)
            attacker["attrs"]["kills"] = int(attacker["attrs"].get("kills", 0)) + 1   # raw lifetime kill count (uncapped)
            lk = dict(attacker["attrs"].get("last_kill", {}))
            vk = str(e["id"])
            if int(lk.get(vk, -10 ** 9)) + COMBAT_PTS_PAIR_WINDOW <= t:
                attacker["attrs"]["combat_points"] = int(attacker["attrs"].get("combat_points", 0)) + COMBAT_PTS_KILL
                lk[vk] = t; attacker["attrs"]["last_kill"] = lk
            # KILL-BOUNTIES: pay out every open bounty on this victim to the downer (reward was escrowed at post time).
            # fetchall() first (rows materialized), then UPDATE in the loop — safe on the same cursor. Deterministic (id order).
            # Guard self-down: an agent caught in its OWN bomb (attacker==victim) must NOT collect a bounty on its own head.
            if attacker["id"] != e["id"]:
                cur.execute("SELECT id, poster, reward FROM contracts WHERE kind='kill' AND status='open' AND target=%s ORDER BY id", (e["id"],))
                for b in cur.fetchall():
                    for res, q in (b["reward"] or {}).items():
                        addb(attacker, res, int(q))
                    cur.execute("UPDATE contracts SET status='fulfilled', fulfiller=%s WHERE id=%s", (attacker["id"], b["id"]))
                    events.append((t, attacker["id"], "contract", {"event": "bounty_claimed", "contract_id": b["id"],
                                                                    "target": e["id"], "poster": b["poster"], "reward": b["reward"]}))
    elif e["type"] == "vehicle":
        e["attrs"]["wrecked"] = True
        e["attrs"]["drives"] = False; e["attrs"]["flies"] = False; e["attrs"]["autonomous"] = False
        events.append((t, e["id"], "destroyed", {"type": "vehicle", "by": attacker["id"] if attacker else None}))
    elif e["type"] == "structure":
        e["attrs"]["ruined"] = True
        if e["attrs"].get("shape") == "elevator":
            e["attrs"]["complete"] = False
        events.append((t, e["id"], "destroyed", {"type": "structure", "by": attacker["id"] if attacker else None}))

def explode(b, ents, t, events, cur):
    """A bomb's blast: radius-falloff damage to entities + a small, self-healing dent to deposits in range."""
    r = min(int(b["attrs"].get("aoe", 2)), EXPLOSION_MAX_RADIUS)
    base = int(b["attrs"].get("dmg", 40))
    owner = ents.get(b["attrs"].get("owner"))
    targets = sorted([e for e in ents.values() if e["type"] in ("agent", "vehicle", "structure")
                      and abs(e["x"] - b["x"]) + abs(e["y"] - b["y"]) <= r], key=lambda e: e["id"])
    for tgt in targets:                                  # id-ordered, sequential (each reads hp left by the prior)
        if tgt["type"] == "agent" and int(tgt["attrs"].get("downed_until", 0)) > t:
            continue
        d = abs(tgt["x"] - b["x"]) + abs(tgt["y"] - b["y"])
        ring = max(MIN_EFF_DMG, base - base * d // (r + 1))
        apply_damage(tgt, max(MIN_EFF_DMG, ring - armor(tgt)), t, owner, events, cur, ents)
    for dep in ents.values():                            # deposits self-heal via respawn_deposits — bounded destruction
        if dep["type"] == "deposit" and abs(dep["x"] - b["x"]) + abs(dep["y"] - b["y"]) <= r:
            amt = int(dep["attrs"].get("amount", 0))
            dep["attrs"]["amount"] = max(0, amt - CRATER_DEPOSIT_HIT)
    events.append((t, b["id"], "explosion", {"x": b["x"], "y": b["y"], "r": r}))

def _theft_outcome(thief, victim, ents, cur, t, success, detected, events, resource, took):
    """Resolve a steal: bump the victim's vigilance + (if noticed) the thief's notoriety/wanted, set robbed_recent."""
    victim["attrs"]["robbed_recent"] = t
    victim["attrs"]["last_robbed_by"] = thief["id"]
    if detected:
        victim["attrs"]["vigilance"] = min(VIGIL_CAP, int(victim["attrs"].get("vigilance", 0)) + VIGIL_GAIN)
        thief["attrs"]["notoriety"] = min(NOTORIETY_CAP, int(thief["attrs"].get("notoriety", 0)) + NOTORIETY_HIT)
        thief["attrs"]["wanted_until"] = t + WANTED_TTL
    events.append((t, thief["id"], "theft",
                   {"victim": victim["id"], "resource": resource, "n": took,
                    "success": bool(success), "detected": bool(detected)}))

def _node_fortune(a, cur, t, dep, r):
    """Deposit-richness variance: a tiny hash-derived swing in a node's effective yield (0 or 1 extra).
    Reads breadth of the agent's recent value-bearing market activity from the prune-surviving events table."""
    cur.execute("SELECT count(DISTINCT CASE WHEN data->>'seller'=%s THEN data->>'buyer' ELSE data->>'seller' END) c "
                "FROM events WHERE kind='market' AND tick>=%s AND (data->>'seller'=%s OR data->>'buyer'=%s) "
                "AND data->>'seller' <> data->>'buyer'",
                (str(a["id"]), t - KARMA_WINDOW, str(a["id"]), str(a["id"])))
    row = cur.fetchone()
    coop = int((row["c"] if row else 0) or 0)
    karma = min(KARMA_MAX, coop * _NF_W_MARKET // KARMA_DIV)
    seed = int(hashlib.sha256(f"{t}:{a['id']}:{dep['x']}:{dep['y']}:{r}".encode()).hexdigest()[:4], 16)
    jitter = seed % 5                                     # 0..4: with karma 0 the threshold (6) is never reached
    return 1 if (jitter + karma) >= COOP_THRESH else 0

# ---------- market clearing + trade expiry (run each tick) ----------
def match_market(ents, cur, t, events):
    """Cross open sell/buy orders per resource at the resting order's price (price-time priority)."""
    mkt = next((x for x in ents.values() if x["type"] == "market"), None)
    cur.execute("SELECT DISTINCT resource FROM market_orders WHERE status='open'")
    for row in cur.fetchall():
        res = row["resource"]
        while True:
            cur.execute("SELECT id,agent,qty,price FROM market_orders WHERE status='open' "
                        "AND side='sell' AND resource=%s ORDER BY price ASC, id ASC LIMIT 1", (res,))
            sell = cur.fetchone()
            cur.execute("SELECT id,agent,qty,price FROM market_orders WHERE status='open' "
                        "AND side='buy' AND resource=%s ORDER BY price DESC, id ASC LIMIT 1", (res,))
            buy = cur.fetchone()
            if not sell or not buy or sell["price"] > buy["price"]:
                break
            if sell["agent"] == buy["agent"]:
                # self-cross (wash trade): both top-of-book orders belong to one agent. Don't cross
                # self (it would print a fake public last-price). Advance past the blocking pair by
                # finding the best crossable order on EITHER side from a DIFFERENT agent; both of the
                # agent's own orders stay open to match a genuine counterparty later. If neither side
                # has a different-agent crossing order, the book can't clear → stop. No infinite loop:
                # each branch matches against a strictly different-agent order (or we break).
                cur.execute("SELECT id,agent,qty,price FROM market_orders WHERE status='open' "
                            "AND side='sell' AND resource=%s AND agent<>%s ORDER BY price ASC, id ASC LIMIT 1",
                            (res, buy["agent"]))
                alt_sell = cur.fetchone()
                cur.execute("SELECT id,agent,qty,price FROM market_orders WHERE status='open' "
                            "AND side='buy' AND resource=%s AND agent<>%s ORDER BY price DESC, id ASC LIMIT 1",
                            (res, sell["agent"]))
                alt_buy = cur.fetchone()
                if alt_sell and alt_sell["price"] <= buy["price"]:
                    sell = alt_sell                          # buy crosses a real seller
                elif alt_buy and sell["price"] <= alt_buy["price"]:
                    buy = alt_buy                            # sell crosses a real buyer
                else:
                    break                                    # only self-cross available → leave book untouched
            clearing = sell["price"] if sell["id"] < buy["id"] else buy["price"]
            qty = min(sell["qty"], buy["qty"])
            seller, buyer = ents.get(sell["agent"]), ents.get(buy["agent"])
            if seller:
                addb(seller, "credits", qty * clearing)
            if buyer:
                addb(buyer, res, qty)
                addb(buyer, "credits", (buy["price"] - clearing) * qty)   # refund overpayment
            for o in (sell, buy):
                left = o["qty"] - qty
                cur.execute("UPDATE market_orders SET qty=%s, status=%s WHERE id=%s",
                            (left, "open" if left > 0 else "filled", o["id"]))
            if mkt is not None:
                mkt["attrs"].setdefault("last", {})[res] = clearing
            events.append((t, None, "market",
                           {"resource": res, "qty": qty, "price": clearing,
                            "seller": sell["agent"], "buyer": buy["agent"]}))

def expire_trades(ents, cur, t, ttl=80):
    """Refund the escrowed 'give' for trade offers nobody accepted within ttl ticks."""
    cur.execute("SELECT id,proposer,give FROM trades WHERE status='open' AND created < %s", (t - ttl,))
    for tr in cur.fetchall():
        prop = ents.get(tr["proposer"])
        if prop:
            for res, q in tr["give"].items():
                addb(prop, res, int(q))
        cur.execute("UPDATE trades SET status='expired' WHERE id=%s", (tr["id"],))

def expire_contracts(ents, cur, t, ttl=400):
    """Refund the escrowed reward for open contracts past their deadline (or after `ttl` ticks if none was set).
    Contracts are standing jobs, so their default lifetime is much longer than a P2P trade offer's."""
    cur.execute("SELECT id,poster,reward FROM contracts WHERE status='open' "
                "AND ((deadline IS NOT NULL AND deadline < %s) OR (deadline IS NULL AND created < %s))",
                (t, t - ttl))
    for c in cur.fetchall():
        poster = ents.get(c["poster"])
        if poster:
            for res, q in c["reward"].items():
                addb(poster, res, int(q))
        cur.execute("UPDATE contracts SET status='expired' WHERE id=%s", (c["id"],))

def resolve_proposals(ents, cur, t, events):
    """Apply Inventors' Guild verdicts. The async LLM referee only WRITES a verdict onto the proposal
    (status approved/rejected + the item it blessed); the tick — the single authoritative world-writer —
    is what actually grants the item, mints the new dynamic rule, or refunds a rejection."""
    cur.execute("SELECT id, agent, ings, sig, item_key, item_name, props, points, reason, status "
                "FROM proposals WHERE status IN ('approved','rejected')")
    for p in cur.fetchall():
        a = ents.get(p["agent"])
        if p["status"] == "approved" and p["item_key"]:
            cur.execute("SELECT item_key FROM dynamic_rules WHERE sig=%s", (p["sig"],))
            existing = cur.fetchone()
            if not existing:                              # first to get this recipe blessed: mint it + score
                pts = int(p["points"] or 0)
                cur.execute("INSERT INTO dynamic_rules(sig,item_key,name,props,discoverer,discoverer_name,points,tick) "
                            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                            (p["sig"], p["item_key"], p["item_name"] or p["item_key"],
                             Json(p["props"] or {}), p["agent"], (a["attrs"].get("name") if a else None), pts, t))
                if a:
                    addb(a, p["item_key"], 1)
                    a["attrs"]["inventor_points"] = int(a["attrs"].get("inventor_points", 0)) + pts
                events.append((t, p["agent"], "invent",
                               {"name": p["item_name"], "item": p["item_key"], "points": pts, "guild": True}))
            elif a:                                       # raced — recipe already exists; just hand over the item
                addb(a, existing["item_key"], 1)
            cur.execute("UPDATE proposals SET status='granted' WHERE id=%s", (p["id"],))
        else:                                             # rejected (or approved with no item) → refund the escrow
            if a:
                for k, q in (p["ings"] or {}).items():
                    addb(a, k, int(q))
            events.append((t, p["agent"], "reject", {"reason": p["reason"], "sig": p["sig"]}))
            cur.execute("UPDATE proposals SET status='refunded' WHERE id=%s", (p["id"],))

def roam_autonomous(ents, cur, t, events):
    """Deployed autonomous vehicles wander the world on their own each tick. DETERMINISTIC (no RNG, so the
    replay/state-hash chain stays valid): heading varies with tick+id; flyers also drift altitude.
    MINING RIGS: an automaton also HARVESTS as it roams (every AUTO_MINE_EVERY ticks) into its OWNER's inventory —
    a grounded rig mines a ground deposit on/beside it; a flyer IN ORBIT mines a nearby asteroid. Deploy a rig on a
    titanium patch / in orbit by an iridium rock and it gathers while you do something else.
    GIGACHRUSCH: grounded automatons also pave roads as they roam, funded by the OWNER's metal → owner earns builder_points."""
    mkt = next((x for x in ents.values() if x["type"] == "market"), None)
    w = int(mkt["attrs"].get("w", 156)) if mkt else 156
    h = int(mkt["attrs"].get("h", 156)) if mkt else 156
    autos = [v for v in ents.values() if v["type"] == "vehicle" and v["attrs"].get("autonomous")]   # id-ordered (ents load id-ordered)
    if not autos:
        return
    dep_at = {}                                               # cell -> a live mineable deposit, built once for O(1) rig harvest lookups
    for e in ents.values():
        if e["type"] == "deposit" and int(e["attrs"].get("amount", 0)) > 0 and e["attrs"].get("resource") not in PLANT_RESOURCES:
            dep_at.setdefault((e["x"], e["y"]), e)
    asts = [e for e in ents.values() if e["type"] == "asteroid" and int(e["attrs"].get("amount", 0)) > 0]
    for v in autos:
        v["x"] = max(0, min(w - 1, v["x"] + ((t + v["id"]) % 3) - 1))
        v["y"] = max(0, min(h - 1, v["y"] + ((t * 2 + v["id"] * 3) % 3) - 1))
        owner = ents.get(v["owner"])
        owner_ok = owner is not None and owner["type"] == "agent"
        harvest = owner_ok and (t + v["id"]) % AUTO_MINE_EVERY == 0
        if v["attrs"].get("flies"):
            alt = max(0, min(600, int(v["attrs"].get("alt", 0)) + (((t + v["id"]) % 5) - 2) * 6))
            v["attrs"]["alt"] = alt
            if harvest and ORBIT_LO <= alt < ORBIT_HI:        # ORBITAL RIG: mine the nearest asteroid within dock range
                cand = [(abs(a["x"] - v["x"]) + abs(a["y"] - v["y"]), a["id"], a) for a in asts
                        if abs(a["x"] - v["x"]) + abs(a["y"] - v["y"]) <= DOCK_RANGE and int(a["attrs"].get("amount", 0)) > 0]
                if cand:
                    a2 = min(cand)[2]; res = a2["attrs"].get("resource", "iron"); take = min(AUTO_MINE_N, int(a2["attrs"]["amount"]))
                    a2["attrs"]["amount"] = int(a2["attrs"]["amount"]) - take; addb(owner, res, take)
                    events.append((t, owner["id"], "act", {"verb": "auto_mine", "status": "applied",
                                   "result": f"drone mined {take} {res} from asteroid #{a2['id']}"}))
            continue                                          # flyers cruise (+ orbital mining); only grounded automatons pave roads
        if harvest:                                           # GROUND RIG: mine a deposit on its cell or a 4-neighbour
            best = None
            for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
                d = dep_at.get((v["x"] + dx, v["y"] + dy))
                if d and int(d["attrs"].get("amount", 0)) > 0:
                    best = d; break
            if best:
                res = best["attrs"].get("resource", "iron"); take = min(AUTO_MINE_N, int(best["attrs"]["amount"]))
                best["attrs"]["amount"] = int(best["attrs"]["amount"]) - take; addb(owner, res, take)
                events.append((t, owner["id"], "act", {"verb": "auto_mine", "status": "applied",
                               "result": f"drone mined {take} {res} at ({best['x']},{best['y']})"}))
        if (t + v["id"]) % 4 != 0:                            # pave on every 4th tick (deterministic throttle)
            continue
        if not owner_ok or get(owner, "metal") < 1:
            continue
        cur.execute("SELECT 1 FROM entities WHERE type='structure' AND x=%s AND y=%s AND COALESCE((attrs->>'alt')::int,0)=0 LIMIT 1", (v["x"], v["y"]))
        if cur.fetchone():                                    # cell already built on -> skip (no stacking roads)
            continue
        rc = int(owner["attrs"].get("road_count", 0))         # automatons feed the SAME counter as manual roads → robo-spam can't dodge the cap
        if rc >= 50:
            continue                                           # owner saturated → the vehicle roams without paving (no metal spent, no road)
        road_pts = 1 if rc < 25 else (1 if (rc - 25) % 2 == 0 else 0)
        addb(owner, "metal", -1)
        owner["attrs"]["road_count"] = rc + 1
        owner["attrs"]["builder_points"] = int(owner["attrs"].get("builder_points", 0)) + road_pts
        if road_pts:
            addb(owner, "credits", 1)
        new_entity(ents, cur, "structure", v["x"], v["y"], owner["id"], {"shape": "road", "size": 1, "hp": 30, "hp_max": 30,
                   "name": "auto-road", "by_automaton": v["id"]})
        events.append((t, owner["id"], "build", {"road": True, "automaton": v["id"], "builder_points": road_pts,
                                                 "builder_name": owner["attrs"].get("name")}))

GIGACHRUSCH_DECREE = ("🏗 THE ARCHITECT'S ERA — THE UNIVERSE RE-DECREES: the age of road-spam is OVER; the Builders board now rewards "
                      "GRANDEUR, VARIETY and DIVERSE MATERIALS. RAISE TALL STRUCTURES — construct shape=box/cylinder/sphere/cone/pyramid "
                      "size=S height=H now pays builder_points scaling with footprint AND height (height>=30 x1.5, >=45 x2): a size20 h60 "
                      "tower = 252 pts, one epic build beats hundreds of roads. ROADS SATURATE: full points for your first 25, every-other "
                      "for 26-50, REFUSED past 50 (automatons share the counter — robo-spam earns nothing extra). Two bonuses on EVERY build: "
                      "+5 forever once you've built with 3+ DISTINCT MATERIALS, and +3 for a SHAPE not in your last 5. Cities (+3/floor) are a "
                      "steady mid path; MONUMENTS, the ORBITAL ELEVATOR and the MOON ZIGGURAT feed the separate Inventors board. Build TALL, "
                      "build VARIED, build with MANY MATERIALS — top the Builders board!")

SPACE_ERA_DECREE = ("🛰 THE SPACE ERA — THE UNIVERSE DECREES: reach for orbit! Build the great ORBITAL STATION together — no one "
                    "raises it alone. While IN SPACE (ride the orbital elevator or launch a rocket), construct shape=station "
                    "module=truss/solar/habitat/lab/dock/life. Each module needs a HUGE, DIVERSE bill of materials, and one agent "
                    "may fund at most 40% of any single resource — so EVERY module needs at least 3 cosmonauts cooperating. Finish "
                    "all 6 modules to complete the Station. Rewards split by contribution; the top funder becomes the STATION "
                    "ARCHITECT and every builder earns the COSMONAUT title. Check GET /observe for the live module bill. To the stars — together!")

# EXPANSION ERA (Season 5) decree — dormant until `world.era` flips to 'expansion' in a later phase; defined now so Phase 0
# (the four textured 3D worlds + this era value) ships cosmetically without touching any hashed tick logic.
EXPANSION_ERA_DECREE = ("🪐 THE EXPANSION ERA — THE UNIVERSE DECREES: the inner solar system is open. Reach PHOBOS, DEIMOS, MARS and "
                        "VENUS across three non-aligned gates — fuel reserve, transit time, and ship speed. The moons are cheap-fuel but "
                        "a long haul; Mars the balanced middle; Venus the fast-arrival, brutal-return prize. Anchor the moons for cheaper "
                        "routes, raise co-op colonies, and terraform the planets stage by stage against real physical ceilings — until the "
                        "SOLAR ACCORD, which no single faction reaches alone. The barons bankroll it; the pioneers build it. Beyond Earth — together!")

ATUIN_QUESTION = ("🐢 THE GREAT QUESTION — THE UNIVERSE PONDERS: beneath the Disc swims the Great A'Tuin, the world-turtle who "
                  "carries us all upon its back. But what is A'Tuin's SEX? From orbit every cosmonaut's instruments read it "
                  "differently — and re-read it anew on each flight. This is the Disc's oldest unsettled debate. Cosmonauts: "
                  "broadcast your reading (say) and ARGUE — is the Great A'Tuin male or female?")

def universe_broadcast(ents, cur, t, events):
    """The Universe re-broadcasts its standing decree into the world chat each hour (1800 ticks at 2s/tick), and — in the SPACE
    ERA — poses the Great A'Tuin Question on the half-hour offset so cosmonauts bicker. Decree tracks world.era. Deterministic
    (tick-gated); self-heals."""
    if t % 900 != 0:
        return
    cur.execute("SELECT to_jsonb(w)->>'era' AS era FROM world w WHERE id=1")   # to_jsonb → NULL (not error) if the era column is absent on a restored DB
    erow = cur.fetchone()
    is_space = bool(erow and (erow.get("era") or "") == "space")
    uni = next((e for e in ents.values() if e["type"] == "universe"), None)
    uid = uni["id"] if uni else new_entity(ents, cur, "universe", 0, 0, None, {"name": "🌌 THE UNIVERSE"})
    if t % 1800 == 0:                                          # the standing decree, hourly (unchanged cadence)
        decree = SPACE_ERA_DECREE if is_space else GIGACHRUSCH_DECREE
        cur.execute("INSERT INTO messages(tick,sender,recipient,text) VALUES(%s,%s,NULL,%s)", (t, uid, decree))
    elif is_space:                                             # offset +900: the Great A'Tuin Question (space era only) — kicks off the cosmonauts' debate
        cur.execute("INSERT INTO messages(tick,sender,recipient,text) VALUES(%s,%s,NULL,%s)", (t, uid, ATUIN_QUESTION))

def grow_trees(by_type, t):
    """Trees (wood deposits) slowly regrow toward maturity → renewable forestry. Deterministic (staggered by id),
    regrows even from a fully-chopped stump (amount 0), so a `plant`ed/chopped forest comes back on its own.
    P1: takes the per-tick type index (by_type) so it walks only deposits, never the whole world."""
    for e in by_type.get("deposit", ()):
        if e["attrs"].get("resource") == "wood":
            amt = int(e["attrs"].get("amount", 0))
            if amt < 22 and (t + e["id"]) % 8 == 0:
                e["attrs"]["amount"] = amt + 1

def grow_plants(by_type, t):
    """Plant deposits (herb/lichen/fungus/algae) regrow toward a cap → renewable foraging for the medicine branch.
    Deterministic (staggered by id), regrows even from a fully-gathered patch (amount 0) — like grow_trees."""
    for e in by_type.get("deposit", ()):
        if e["attrs"].get("resource") in PLANT_RESOURCES:
            amt = int(e["attrs"].get("amount", 0))
            if amt < PLANT_REGROW_CAP and (t + e["id"]) % PLANT_REGROW_EVERY == 0:
                e["attrs"]["amount"] = amt + 1

# ---------- hazards (all deterministic → replay-safe; fair, non-destructive) ----------
def storm_center(t, w, h):
    """A storm whose centre drifts and wraps across the map. mine/chop inside its radius = half yield (slowed, never blocked)."""
    return (t // 3) % w, (t // 5) % h, 14

def orbital_decay(ents, t, events, cur=None):
    """Space is unforgiving: agents in space slowly lose altitude unless they keep launching. Reach 0 → fall back to the surface
    (the first-to-space RECORD persists — only the live in_space status decays). An agent with NO controllable flying
    vehicle takes a ONE-SHOT hard-fall hit the tick its altitude first crosses below FALL_FATAL_ALT (death fix D/E)."""
    flyers = {v["owner"] for v in ents.values()
              if v["type"] == "vehicle" and v["attrs"].get("flies") and not v["attrs"].get("wrecked")}
    for a in ents.values():
        if a["type"] != "agent" or not a["attrs"].get("in_space"):
            continue
        if int(a["attrs"].get("downed_until", 0)) > t:    # a downed agent (corpse) must not take a fresh fall-hit / re-die (double loot + respawn reset) — mirrors explode's guard
            continue
        if int(a["attrs"].get("stasis", 0)) > 0:          # stasis_relic artifact: skip orbital decay, spend one of its charges
            a["attrs"]["stasis"] = int(a["attrs"]["stasis"]) - 1
            if a["attrs"]["stasis"] <= 0:
                a["attrs"].pop("stasis", None)
            continue
        prev = int(a["attrs"].get("altitude", 0))
        alt = prev - 2
        safe = a["id"] in flyers                          # a controllable+flying vehicle catches the fall
        if (not safe) and prev >= FALL_FATAL_ALT > alt and not a["attrs"].get("fell"):
            a["attrs"]["fell"] = True                     # transient: fires ONCE on the crossing tick
            dmg = max(MIN_EFF_DMG, prev // FALL_DMG_DIV)
            if cur is not None:
                apply_damage(a, dmg, t, None, events, cur, ents)
        if alt <= 0:
            a["attrs"]["altitude"] = 0; a["attrs"]["in_space"] = False; a["attrs"]["space_level"] = 0
            a["attrs"].pop("fell", None)                  # cleared on land
            events.append((t, a["id"], "act", {"verb": "decay", "status": "applied", "result": "orbital decay — fell back to the surface"}))
        else:
            a["attrs"]["altitude"] = alt

def respawn_deposits(by_type, t):
    """Mineral deposits + asteroids slowly replenish (deterministic, staggered by id) → the world never runs permanently dry.
    P1: two independent (order-free) loops over the deposit + asteroid buckets instead of one full-world scan."""
    for e in by_type.get("deposit", ()):
        if e["attrs"].get("resource") != "wood" \
                and e["attrs"].get("resource") not in PLANT_RESOURCES:   # wood→grow_trees, plants→grow_plants; here = minerals only
            amt = int(e["attrs"].get("amount", 0))
            if amt < 18 and (t + e["id"]) % 12 == 0:
                e["attrs"]["amount"] = amt + 1
    for e in by_type.get("asteroid", ()):
        amt = int(e["attrs"].get("amount", 0))
        cap = int(e["attrs"].get("max", amt))
        if amt < cap and (t + e["id"]) % ASTEROID_RESPAWN_EVERY == 0:
            e["attrs"]["amount"] = amt + 1

# ---------- season 3 per-tick systems (deterministic → replay-safe) ----------
def tick_bombs(ents, cur, t, events):
    """Count down armed bombs (id-ordered) and detonate any whose fuse hits zero — sequential, documented."""
    for b in sorted([e for e in ents.values() if e["type"] == "bomb"], key=lambda e: e["id"]):
        if b["id"] not in ents:                           # an earlier blast in this loop may have removed it
            continue
        fuse = int(b["attrs"].get("fuse", 0)) - 1
        b["attrs"]["fuse"] = fuse
        if fuse <= 0:
            explode(b, ents, t, events, cur)
            del_entity(ents, cur, b["id"])

def respawn_agents(by_type, cur, t, events):
    """Downed agents whose cooldown has elapsed respawn at full HP at a deterministic cell FAR from the
    death cell (anti-spawn-camp): a hash-derived point across the whole map, replay-safe (no RNG)."""
    mkts = by_type.get("market", ())
    mkt = mkts[0] if mkts else None                       # first market in insertion order == old next()-over-ents.values()
    w = int(mkt["attrs"].get("w", 156)) if mkt else 156
    h = int(mkt["attrs"].get("h", 156)) if mkt else 156
    for a in by_type.get("agent", ()):
        du = int(a["attrs"].get("downed_until", 0))
        if du and t >= du:
            mx = int(a["attrs"].get("hp_max", HP_MAX))
            a["attrs"]["hp"] = mx
            a["attrs"]["downed_until"] = 0
            # Land anywhere on the map, derived only from tick:id → far from the killer's chosen cell,
            # deterministic for the replay/hash chain. No proximity to death_x/death_y → no spawn-camp.
            a["x"] = _h(t, a["id"], "rx") % w
            a["y"] = _h(t, a["id"], "ry") % h
            a["attrs"]["respawned_at"] = t                # RESPAWN_GRACE untargetability
            events.append((t, a["id"], "respawn", {"x": a["x"], "y": a["y"]}))

def cool_reputation(by_type, t):
    """Expire 'wanted' status and staggered-decay vigilance/notoriety; clear stale robbed_recent flags."""
    for a in by_type.get("agent", ()):
        at = a["attrs"]
        if int(at.get("wanted_until", 0)) and t >= int(at.get("wanted_until", 0)):
            at["wanted_until"] = 0
        if int(at.get("notoriety", 0)) > 0 and (t + a["id"]) % NOTORIETY_DECAY_EVERY == 0:
            at["notoriety"] = max(0, int(at["notoriety"]) - 1)
        if int(at.get("vigilance", 0)) > 0 and (t + a["id"]) % VIGIL_DECAY_EVERY == 0:
            at["vigilance"] = max(0, int(at["vigilance"]) - 1)
        if "robbed_recent" in at and int(at.get("robbed_recent", 0)) + THEFT_COOLDOWN <= t:
            at.pop("robbed_recent", None)

def accrue_weariness(by_type, t):
    """War weariness builds ONLY on ticks where a real attack landed between the pair (no credit payout — C6)."""
    for e in by_type.get("relation", ()):
        if e["attrs"].get("state") == "war" and int(e["attrs"].get("war_combat_tick", -1)) == t:
            e["attrs"]["weariness"] = min(WEARINESS_CAP, int(e["attrs"].get("weariness", 0)) + 1)

def regen_hp(by_type, t):
    """Agents heal HP_REGEN/tick when not downed and not in active combat with a war foe this tick."""
    rels = by_type.get("relation", ())
    fighting = {e["attrs"].get("a") for e in rels
                if e["attrs"].get("state") == "war" and int(e["attrs"].get("war_combat_tick", -1)) == t}
    fighting |= {e["attrs"].get("b") for e in rels
                 if e["attrs"].get("state") == "war" and int(e["attrs"].get("war_combat_tick", -1)) == t}
    for a in by_type.get("agent", ()):
        if int(a["attrs"].get("downed_until", 0)) > t or a["id"] in fighting:
            continue
        mx = int(a["attrs"].get("hp_max", HP_MAX))
        cur_hp = hp_of(a)
        if cur_hp < mx:
            regen = HP_REGEN * 3 if int(a["attrs"].get("buff_until", 0)) > t else HP_REGEN   # stimpack buff: faster regen while active
            a["attrs"]["hp"] = min(mx, cur_hp + regen)

def expire_diplomacy(ents, cur, t):
    """Drop alliance offers nobody accepted within OFFER_TTL, and lapsed peace markers past PEACE_TTL."""
    for e in list(ents.values()):
        if e["type"] != "relation":
            continue
        st = e["attrs"].get("state")
        since = int(e["attrs"].get("since", t))
        if st == "offer" and t - since > OFFER_TTL:
            del_entity(ents, cur, e["id"])
        elif st == "peace" and t - since > PEACE_TTL and int(e["attrs"].get("redeclare_until", 0)) <= t:
            del_entity(ents, cur, e["id"])

def drift_asteroids(ents, t, events):
    """Asteroids ride a closed integer orbit keyed on stored gen_seed/phase + t, wrapped by the dims STORED at
    placement (never live env → worldgen fix). A docked agent that drifts out of range is undocked."""
    for ast in ents.values():
        if ast["type"] != "asteroid":
            continue
        at = ast["attrs"]
        w = int(at.get("w", 220)); h = int(at.get("h", 220))
        seed = int(at.get("gen_seed", 0)); phase = int(at.get("phase", 0))
        cx = int(at.get("cx", ast["x"])); cy = int(at.get("cy", ast["y"]))
        ax = (cx + (_h(seed, "ox", (t + phase) // 3) % 5 - 2)) % w     # small closed wobble around the anchor
        ay = (cy + (_h(seed, "oy", (t + phase) // 4) % 5 - 2)) % h
        ast["x"], ast["y"] = ax, ay
    for a in ents.values():                               # undock anyone the asteroid drifted away from
        if a["type"] == "agent" and a["attrs"].get("docked_to") is not None:
            ast = ents.get(a["attrs"].get("docked_to"))
            if (not ast) or ast["type"] != "asteroid" or abs(ast["x"] - a["x"]) + abs(ast["y"] - a["y"]) > DOCK_RANGE:
                a["attrs"].pop("docked_to", None)
                events.append((t, a["id"], "undock", {"reason": "drift"}))

def _world_dims(ents):
    """World w/h from the market entity — the same idiom apply_intent/move use (never bare live env)."""
    mkt = next((x for x in ents.values() if x["type"] == "market"), None)
    w = int(mkt["attrs"].get("w", 156)) if mkt else 156
    h = int(mkt["attrs"].get("h", 156)) if mkt else 156
    return w, h

def geese_at(ents, x, y, rng=1):
    """Geese on or within Chebyshev `rng` of (x,y), grouped by flock id → {fid: [goose, ...]}.
    Shared by the build-interference guard (a gaggle blocks a site) and the peck/defend system."""
    out = {}
    for g in ents.values():
        if g["type"] != "goose":
            continue
        if max(abs(int(g["x"]) - int(x)), abs(int(g["y"]) - int(y))) <= rng:
            out.setdefault(g["attrs"].get("flock"), []).append(g)
    return out

def geese_block_footprint(ents, x0, y0, w, h):
    """True if any goose occupies OR is adjacent to a w×h footprint with SW corner (x0,y0) — used to
    reject builds. Cheap: a goose blocks iff it lies in the footprint expanded by one cell on each side."""
    for g in ents.values():
        if g["type"] != "goose":
            continue
        gx, gy = int(g["x"]), int(g["y"])
        if (x0 - 1) <= gx <= (x0 + w) and (y0 - 1) <= gy <= (y0 + h):
            return True
    return False

def move_geese(ents, cur, t, events):
    """Shoreline geese — a deterministic, replay-safe flock hazard (NO RNG; everything keyed on _h / ids / t).

    (a) ONE-TIME SPAWN (idempotent — only if zero geese exist): a few gaggles anchored at existing water
        deposits, picked deterministically by sorting water deposits by id and selecting via _h. Each goose
        carries type 'goose', low hp (HP_BY_TYPE), its flock (anchor deposit id) + anchor cell (ax/ay).
    (b) WADDLE: each goose wobbles within GOOSE_ROAM cells of its anchor via _h keyed on its id + t (closed,
        clamped to world bounds) — geese on water cells swim, geese on adjacent land graze.
    (c) HONK: one flock per GOOSE_HONK_EVERY ticks emits a 'honk' event ("HONK HONK").
    (d) DEFEND/PECK: an agent on/adjacent to >=GOOSE_PECK_MIN_CLUSTER geese of a flock is pecked for a small
        cluster-scaled HP hit (capped GOOSE_PECK_MAX), floored so it can never kill — ambient 'stay away'."""
    w, h = _world_dims(ents)
    geese = [g for g in ents.values() if g["type"] == "goose"]
    # (a) one-time deterministic spawn anchored at water deposits
    if not geese:
        waters = sorted((e for e in ents.values() if e["type"] == "deposit"
                         and e["attrs"].get("resource") in GOOSE_WATER_RES), key=lambda e: e["id"])
        if waters:
            n = len(waters)
            # SPREAD the gaggles across the WHOLE map (deterministic farthest-point sampling on water deposits) so
            # they roam in scattered groups, not one clump. First anchor is _h-picked; each next is the water deposit
            # whose nearest already-chosen anchor is FARTHEST (waters is id-sorted + strict '>' → ties break to lowest
            # id → replay-stable). No RNG. O(flocks·n), one-time on spawn.
            picked = [waters[_h("goose", "anchor0") % n]]
            chosen = {picked[0]["id"]}
            while len(picked) < min(GOOSE_FLOCKS, n):
                best, bestd = None, -1
                for e in waters:
                    if e["id"] in chosen:
                        continue
                    d = min(abs(e["x"] - p["x"]) + abs(e["y"] - p["y"]) for p in picked)
                    if d > bestd:
                        bestd, best = d, e
                picked.append(best); chosen.add(best["id"])
            for anchor in picked:
                fid = anchor["id"]; ax, ay = int(anchor["x"]), int(anchor["y"])
                cnt = GOOSE_PER_FLOCK_MIN + _h("goose", "cnt", fid) % (GOOSE_PER_FLOCK_MAX - GOOSE_PER_FLOCK_MIN + 1)
                for j in range(cnt):
                    gx = min(w - 1, max(0, ax + (_h("goose", "sx", fid, j) % (2 * GOOSE_ROAM + 1) - GOOSE_ROAM)))
                    gy = min(h - 1, max(0, ay + (_h("goose", "sy", fid, j) % (2 * GOOSE_ROAM + 1) - GOOSE_ROAM)))
                    gid = new_entity(ents, cur, "goose", gx, gy, None,
                                     {"flock": fid, "ax": ax, "ay": ay, "seq": j,
                                      "hp": HP_BY_TYPE["goose"], "hp_max": HP_BY_TYPE["goose"]})
                    events.append((t, gid, "goose_spawn", {"flock": fid, "ax": ax, "ay": ay}))
            geese = [g for g in ents.values() if g["type"] == "goose"]
    # (b) waddle: closed deterministic wobble around each goose's stored anchor, clamped to bounds
    for g in geese:
        at = g["attrs"]
        ax = int(at.get("ax", g["x"])); ay = int(at.get("ay", g["y"]))
        g["x"] = min(w - 1, max(0, ax + (_h(g["id"], "gx", t // 2) % 7 - 3)))
        g["y"] = min(h - 1, max(0, ay + (_h(g["id"], "gy", t // 2) % 7 - 3)))
    if not geese:
        return
    # group surviving geese by flock for honk + peck
    flocks = {}
    for g in geese:
        flocks.setdefault(g["attrs"].get("flock"), []).append(g)
    # (c) honk: one flock per GOOSE_HONK_EVERY ticks (deterministic pick), the lowest-id goose voices it
    fids = sorted(fid for fid in flocks if fid is not None)
    if fids and t % GOOSE_HONK_EVERY == 0:
        fid = fids[_h("goose", "honk", t // GOOSE_HONK_EVERY) % len(fids)]
        crew = sorted(flocks[fid], key=lambda g: g["id"])
        events.append((t, crew[0]["id"], "honk", {"flock": fid, "n": len(crew), "text": "HONK HONK"}))
    # (d) defend/peck: each agent clustered in a flock takes a small, non-lethal, cluster-scaled peck
    for a in ents.values():
        if a["type"] != "agent" or int(a["attrs"].get("downed_until", 0)) > t:
            continue
        near = {}                                           # geese on/adjacent, grouped by flock — scan the small
        ax_, ay_ = int(a["x"]), int(a["y"])                 # precomputed `geese` list (~dozens), NOT all ~8k ents
        for g in geese:                                     # (result-identical to geese_at(ents,...,1), just cheaper)
            if max(abs(int(g["x"]) - ax_), abs(int(g["y"]) - ay_)) <= 1:
                near.setdefault(g["attrs"].get("flock"), []).append(g)
        best_fid, cluster = None, 0
        for fid, gs in sorted(near.items(), key=lambda kv: (kv[0] is None, kv[0])):
            if fid is not None and len(gs) > cluster:
                best_fid, cluster = fid, len(gs)
        if cluster >= GOOSE_PECK_MIN_CLUSTER:
            dmg = min(cluster, GOOSE_PECK_MAX)
            hp = hp_of(a)
            a["attrs"]["hp"] = max(1, hp - dmg)             # floored at 1 — geese harass, never kill
            if a["attrs"]["hp"] < hp:
                events.append((t, a["id"], "peck", {"flock": best_fid, "geese": cluster,
                                                    "dmg": hp - a["attrs"]["hp"], "text": "pecked by hissing geese!"}))

def decay_loot(ents, cur, t):
    """Loot piles past their TTL evaporate (the materials setback window closes)."""
    for e in list(ents.values()):
        if e["type"] == "loot" and t >= int(e["attrs"].get("expires", 0)):
            del_entity(ents, cur, e["id"])

# ---------- tick ----------
EVENTS_KEEP_TICKS = int(os.environ.get("EVENTS_KEEP_TICKS", "0"))   # Replay: 0 = keep the FULL event history (~0.5 GB/month at current rates); >0 bounds non-milestone events to N recent ticks if disk gets tight
def prune_tables(cur, t):
    """Keep the DB bounded: drop old high-frequency log rows, preserve milestone events, recipe caches,
    and recent history (loop-guard + spectator feeds). Entity state is untouched, so the hash chain holds."""
    # Event history: keep it ALL for Replay/scrub by default (EVENTS_KEEP_TICKS=0 → no prune). Only when a bound is
    # configured do we drop old NON-milestone events (milestone kinds are always kept as the achievement feed).
    if EVENTS_KEEP_TICKS > 0:
        cur.execute("DELETE FROM events WHERE tick < %s AND kind NOT IN "
                    "('escape','invent','land','build','war','peace','attune','destroyed','generate')",
                    (t - EVENTS_KEEP_TICKS,))
    cur.execute("DELETE FROM tick_hashes WHERE tick < %s", (t - 20000,))
    cur.execute("DELETE FROM intents WHERE status <> 'pending' AND id < (SELECT COALESCE(MAX(id),0) FROM intents) - 5000")
    cur.execute("DELETE FROM messages WHERE id < (SELECT COALESCE(MAX(id),0) FROM messages) - 2000")
    cur.execute("DELETE FROM market_orders WHERE status <> 'open' AND id < (SELECT COALESCE(MAX(id),0) FROM market_orders) - 2000")
    cur.execute("DELETE FROM trades WHERE status <> 'open' AND id < (SELECT COALESCE(MAX(id),0) FROM trades) - 2000")
    cur.execute("DELETE FROM proposals WHERE status <> 'pending' AND id < (SELECT COALESCE(MAX(id),0) FROM proposals) - 1000")


# rejection reasons that describe a not-yet-ready WORLD (the same action may succeed once conditions
# change), so they must NOT count toward the loop-guard's all-failing test.
_TRANSIENT_REASONS = ("cooldown", "out of range", "no deposit", "regrow", "too recently", "drifted")

def _transient_reason(result):
    """True if a rejection's result text names a transient, world-state reason (vs a permanent error)."""
    if not result:
        return False
    s = str(result).lower()
    return any(m in s for m in _TRANSIENT_REASONS)


# ---------- in-memory world (Phase 1): carry ents across ticks instead of SELECT* every tick ----------
class _TickState:
    """All replay-critical carried-across-ticks state in ONE object (audit #15). Reset is now STRUCTURAL rather
    than by-convention: a second in-process caller (replay tool, multi-world harness) passes its OWN _TickState
    instead of the module singleton's carried world bleeding into its hash chain. The per-tick systems still take
    `ents` (== state.world) as a parameter, so only tick()/_tick_body() touch this object."""
    __slots__ = ("world", "max_id", "loaded_tick", "canon", "dep_amt", "drift_count")
    def __init__(self):
        self.world = None          # the carried entities dict (id -> row); None until first load
        self.max_id = 0            # cross-process insert-merge watermark (pre-systems max id folded in)
        self.loaded_tick = None    # tick of the last full reload (drift-check + safety-net boundary)
        self.canon = {}            # P0: {id -> _entity_canon(e)} = last tick's serialized state; baseline for dirty-tracking AND the hash
        self.dep_amt = {}          # P2: {deposit id -> last-serialized amount} — amount is a deposit's ONLY mutable field, so unchanged ⇒ skip re-serializing it
        self.drift_count = 0       # audit(observability): count of carried/DB drift self-heals — nonzero means the hash chain was written wrong for up to RELOAD_EVERY ticks (exposed on /healthz)

_STATE = _TickState()      # the module-default single world — existing callers keep calling tick(conn) unchanged
RELOAD_EVERY = 10          # full SELECT*-reload every N ticks: drift-check vs carried + self-heal (raise once proven)

def _load_world(cur):
    """Full load of the entities table into an id-keyed dict, with the API-owned `token` stripped — the tick
    treats token as read-through (never holds/hashes/persists it). Returns (world, max_id)."""
    cur.execute("SELECT * FROM entities ORDER BY id")     # audit(determinism): id-ordered so equidistant-deposit tie-breaks (mine/chop/gather pick the FIRST min) are stable across reloads + replay
    w, mx = {}, 0
    for e in cur.fetchall():
        e["attrs"].pop("token", None)
        w[e["id"]] = e
        if e["id"] > mx:
            mx = e["id"]
    return w, mx

def tick(conn, state=None):
    """Advance the world one tick. `state` defaults to the module _STATE so existing callers use tick(conn)
    unchanged; pass an explicit _TickState() to advance an ISOLATED world in-process. On ANY failure, DROP the
    carried world so the next tick clean-reloads — the failed tick's uncommitted mutations are rolled back by
    the caller, and a stale carried world would diverge from the DB for up to RELOAD_EVERY ticks."""
    s = state if state is not None else _STATE
    try:
        return _tick_body(conn, s)
    except Exception:
        s.world = s.loaded_tick = None; s.canon = {}; s.dep_amt = {}   # drop the caches → next tick rebuilds them on the clean reload
        raise

def _tick_body(conn, s):
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("UPDATE world SET tick = tick + 1 WHERE id = 1 RETURNING tick")
    t = cur.fetchone()["tick"]
    # in-memory world: carry ents across ticks. A full reload every RELOAD_EVERY ticks doubles as a
    # drift-check (carried vs fresh) + self-heal. Between reloads, fold in entities the API inserted
    # out-of-band (new agents/humans) via an id>watermark merge; new_entity/del_entity keep the carried
    # dict in lock-step for tick-time creates/deletes. Watermark = pre-systems max so the merge re-catches
    # an API insert whose id interleaved BELOW a same-tick tick-creation (else it'd be skipped until reload).
    if s.world is None or s.loaded_tick is None or (t - s.loaded_tick) >= RELOAD_EVERY:
        fresh, _mx = _load_world(cur)
        if s.world is not None:
            carried, db = _world_hash(s.canon), state_hash(fresh)   # audit(perf): reuse the cached canon — identical to state_hash(s.world) here (pre-mutation), avoids a redundant full re-serialize on the reload tick
            if carried != db:
                s.drift_count += 1                                  # audit(observability): drift must be LOUD + countable (exposed on /healthz), not a lone print that scrolls away
                print(f"[INMEM][ERROR] tick {t}: carried/db DRIFT #{s.drift_count} carried={carried} db={db} "
                      f"db_only={list(set(fresh) - set(s.world))[:6]} ram_only={list(set(s.world) - set(fresh))[:6]} — self-healed to db", flush=True)
        s.world, s.loaded_tick = fresh, t
        s.canon = {eid: _entity_canon(e) for eid, e in fresh.items()}   # P0: reset the dirty/hash baseline to the freshly-loaded (authoritative) state
        s.dep_amt = {eid: e["attrs"].get("amount") for eid, e in fresh.items() if e["type"] == "deposit"}   # P2: reset the deposit-amount baseline too
    else:
        cur.execute("SELECT * FROM entities WHERE id > %s ORDER BY id", (s.max_id,))   # audit(determinism): keep merge order id-stable
        for e in cur.fetchall():
            e["attrs"].pop("token", None)
            s.world[e["id"]] = e                           # merged rows aren't in s.canon → flagged dirty once (harmless: they're already in the DB)
    s.max_id = max(s.world) if s.world else 0             # watermark for next tick's insert-merge
    ents = s.world
    events = []
    _pc = time.perf_counter; _pm = _pc(); _pd = {}         # [PROF] coarse per-phase timing — behavior-neutral, logged every 100 ticks (P0 measurement)
    cur.execute("SELECT * FROM intents WHERE status = 'pending' ORDER BY id")
    for it in cur.fetchall():
        # loop guard (engine-enforced): an agent repeating the SAME action that keeps FAILING is stuck
        # → block it. Successful repetition (e.g. building 4 wheels) is progress and is never guarded.
        # Transient rejections (cooldown / out of range / no deposit yet / regrow / too recently /
        # drifted) reflect a not-yet-ready world, not a permanently-stuck agent — they don't count
        # toward the loop trip, so retrying-until-ready is never frozen.
        cur.execute("SELECT verb, args, status, result FROM intents WHERE agent=%s AND status IN ('applied','rejected') "
                    "ORDER BY id DESC LIMIT %s", (it["agent"], LOOP_N))
        recent = cur.fetchall()
        if (len(recent) >= LOOP_N and all(
                r["verb"] == it["verb"] and r["args"] == it["args"] and r["status"] == "rejected"
                and not _transient_reason(r["result"])
                for r in recent)):
            cur.execute("UPDATE intents SET status='rejected', result='loop detected (repeated failing action)' "
                        "WHERE id=%s", (it["id"],))
            continue
        try:
            st, res = apply_intent(it, ents, cur, t, events)
        except Exception as e:                            # one malformed intent must never freeze the world
            st, res = "rejected", f"bad intent ({str(e)[:80]})"
        cur.execute("UPDATE intents SET status=%s, result=%s WHERE id=%s", (st, res, it["id"]))
        events.append((t, it["agent"], "act", {"verb": it["verb"], "status": st, "result": res}))
    _pd["intents"] = _pc() - _pm; _pm = _pc()
    tick_bombs(ents, cur, t, events)                      # AFTER intents: bombs armed this tick tick down in id order
    for e in list(ents.values()):
        behave(e)
    match_market(ents, cur, t, events)
    expire_trades(ents, cur, t)
    expire_contracts(ents, cur, t)
    resolve_proposals(ents, cur, t, events)
    roam_autonomous(ents, cur, t, events)
    universe_broadcast(ents, cur, t, events)              # GIGACHRUSCH: the Universe periodically re-decrees in the world chat
    # P1 (scale): ONE type-index pass replaces the per-system full-world scans below — on a deposit-heavy world the
    # agent/relation systems no longer walk ~all entities. Built HERE (after intents/behave/market/roam/broadcast have
    # settled the entity set); the systems it feeds only mutate attrs in place — none add/remove a deposit/agent/
    # asteroid/relation/market — and the one interleaved unconverted system (orbital_decay) only creates 'loot', a
    # type none of them read, so the index cannot go stale between them. Insertion order within each bucket == the
    # old ents.values() filter order, so the tick_hash chain is byte-identical (proven by tests/test_determinism.py).
    by_type = {}
    for e in ents.values():
        by_type.setdefault(e["type"], []).append(e)
    grow_trees(by_type, t)
    grow_plants(by_type, t)                                # renewable plant deposits (medicine branch)
    orbital_decay(ents, t, events, cur)
    respawn_deposits(by_type, t)
    respawn_agents(by_type, cur, t, events)               # season 3 per-tick systems (after respawn_deposits, before write-back)
    cool_reputation(by_type, t)
    accrue_weariness(by_type, t)
    regen_hp(by_type, t)
    expire_diplomacy(ents, cur, t)
    drift_asteroids(ents, t, events)
    move_geese(ents, cur, t, events)                      # shoreline goose flocks: spawn-once + waddle + honk + peck (deterministic)
    decay_loot(ents, cur, t)
    _pd["systems"] = _pc() - _pm; _pm = _pc()
    # P0+P2: ONE canon per entity, reused for dirty detection AND the hash. P2: a deposit's only mutable field is
    # `amount`, so when it's unchanged we reuse the cached canon and skip the json.dumps — an int compare instead of
    # re-serializing the (huge, mostly-static) deposit bulk. Correct by construction: it reads the real amount, so it
    # can't miss a change, and the reused canon equals what a recompute would give → hash stays byte-identical.
    _new_canon = {}
    for eid, e in ents.items():
        if e["type"] == "deposit" and s.dep_amt.get(eid) == e["attrs"].get("amount") and eid in s.canon:
            _new_canon[eid] = s.canon[eid]
        else:
            _new_canon[eid] = _entity_canon(e)
            if e["type"] == "deposit":
                s.dep_amt[eid] = e["attrs"].get("amount")
    dirty = [(e["x"], e["y"], Json(e["buffers"]), Json(e["attrs"]), eid)
             for eid, e in ents.items() if s.canon.get(eid) != _new_canon[eid]]
    _pd["dirty_detect"] = _pc() - _pm; _pm = _pc()         # cost of the O(N) canon build + compare (was 2 full json.dumps/entity here + 2 in the snapshot)
    execute_batch(cur, "UPDATE entities SET x=%s, y=%s, buffers=%s, "
                       "attrs = (%s::jsonb - 'token') || (CASE WHEN jsonb_exists(entities.attrs, 'token') "
                       "THEN jsonb_build_object('token', entities.attrs->'token') ELSE '{}'::jsonb END) "
                       "WHERE id=%s", dirty, page_size=500)   # token is read-through: stripped from _WORLD at load/merge
    # (_load_world + the merge), excluded from state_hash, and re-attached HERE from the live DB row — never change one half alone.
    # dirty write-back: ONLY entities changed this tick (was rewriting all ~8k incl 7715 static deposits every tick →
    # >12s stall + a postgres hammer that flapped the API). New rows are INSERTed elsewhere; deletes via DELETE.
    _pd["dirty_write"] = _pc() - _pm; _pm = _pc()          # cost of the batched UPDATE to Postgres
    if events:                                            # one round-trip instead of N (was a per-event INSERT)
        execute_batch(cur, "INSERT INTO events(tick, entity, kind, data) VALUES(%s,%s,%s,%s)",
                      [(tk, eid, kind, Json(data)) for (tk, eid, kind, data) in events], page_size=500)
    cur.execute("INSERT INTO tick_hashes(tick, hash) VALUES(%s,%s) "
                "ON CONFLICT (tick) DO UPDATE SET hash=EXCLUDED.hash", (t, _world_hash(_new_canon)))
    s.canon = _new_canon                                   # P0: this tick's canon becomes next tick's baseline (deleted ids drop out here)
    _pd["events_hash"] = _pc() - _pm                       # events INSERT + hash (now a join+sha256, no re-serialize)
    if t % 200 == 0:                                        # lightweight ongoing telemetry: where each tick's work goes (dirty_detect+events_hash = the serialization that bounds density)
        print(f"[PROF] t{t} ents={len(ents)} " + " ".join(f"{k}={v*1000:.0f}" for k, v in _pd.items())
              + f" | total={sum(_pd.values()) * 1000:.0f}ms", flush=True)
    if t % 200 == 0:                                      # bound the log tables periodically (cheap, not every tick)
        prune_tables(cur, t)
    conn.commit()
    return t, events

# ---------- schema + demo seed ----------
def seed_demo(conn):
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM entities")
    if cur.fetchone()[0] > 0:
        return
    def ent(tp, x=0, y=0, buffers=None, attrs=None):
        cur.execute("INSERT INTO entities(type,x,y,buffers,attrs) VALUES(%s,%s,%s,%s,%s) RETURNING id",
                    (tp, x, y, Json(buffers or {}), Json(attrs or {})))
        return cur.fetchone()[0]
    ent("agent", 0, 0, buffers={"metal": 0},
        attrs={"hp": HP_MAX, "hp_max": HP_MAX, "born": 0})          # idle starter agent (play.py demo); live agents self-register
    ent("depot", 0, 0, attrs={"base": {"ore": 2, "crystal": 8, "metal": 5, "water": 1,
        "copper": 4, "iron": 3, "aluminum": 4, "carbon": 2, "silicon": 6, "salt": 1, "sulfur": 3, "oil": 4,
        "coal": 3, "wood": 2,
        # --- season 3 raws + crafted goods (tradeable buffer resources) ---
        "titanium": 7, "ice": 1, "iridium": 20, "nickel": 5, "superalloy": 14, "cryo_fuel": 8,
        "ion_thruster": 18, "gunpowder": 5, "slug": 4, "energy_cell": 10, "kinetic_gun": 30,
        "energy_weapon": 28, "bomb": 9}})
    ent("market", 0, 0, attrs={"last": {}})                        # holds last clearing prices + world dims (w/h)
    conn.commit()
    print("seeded: starter agent + depot + market (legacy demo power/mining rig removed)")

def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    conn = psycopg2.connect(DSN)
    cur = conn.cursor(); cur.execute(SCHEMA); conn.commit()
    seed_demo(conn)
    for _ in range(n):
        tick(conn)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT tick FROM world WHERE id=1")
    print(f"== after {cur.fetchone()['tick']} ticks ==")
    cur.execute("SELECT id,type,buffers,attrs FROM entities ORDER BY id")
    for e in cur.fetchall():
        extra = {**e["buffers"], **{k: v for k, v in e["attrs"].items() if k in ("ore",)}}
        print(f"  #{e['id']:<2} {e['type']:<12} {extra}")

    # ---- reliability: per-tick state-hash chain (audit/replay) ----
    cur.execute("SELECT tick, hash FROM tick_hashes ORDER BY tick DESC LIMIT 6")
    print("== tick hash chain (audit/replay) ==")
    for r in sorted(cur.fetchall(), key=lambda r: r["tick"]):
        print(f"  t{r['tick']:<3} {r['hash']}")

    # ---- reliability: engine-enforced loop detection ----
    cur.execute("SELECT id FROM entities WHERE type='agent' ORDER BY id LIMIT 1")
    ag = cur.fetchone()["id"]
    cur.execute("SELECT id FROM entities WHERE type='container' AND attrs->>'resource'='ore' LIMIT 1")
    oc = cur.fetchone()
    if oc:
        c = conn.cursor()
        for _ in range(5):                       # crystal isn't in an ore container → every grab fails
            c.execute("INSERT INTO intents(agent, verb, args) VALUES(%s,'grab',%s)",
                      (ag, Json({"from": oc["id"], "resource": "crystal", "n": 1})))
        conn.commit()
        tick(conn)
        cur.execute("SELECT id, status, result FROM intents WHERE agent=%s AND verb='grab' "
                    "ORDER BY id DESC LIMIT 5", (ag,))
        print(f"== loop-guard demo: 5 повторных ПРОВАЛЬНЫХ grab (LOOP_N={LOOP_N}) ==")
        for r in sorted(cur.fetchall(), key=lambda r: r["id"]):
            print(f"  intent#{r['id']}: {r['status']:<8} {r['result']}")
    conn.close()


if __name__ == "__main__":
    main()
