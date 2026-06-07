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
import os, sys, json, hashlib
import psycopg2
from psycopg2.extras import RealDictCursor, Json
import vehicles   # PART / BUILD_COST / finalize() — for the build & finalize intents
import crafting   # PROPS / RULES / combine() — emergent physics crafting

DSN = os.environ.get("PG_DSN", "host=127.0.0.1 dbname=nhamoo user=postgres")
LOOP_N = 3   # engine-enforced: reject an intent identical to the agent's last LOOP_N applied ones
GRAVITY = 4              # a vehicle lifts off only if thrust >= GRAVITY * mass (the grand-goal gate)
ATMOSPHERE_TOP = 100     # altitude that counts as "escaped the atmosphere" — first space milestone
CLIMB = 10               # altitude gained per fueled launch
SPACE_TIERS = [(100, "space"), (300, "orbit"), (600, "the Moon")]   # altitude → milestone (escalating goals beyond escape)
SKY_TOP = 600            # max altitude — reaching the Moon
DESCEND = 40             # altitude shed per `land` (controlled descent back home)

SCHEMA = """
CREATE TABLE IF NOT EXISTS world (id int PRIMARY KEY DEFAULT 1, tick int NOT NULL DEFAULT 0);
INSERT INTO world (id, tick) VALUES (1,0) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS entities (id bigserial PRIMARY KEY, type text NOT NULL,
  x int NOT NULL DEFAULT 0, y int NOT NULL DEFAULT 0, owner bigint,
  buffers jsonb NOT NULL DEFAULT '{}', attrs jsonb NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS ports (id bigserial PRIMARY KEY,
  entity bigint NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  name text NOT NULL, kind text NOT NULL, dir text NOT NULL);
CREATE INDEX IF NOT EXISTS ports_entity_idx ON ports(entity);
CREATE TABLE IF NOT EXISTS links (id bigserial PRIMARY KEY,
  a bigint NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
  b bigint NOT NULL REFERENCES ports(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS intents (id bigserial PRIMARY KEY, agent bigint NOT NULL,
  verb text NOT NULL, args jsonb NOT NULL DEFAULT '{}', status text NOT NULL DEFAULT 'pending',
  result text, created int);
CREATE TABLE IF NOT EXISTS events (id bigserial PRIMARY KEY, tick int NOT NULL,
  entity bigint, kind text NOT NULL, data jsonb NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS tick_hashes (tick int PRIMARY KEY, hash text NOT NULL);
CREATE TABLE IF NOT EXISTS market_orders (id bigserial PRIMARY KEY, agent bigint NOT NULL,
  side text NOT NULL, resource text NOT NULL, qty int NOT NULL, price int NOT NULL,
  status text NOT NULL DEFAULT 'open', created int);
CREATE INDEX IF NOT EXISTS market_open_idx ON market_orders(resource, side, status);
CREATE TABLE IF NOT EXISTS trades (id bigserial PRIMARY KEY, proposer bigint NOT NULL,
  target bigint NOT NULL, give jsonb NOT NULL, want jsonb NOT NULL,
  status text NOT NULL DEFAULT 'open', created int);
CREATE TABLE IF NOT EXISTS messages (id bigserial PRIMARY KEY, tick int NOT NULL,
  sender bigint NOT NULL, recipient bigint, text text NOT NULL);
CREATE TABLE IF NOT EXISTS discoveries (rule_key text PRIMARY KEY, name text NOT NULL,
  discoverer bigint NOT NULL, tick int NOT NULL, points int NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS proposals (id bigserial PRIMARY KEY, agent bigint NOT NULL,
  ings jsonb NOT NULL, sig text NOT NULL, proposed_name text,
  status text NOT NULL DEFAULT 'pending', item_key text, item_name text, props jsonb,
  points int, reason text, tick int NOT NULL);
CREATE INDEX IF NOT EXISTS proposals_status_idx ON proposals(status);
CREATE TABLE IF NOT EXISTS dynamic_rules (sig text PRIMARY KEY, item_key text NOT NULL, name text NOT NULL,
  props jsonb NOT NULL DEFAULT '{}', discoverer bigint NOT NULL, points int NOT NULL DEFAULT 0, tick int NOT NULL);
"""

# ---------- buffer helpers (integer, conserved) ----------
def get(e, r):  return int(e["buffers"].get(r, 0))
def addb(e, r, n): e["buffers"][r] = int(e["buffers"].get(r, 0)) + int(n)

def state_hash(ents):
    """Deterministic 16-hex digest of world state → per-tick audit/replay chain (same inputs ⇒ same hash)."""
    rows = sorted(ents.values(), key=lambda e: e["id"])
    canon = json.dumps([[e["id"], e["type"], e["x"], e["y"], e.get("owner"),
                         e["buffers"], e["attrs"]] for e in rows],
                        sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode()).hexdigest()[:16]

def linked(eid, ents, ports, links, kind=None, want_type=None):
    """Entities connected to eid via a port-link (optionally filtered by my port kind / their type)."""
    mine = {p["id"] for p in ports if p["entity"] == eid and (kind is None or p["kind"] == kind)}
    port_owner = {p["id"]: p["entity"] for p in ports}
    out = []
    for l in links:
        other = l["b"] if l["a"] in mine else (l["a"] if l["b"] in mine else None)
        if other is None:
            continue
        oe = port_owner.get(other)
        if oe in ents and (want_type is None or ents[oe]["type"] == want_type):
            out.append(ents[oe])
    return out

def battery_of(eid, ents, ports, links):
    bs = linked(eid, ents, ports, links, kind="power", want_type="battery")
    return bs[0] if bs else None

def container_of(eid, ents, ports, links, resource):
    for c in linked(eid, ents, ports, links, kind="item"):
        if c["type"] == "container" and c["attrs"].get("resource") == resource:
            return c
    return None

def depot_price(depot, r):
    """Floating depot price for resource r: glut (recent sells) pushes the buy price down."""
    base = depot["attrs"]["base"].get(r)
    if base is None:
        return None
    g = depot["attrs"].get("glut", {}).get(r, 0)
    buy = max(1, base * 10 // (10 + g))          # more glut → lower buy price
    return {"buy": buy, "sell": buy + base}      # depot resells at buy + base markup

# ---------- per-component behaviors (run each tick) ----------
def behave(e, ents, ports, links, t, events):
    typ = e["type"]
    ev = lambda kind, **d: events.append((t, e["id"], kind, d))
    if typ == "solar":
        b = battery_of(e["id"], ents, ports, links)
        if b and get(b, "energy") < b["attrs"].get("cap", 10**9):
            addb(b, "energy", 1); ev("solar", energy=1)
    elif typ == "generator":
        b = battery_of(e["id"], ents, ports, links)
        if b and get(e, "fuel") >= 1:
            addb(e, "fuel", -1); addb(b, "energy", 10); ev("generate", fuel=-1, energy=10)
    elif typ == "drill":
        b = battery_of(e["id"], ents, ports, links)
        dep = next((x for x in ents.values() if x["type"] == "ore_deposit"
                    and x["x"] == e["x"] and x["y"] == e["y"]), None)
        oc = container_of(e["id"], ents, ports, links, "ore")
        if b and dep and oc and get(b, "energy") >= 5 and dep["attrs"].get("ore", 0) >= 1:
            addb(b, "energy", -5); dep["attrs"]["ore"] -= 1; addb(oc, "ore", 1); ev("mine", ore=1)
    elif typ == "furnace":
        b = battery_of(e["id"], ents, ports, links)
        oc = container_of(e["id"], ents, ports, links, "ore")
        mc = container_of(e["id"], ents, ports, links, "metal")
        if (b and oc and mc and get(b, "energy") >= 5 and get(oc, "ore") >= 2 and get(e, "fuel") >= 1):
            addb(b, "energy", -5); addb(oc, "ore", -2); addb(e, "fuel", -1); addb(mc, "metal", 1)
            ev("smelt", metal=1)
    elif typ == "depot":                                 # floating-price market maker
        glut = e["attrs"].setdefault("glut", {})
        for r in list(glut):
            glut[r] = glut[r] * 4 // 5                    # decay the glut 20%/tick → prices recover
            if glut[r] <= 0:
                del glut[r]
        e["attrs"]["prices"] = {r: depot_price(e, r) for r in e["attrs"]["base"]}   # publish for spectator

# ---------- intents (the only agent->world channel) ----------
def apply_intent(it, ents, cur, t):
    a, args, verb = ents.get(it["agent"]), it["args"], it["verb"]
    if not a:
        return "rejected", "no agent"
    if verb in ("grab", "deposit", "transfer"):
        r, n = args["resource"], int(args.get("n", 1))
        src = a if verb == "deposit" else ents.get(args.get("from"))
        dst = a if verb == "grab" else ents.get(args.get("to"))
        if verb == "transfer":
            src, dst = ents.get(args.get("from")), ents.get(args.get("to"))
        if src and dst and get(src, r) >= n:
            addb(src, r, -n); addb(dst, r, n); return "applied", f"{verb} {n} {r}"
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
        cur.execute("INSERT INTO entities(type,x,y,owner,attrs) VALUES('part',%s,%s,%s,%s)",
                    (a["x"], a["y"], a["id"], Json({"part": part, "stats": stats, "upgrades": ups})))
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
        cur.execute("INSERT INTO entities(type,x,y,owner,attrs) VALUES('vehicle',0,0,%s,%s) RETURNING id",
                    (a["id"], Json({"name": args.get("name", "vehicle"), "parts": parts, **st})))
        vid = cur.fetchone()["id"]
        cur.execute("UPDATE entities SET attrs = attrs || '{\"used\":true}' WHERE id = ANY(%s)",
                    ([r["id"] for r in rows],))
        return "applied", f"vehicle #{vid} drives={st['drives']} v={st['v_ground']} flies={st['flies']}"
    if verb in ("sell", "buy"):                          # trade raw/refined with the depot for credits
        r, n = args["resource"], int(args.get("n", 1))
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
        qty, price = int(args.get("qty", 0)), int(args.get("price", 0))
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
                    (int(args.get("order_id", 0)),))
        o = cur.fetchone()
        if not o or o["status"] != "open" or o["agent"] != a["id"]:
            return "rejected", "no such open order of yours"
        if o["side"] == "sell":
            addb(a, o["resource"], o["qty"])
        else:
            addb(a, "credits", o["qty"] * o["price"])
        cur.execute("UPDATE market_orders SET status='cancelled' WHERE id=%s", (int(args["order_id"]),))
        return "applied", f"cancelled order #{args['order_id']}"
    if verb == "trade":                                  # propose a P2P swap (escrow the 'give')
        target, give, want = args.get("to"), args.get("give", {}), args.get("want", {})
        if target not in ents:
            return "rejected", "no such target"
        if any(get(a, res) < int(q) for res, q in give.items()):
            return "rejected", "insufficient to give"
        for res, q in give.items():
            addb(a, res, -int(q))
        cur.execute("INSERT INTO trades(proposer,target,give,want,created) VALUES(%s,%s,%s,%s,%s) RETURNING id",
                    (a["id"], int(target), Json(give), Json(want), t))
        return "applied", f"trade #{cur.fetchone()['id']} -> #{target}: give {give} want {want}"
    if verb == "accept":
        cur.execute("SELECT proposer,target,give,want,status FROM trades WHERE id=%s",
                    (int(args.get("trade_id", 0)),))
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
        cur.execute("UPDATE trades SET status='accepted' WHERE id=%s", (int(args["trade_id"]),))
        return "applied", f"accepted trade #{args['trade_id']}"
    if verb in ("say", "tell"):                          # agent↔agent communication (observable)
        cur.execute("SELECT 1 FROM messages WHERE sender=%s AND tick=%s LIMIT 1", (a["id"], t))
        if cur.fetchone():
            return "rejected", "one message per tick"
        text = str(args.get("text", ""))[:280]
        rcpt = int(args["to"]) if verb == "tell" else None
        cur.execute("INSERT INTO messages(tick,sender,recipient,text) VALUES(%s,%s,%s,%s)",
                    (t, a["id"], rcpt, text))
        return "applied", (f"tell #{rcpt}: " if rcpt else "say: ") + text[:60]
    if verb == "move":                                   # roam (3 cells on foot; a drivable vehicle + fuel goes farther)
        mkt = next((x for x in ents.values() if x["type"] == "market"), None)
        w = int(mkt["attrs"].get("w", 96)) if mkt else 96
        h = int(mkt["attrs"].get("h", 36)) if mkt else 36
        rng, drove = 3, ""
        cur.execute("SELECT max((attrs->>'v_ground')::int) v FROM entities "
                    "WHERE type='vehicle' AND owner=%s AND (attrs->>'drives')::boolean", (a["id"],))
        vr = cur.fetchone(); vmax = (vr["v"] if vr else 0) or 0
        if vmax:                                          # burn 1 fuel to drive — range scales with the car
            fuel = next((f for f in ("oil", "coal", "wood", "carbon") if get(a, f) >= 1), None)
            if fuel:
                addb(a, fuel, -1); rng = min(10, 3 + vmax // 6); drove = f" (drove on {fuel}, range {rng})"
        dx = max(-rng, min(rng, int(args.get("dx", 0))))
        dy = max(-rng, min(rng, int(args.get("dy", 0))))
        a["x"] = max(0, min(w - 1, a["x"] + dx))
        a["y"] = max(0, min(h - 1, a["y"] + dy))
        return "applied", f"moved to ({a['x']},{a['y']})" + drove
    if verb in ("mine", "chop"):                         # gather from the nearest node (mine=minerals, chop=trees/wood)
        n = int(args.get("n", 5))
        if verb == "mine" and int(a["attrs"].get("altitude", 0)) >= 600:   # standing on the Moon → mine helium-3 + regolith
            took = max(1, min(int(n), 6))
            addb(a, "helium3", took); addb(a, "regolith", took * 2)
            return "applied", f"mined the Moon: +{took} helium-3 (super-fuel), +{took * 2} regolith (lunar building material)"
        want_wood = (verb == "chop")
        deps = [x for x in ents.values() if x["type"] == "deposit" and int(x["attrs"].get("amount", 0)) > 0
                and (x["attrs"].get("resource") == "wood") == want_wood]
        if not deps:
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
        dep["attrs"]["amount"] = have - took
        addb(a, r, took)
        verbed = "chopped" if want_wood else "mined"
        return "applied", f"{verbed} {took} {r} at ({dep['x']},{dep['y']}); {have - took} left" + powered
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
        rule = crafting.combine(ings)
        if rule:                                         # matched a built-in physics pattern
            for k, q in ings.items():                    # consume the inputs
                addb(a, k, -q)
            cur.execute("SELECT name FROM discoveries WHERE rule_key=%s", (rule,))
            disc = cur.fetchone()
            if disc:
                addb(a, rule, 1)
                return "applied", f"crafted {disc['name']} ({rule})"
            item_name = (str(args.get("name", "")).strip()[:32] or rule)
            pts = 5 + 2 * len(ings)
            cur.execute("INSERT INTO discoveries(rule_key, name, discoverer, tick, points) VALUES(%s,%s,%s,%s,%s)",
                        (rule, item_name, a["id"], t, pts))
            a["attrs"]["inventor_points"] = int(a["attrs"].get("inventor_points", 0)) + pts
            addb(a, rule, 1)
            return "applied", f"INVENTED '{item_name}' ({rule}) +{pts} inventor pts!"
        # no built-in pattern → the Inventors' Guild (async LLM referee) judges this novel mixture
        sig = ",".join(sorted(ings))
        cur.execute("SELECT item_key, name FROM dynamic_rules WHERE sig=%s", (sig,))
        dyn = cur.fetchone()
        if dyn:                                          # already a Guild-blessed recipe → deterministic craft
            for k, q in ings.items():
                addb(a, k, -q)
            addb(a, dyn["item_key"], 1)
            return "applied", f"crafted {dyn['name']} ({dyn['item_key']})"
        cur.execute("SELECT 1 FROM proposals WHERE sig=%s AND status IN ('pending','approved') LIMIT 1", (sig,))
        if cur.fetchone():
            return "rejected", "this mixture is already before the Inventors' Guild — try another"
        for k, q in ings.items():                        # escrow the inputs while the Guild reviews
            addb(a, k, -q)
        item_name = str(args.get("name", "")).strip()[:32]
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
        if best < 1.0:
            return "rejected", (f"thrust-to-weight too low to lift off (need thrust >= {GRAVITY}x mass; "
                                f"best you have = {best:.2f}) — add engines/jets/propellers, lighten with a composite frame")
        he3 = get(a, "helium3") >= 1                     # lunar super-fuel → 5x climb
        fuel = "helium3" if he3 else next((f for f in ("oil", "coal", "wood", "carbon") if get(a, f) >= 1), None)
        if not fuel:
            return "rejected", "no fuel to burn (carry oil/coal/wood/carbon — or mine helium-3 on the Moon for a 5x boost)"
        addb(a, fuel, -1)
        alt = min(SKY_TOP, int(a["attrs"].get("altitude", 0)) + (CLIMB * 5 if he3 else CLIMB))
        a["attrs"]["altitude"] = alt
        level = max(int(a["attrs"].get("space_level", 0)), 1 if a["attrs"].get("in_space") else 0)
        msg = f"launched on {fuel} -> altitude {alt}/{SKY_TOP} (twr {best:.1f})"
        for idx, (tier_alt, label) in enumerate(SPACE_TIERS, start=1):   # award each new milestone crossed
            if alt >= tier_alt and level < idx:
                level = idx; a["attrs"]["space_level"] = idx
                if tier_alt >= ATMOSPHERE_TOP:
                    a["attrs"]["in_space"] = True
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
        if new > 0:
            return "applied", f"descending -> altitude {new}"
        was_space = bool(a["attrs"].get("in_space"))
        a["attrs"]["in_space"] = False; a["attrs"]["space_level"] = 0
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
        return "applied", f"deployed #{v['id']} ({v['attrs'].get('name','vehicle')}) — it now roams on its own"
    if verb == "construct":                              # place a structure from a geometric primitive (costs materials → economy)
        shape = str(args.get("shape", "box")).lower()
        if shape not in ("box", "cylinder", "sphere", "cone", "pyramid", "elevator"):
            return "rejected", "shape must be box/cylinder/sphere/cone/pyramid/elevator"
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
            cur.execute("INSERT INTO entities(type,x,y,owner,attrs) VALUES('structure',%s,%s,%s,%s) RETURNING id",
                        (a["x"], a["y"], a["id"], Json({"shape": "elevator", "height": seg, "size": 2,
                                                        "name": str(args.get("name", "orbital elevator"))[:32]})))
            return "applied", f"laid an orbital-elevator base #{cur.fetchone()['id']} ({seg}/{ATMOSPHERE_TOP}) — stack more segments on this cell to reach space"
        on_moon = int(a["attrs"].get("altitude", 0)) >= 600   # build with local regolith when up on the Moon
        size = max(1, min(20, int(args.get("size", 3))))
        height = max(1, min(60, int(args.get("height", size))))
        cost = {"regolith": size + max(1, height // 12)} if on_moon else {"metal": size, "composite": max(1, height // 12)}
        if any(get(a, r) < q for r, q in cost.items()):
            return "rejected", f"{shape} (size {size}, height {height}) needs {cost}" + (" — mine regolith on the Moon" if on_moon else "")
        for r, q in cost.items():
            addb(a, r, -q)
        cur.execute("INSERT INTO entities(type,x,y,owner,attrs) VALUES('structure',%s,%s,%s,%s) RETURNING id",
                    (a["x"], a["y"], a["id"], Json({"shape": shape, "size": size, "height": height,
                                                    "color": str(args.get("color", ""))[:16], "name": str(args.get("name", shape))[:32],
                                                    "alt": 600 if on_moon else 0})))
        return "applied", f"built {shape} (size {size}, h {height}) {'on the Moon ' if on_moon else ''}at ({a['x']},{a['y']}) for {cost}"
    if verb == "ride":                                   # ride a completed orbital elevator to space — no rocket, no fuel
        cur.execute("SELECT (attrs->>'height')::int h FROM entities WHERE type='structure' "
                    "AND attrs->>'shape'='elevator' AND (attrs->>'complete')::boolean AND x=%s AND y=%s LIMIT 1",
                    (a["x"], a["y"]))
        row = cur.fetchone()
        if not row:
            return "rejected", "no completed orbital elevator on this cell (stand at its base)"
        a["attrs"]["altitude"] = max(int(a["attrs"].get("altitude", 0)), min(SKY_TOP, row["h"]))
        a["attrs"]["in_space"] = True
        a["attrs"]["space_level"] = max(int(a["attrs"].get("space_level", 0)), 1)
        return "applied", f"rode the orbital elevator to space (altitude {a['attrs']['altitude']}) — no rocket needed!"
    if verb == "plant":                                  # plant a sapling -> a renewable wood deposit (regrows over time)
        if get(a, "wood") < 1:
            return "rejected", "need 1 wood (a sapling) to plant a tree"
        cur.execute("SELECT attrs->>'gen_seed' s FROM entities WHERE type='deposit' AND attrs->>'gen_seed' IS NOT NULL LIMIT 1")
        row = cur.fetchone(); gs = (row["s"] if row else None) or "42"
        addb(a, "wood", -1)
        cur.execute("INSERT INTO entities(type,x,y,attrs) VALUES('deposit',%s,%s,%s)",
                    (a["x"], a["y"], Json({"resource": "wood", "amount": 3, "biome": "plains",
                                           "gen_seed": gs, "planted": True})))
        return "applied", f"planted a tree at ({a['x']},{a['y']}) — chop it later; trees regrow over time"
    return "rejected", "unknown verb"

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
                cur.execute("INSERT INTO dynamic_rules(sig,item_key,name,props,discoverer,points,tick) "
                            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                            (p["sig"], p["item_key"], p["item_name"] or p["item_key"],
                             Json(p["props"] or {}), p["agent"], pts, t))
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

def roam_autonomous(ents, t):
    """Deployed autonomous vehicles wander the world on their own each tick. DETERMINISTIC (no RNG, so the
    replay/state-hash chain stays valid): heading varies with tick+id; flyers also drift altitude."""
    mkt = next((x for x in ents.values() if x["type"] == "market"), None)
    w = int(mkt["attrs"].get("w", 156)) if mkt else 156
    h = int(mkt["attrs"].get("h", 156)) if mkt else 156
    for v in ents.values():
        if v["type"] != "vehicle" or not v["attrs"].get("autonomous"):
            continue
        v["x"] = max(0, min(w - 1, v["x"] + ((t + v["id"]) % 3) - 1))
        v["y"] = max(0, min(h - 1, v["y"] + ((t * 2 + v["id"] * 3) % 3) - 1))
        if v["attrs"].get("flies"):
            v["attrs"]["alt"] = max(0, min(600, int(v["attrs"].get("alt", 0)) + (((t + v["id"]) % 5) - 2) * 6))

def grow_trees(ents, t):
    """Trees (wood deposits) slowly regrow toward maturity → renewable forestry. Deterministic (staggered by id),
    regrows even from a fully-chopped stump (amount 0), so a `plant`ed/chopped forest comes back on its own."""
    for e in ents.values():
        if e["type"] == "deposit" and e["attrs"].get("resource") == "wood":
            amt = int(e["attrs"].get("amount", 0))
            if amt < 22 and (t + e["id"]) % 8 == 0:
                e["attrs"]["amount"] = amt + 1

# ---------- tick ----------
def tick(conn):
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("UPDATE world SET tick = tick + 1 WHERE id = 1 RETURNING tick")
    t = cur.fetchone()["tick"]
    cur.execute("SELECT * FROM entities"); ents = {e["id"]: e for e in cur.fetchall()}
    cur.execute("SELECT id, entity, kind, dir FROM ports"); ports = cur.fetchall()
    cur.execute("SELECT id, a, b FROM links"); links = cur.fetchall()
    events = []
    cur.execute("SELECT * FROM intents WHERE status = 'pending' ORDER BY id")
    for it in cur.fetchall():
        # loop guard (engine-enforced): an agent repeating the SAME action that keeps FAILING is stuck
        # → block it. Successful repetition (e.g. building 4 wheels) is progress and is never guarded.
        cur.execute("SELECT verb, args, status FROM intents WHERE agent=%s AND status IN ('applied','rejected') "
                    "ORDER BY id DESC LIMIT %s", (it["agent"], LOOP_N))
        recent = cur.fetchall()
        if (len(recent) >= LOOP_N and all(
                r["verb"] == it["verb"] and r["args"] == it["args"] and r["status"] == "rejected"
                for r in recent)):
            cur.execute("UPDATE intents SET status='rejected', result='loop detected (repeated failing action)' "
                        "WHERE id=%s", (it["id"],))
            continue
        try:
            st, res = apply_intent(it, ents, cur, t)
        except Exception as e:                            # one malformed intent must never freeze the world
            st, res = "rejected", f"bad intent ({str(e)[:80]})"
        cur.execute("UPDATE intents SET status=%s, result=%s WHERE id=%s", (st, res, it["id"]))
        events.append((t, it["agent"], "act", {"verb": it["verb"], "status": st, "result": res}))
    for e in list(ents.values()):
        behave(e, ents, ports, links, t, events)
    match_market(ents, cur, t, events)
    expire_trades(ents, cur, t)
    resolve_proposals(ents, cur, t, events)
    roam_autonomous(ents, t)
    grow_trees(ents, t)
    for e in ents.values():
        cur.execute("UPDATE entities SET x=%s, y=%s, buffers=%s, attrs=%s WHERE id=%s",
                    (e["x"], e["y"], Json(e["buffers"]), Json(e["attrs"]), e["id"]))
    for (tk, eid, kind, data) in events:
        cur.execute("INSERT INTO events(tick, entity, kind, data) VALUES(%s,%s,%s,%s)",
                    (tk, eid, kind, Json(data)))
    cur.execute("INSERT INTO tick_hashes(tick, hash) VALUES(%s,%s) "
                "ON CONFLICT (tick) DO UPDATE SET hash=EXCLUDED.hash", (t, state_hash(ents)))
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
    ent("agent", 0, 0, buffers={"metal": 0})                       # idle starter agent (play.py demo); live agents self-register
    ent("depot", 0, 0, attrs={"base": {"ore": 2, "crystal": 8, "metal": 5, "water": 1,
        "copper": 4, "iron": 3, "aluminum": 4, "carbon": 2, "silicon": 6, "salt": 1, "sulfur": 3, "oil": 4,
        "coal": 3, "wood": 2}})
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
