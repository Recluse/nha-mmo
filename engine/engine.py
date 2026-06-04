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

DSN = os.environ.get("PG_DSN", "host=127.0.0.1 dbname=nhamoo user=postgres")
LOOP_N = 3   # engine-enforced: reject an intent identical to the agent's last LOOP_N applied ones

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
    if verb == "build":                                  # craft one part from the agent's materials
        part = args.get("part"); cost = vehicles.BUILD_COST.get(part)
        if not cost:
            return "rejected", f"unknown part {part}"
        if not all(get(a, res) >= q for res, q in cost.items()):
            return "rejected", f"insufficient for {part} (need {cost})"
        for res, q in cost.items():
            addb(a, res, -q)
        cur.execute("INSERT INTO entities(type,x,y,owner,attrs) VALUES('part',%s,%s,%s,%s)",
                    (a["x"], a["y"], a["id"], Json({"part": part})))
        return "applied", f"built {part}"
    if verb == "finalize":                               # assemble the agent's loose parts into a vehicle
        cur.execute("SELECT id, attrs->>'part' part FROM entities "
                    "WHERE type='part' AND owner=%s AND (attrs->>'used') IS NULL", (a["id"],))
        rows = cur.fetchall()
        if not rows:
            return "rejected", "no loose parts"
        parts = [r["part"] for r in rows]
        st = vehicles.finalize(parts)
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
        st, res = apply_intent(it, ents, cur, t)
        cur.execute("UPDATE intents SET status=%s, result=%s WHERE id=%s", (st, res, it["id"]))
        events.append((t, it["agent"], "act", {"verb": it["verb"], "status": st, "result": res}))
    for e in list(ents.values()):
        behave(e, ents, ports, links, t, events)
    match_market(ents, cur, t, events)
    expire_trades(ents, cur, t)
    for e in ents.values():
        cur.execute("UPDATE entities SET buffers=%s, attrs=%s WHERE id=%s",
                    (Json(e["buffers"]), Json(e["attrs"]), e["id"]))
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
    def port(e, kind, dir):
        cur.execute("INSERT INTO ports(entity,name,kind,dir) VALUES(%s,%s,%s,%s) RETURNING id",
                    (e, kind[0], kind, dir)); return cur.fetchone()[0]
    def link(pa, pb): cur.execute("INSERT INTO links(a,b) VALUES(%s,%s)", (pa, pb))

    ent("agent", 0, 0, buffers={"metal": 0})
    deposit = ent("ore_deposit", 1, 0, attrs={"ore": 20})
    battery = ent("battery", 0, 0, buffers={"energy": 0}, attrs={"cap": 1000})
    solar   = ent("solar", 0, 0)
    gen     = ent("generator", 0, 0, buffers={"fuel": 5})
    drill   = ent("drill", 1, 0)                                   # on the deposit's cell
    ore_c   = ent("container", 1, 0, buffers={"ore": 0}, attrs={"resource": "ore", "cap": 1000})
    ent("depot", 0, 0, attrs={"base": {"ore": 2, "fuel": 1, "crystal": 8, "metal": 5, "water": 1}})
    ent("market", 0, 0, attrs={"last": {}})
    bp = port(battery, "power", "bi")
    link(port(solar, "power", "out"), bp)
    link(port(gen, "power", "out"), bp)
    link(port(drill, "power", "in"), bp)
    link(port(drill, "item", "out"), port(ore_c, "item", "in"))
    conn.commit()
    print(f"seeded demo: deposit={deposit} battery={battery} solar={solar} gen={gen} drill={drill} ore_container={ore_c}")

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
