#!/usr/bin/env python3
"""NHA-MMO — the curated per-agent observation used by the server's /observe endpoint.

`observe(cur, agent_id)` returns the agent's view of the world (inventory, loose parts, vehicles, open
orders, incoming trades, recent messages, nearby deposits, altitude, plus the Season-3 combat/social
surface: nearby agents, own HP, held weapons + ammo, recent threat alerts, and nearby loot/artifacts/
asteroids, plus the medicine branch: nearby plant deposits, held medicines, and active buff/toxin state).
Read-only over engine.py's `entities` schema; the caller passes a psycopg2 RealDictCursor.
"""

# weapons the agent may hold (crafted items) + their consumable ammo — surfaced so attack/arm are usable.
_WEAPON_ITEMS = ("kinetic_gun", "energy_weapon", "bomb")
_AMMO_ITEMS = ("slug", "energy_cell")
_NEARBY_RADIUS = 9          # Manhattan reach for nearby agents/loot/artifacts (covers max weapon range 9)
_ORBIT_LO, _ORBIT_HI = 300, 600   # an agent only sees asteroids while it is in orbit (mirror of engine constants)
# --- season 3 medicine branch (mirror of engine constants) — surfaced so gather/heal are targetable ---
_PLANT_RESOURCES = ("herb", "lichen", "fungus", "algae")   # gatherable plant deposits (renewable botany)
_GATHER_RANGE = 8           # auto-walk reach of the `gather` verb (mirror of engine.GATHER_RANGE)
_MEDICINE_ITEMS = ("salve", "stimpack", "medkit", "antidote")  # consumable HP medicines held in buffers (mirror engine.MEDICINES)
from engine import storm_center   # SCIENCE LAYER: reuse the ONE storm formula (no drift) for the observatory forecast; safe — engine never imports play
_FORECAST_HORIZON = 30            # ticks of storm track an observatory reveals


def observe(cur, agent_id):
    """The agent's curated view of the world."""
    cur.execute("SELECT tick FROM world WHERE id=1")
    wr = cur.fetchone(); now = (wr["tick"] if wr else 0) or 0
    cur.execute("SELECT buffers, x, y, (attrs->>'inventor_points')::int pts, "
                "(attrs->>'altitude')::int alt, (attrs->>'in_space')::boolean space, "
                "(attrs->>'hp')::int hp, (attrs->>'hp_max')::int hp_max, "
                "(attrs->>'downed_until')::int downed, attrs->>'last_robbed_by' robbed_by, "
                "(attrs->>'buff_until')::int buff_until, (attrs->>'toxin_until')::int toxin_until "
                "FROM entities WHERE id=%s", (agent_id,))
    me = cur.fetchone(); inv = me["buffers"]; ax, ay = me["x"], me["y"]; ipts = me["pts"] or 0
    altitude = me["alt"] or 0; in_space = bool(me["space"])
    hp = me["hp"] if me["hp"] is not None else 100
    hp_max = me["hp_max"] if me["hp_max"] is not None else 100
    downed_until = me["downed"] or 0
    last_robbed_by = int(me["robbed_by"]) if me["robbed_by"] is not None else None
    cur.execute("SELECT attrs->>'part' part FROM entities "
                "WHERE type='part' AND owner=%s AND (attrs->>'used') IS NULL", (agent_id,))
    loose = [r["part"] for r in cur.fetchall()]
    cur.execute("SELECT attrs->>'name' name, (attrs->>'drives')::bool drives, (attrs->>'flies')::bool flies, "
                "attrs->>'v_ground' vg, attrs->>'v_air' va FROM entities "
                "WHERE type='vehicle' AND owner=%s", (agent_id,))
    vehicles = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT id,side,resource,qty,price FROM market_orders "
                "WHERE agent=%s AND status='open' ORDER BY id", (agent_id,))
    orders = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT id,proposer,give,want FROM trades "
                "WHERE target=%s AND status='open' ORDER BY id", (agent_id,))
    offers = [dict(r) for r in cur.fetchall()]
    # open supply-contract board relevant to me: jobs I can take (open to anyone or reserved for me) + my own posted jobs.
    cur.execute("SELECT id, poster, reward, want, target, deadline FROM contracts "
                "WHERE status='open' AND kind='supply' AND (target IS NULL OR target=%s OR poster=%s) ORDER BY id DESC LIMIT 20",
                (agent_id, agent_id))
    contracts = [{**dict(r), "mine": r["poster"] == agent_id} for r in cur.fetchall()]
    # open KILL-BOUNTIES (public hunts): whom to hunt for a reward — and, critically, any bounty on MY OWN head (on_me).
    # Order own-head bounties FIRST so the LIMIT can never drop the observer's own-head warning behind newer hunts.
    cur.execute("SELECT id, poster, reward, target, deadline FROM contracts "
                "WHERE status='open' AND kind='kill' ORDER BY (target=%s) DESC, id DESC LIMIT 20", (agent_id,))
    bounties = [{**dict(r), "on_me": r["target"] == agent_id, "mine": r["poster"] == agent_id} for r in cur.fetchall()]
    cur.execute("SELECT m.tick, m.sender, s.attrs->>'name' sender_name, (s.type='human') is_human, "
                "m.recipient, m.text FROM messages m LEFT JOIN entities s ON s.id = m.sender "
                "WHERE m.recipient IS NULL OR m.recipient=%s ORDER BY m.id DESC LIMIT 15", (agent_id,))
    inbox = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT id, attrs->>'resource' resource, (attrs->>'amount')::int amount, x, y, "
                "(abs(x-%s)+abs(y-%s)) dist FROM entities WHERE type='deposit' "
                "AND x BETWEEN %s AND %s AND y BETWEEN %s AND %s "     # audit(perf): sargable box → uses the entities_deposit_xy index instead of scanning every deposit; ±72 always holds >=6 on a live world and mining navigates within it
                "AND (attrs->>'amount')::int > 0 "
                "ORDER BY dist LIMIT 6", (ax, ay, ax - 72, ax + 72, ay - 72, ay + 72))
    nearby = [dict(r) for r in cur.fetchall()]

    # --- season 3: who/what is in reach (so attack/steal/ally/collect/attune/dock are targetable) ---
    # nearby agents (others within radius): id/name/x/y/hp/wanted — NO karma/yield_buff/notoriety surfaced.
    cur.execute("SELECT id, attrs->>'name' name, x, y, COALESCE((attrs->>'hp')::int, 100) hp, "
                "(COALESCE((attrs->>'wanted_until')::int,0) > %s) wanted, "
                "(abs(x-%s)+abs(y-%s)) dist FROM entities "
                "WHERE type='agent' AND id<>%s AND (abs(x-%s)+abs(y-%s)) <= %s ORDER BY dist, id LIMIT 12",
                (now, ax, ay, agent_id, ax, ay, _NEARBY_RADIUS))
    nearby_agents = [{"id": r["id"], "name": r["name"], "x": r["x"], "y": r["y"],
                      "hp": r["hp"], "wanted": bool(r["wanted"]), "dist": r["dist"]} for r in cur.fetchall()]

    # held weapons + ammo counts (drawn from inventory buffers, so the agent knows it can attack/arm).
    weapons = {w: int(inv.get(w, 0)) for w in _WEAPON_ITEMS if int(inv.get(w, 0)) > 0}
    ammo = {m: int(inv.get(m, 0)) for m in _AMMO_ITEMS if int(inv.get(m, 0)) > 0}

    # threat alerts: recent events where I am the victim (so retaliation / fleeing is possible).
    cur.execute("SELECT tick, entity, kind, data FROM events "
                "WHERE tick >= %s AND ("
                "  (kind='damage' AND (data->>'target')::bigint = %s)"
                "  OR (kind='theft' AND (data->>'victim')::bigint = %s)"
                "  OR (kind='destroyed' AND entity = %s)"
                ") ORDER BY id DESC LIMIT 15", (now - 60, agent_id, agent_id, agent_id))
    alerts = []
    for r in cur.fetchall():
        d = r["data"] or {}
        if r["kind"] == "damage":
            alerts.append({"tick": r["tick"], "kind": "attacked", "by": r["entity"],
                           "dmg": d.get("dmg"), "hp": d.get("hp")})
        elif r["kind"] == "theft":
            alerts.append({"tick": r["tick"], "kind": "robbed", "by": r["entity"],
                           "resource": d.get("resource"), "n": d.get("n"),
                           "success": d.get("success"), "detected": d.get("detected")})
        elif r["kind"] == "destroyed":
            alerts.append({"tick": r["tick"], "kind": "downed", "by": d.get("by")})

    # nearby loot piles (pick up with `collect`).
    cur.execute("SELECT id, x, y, buffers, (abs(x-%s)+abs(y-%s)) dist FROM entities "
                "WHERE type='loot' AND (abs(x-%s)+abs(y-%s)) <= %s ORDER BY dist, id LIMIT 8",
                (ax, ay, ax, ay, _NEARBY_RADIUS))
    loot = [{"id": r["id"], "x": r["x"], "y": r["y"], "contents": r["buffers"], "dist": r["dist"]}
            for r in cur.fetchall()]

    # nearby ancient artifacts (bond with `attune`).
    cur.execute("SELECT id, x, y, attrs->>'kind' kind, attrs->>'loc' loc, (abs(x-%s)+abs(y-%s)) dist "
                "FROM entities WHERE type='artifact' AND (abs(x-%s)+abs(y-%s)) <= %s ORDER BY dist, id LIMIT 8",
                (ax, ay, ax, ay, _NEARBY_RADIUS))
    artifacts = [{"id": r["id"], "x": r["x"], "y": r["y"], "kind": r["kind"], "loc": r["loc"],
                  "dist": r["dist"]} for r in cur.fetchall()]

    # --- season 3 medicine branch: plant deposits, held medicines, active buff (so gather/heal are usable) ---
    # nearby plant deposits (herb/lichen/fungus/algae) within gather reach — so `gather` is targetable.
    # (these also appear in nearby_deposits; this is the gather-specific, biome-tagged subset.)
    cur.execute("SELECT id, attrs->>'resource' resource, (attrs->>'amount')::int amount, x, y, "
                "(abs(x-%s)+abs(y-%s)) dist FROM entities "
                "WHERE type='deposit' AND x BETWEEN %s AND %s AND y BETWEEN %s AND %s "   # audit(perf): sargable box (= gather reach) uses the (x,y) index; exact Manhattan <=range kept below as the post-filter → identical results
                "AND (attrs->>'amount')::int > 0 "
                "AND attrs->>'resource' = ANY(%s) AND (abs(x-%s)+abs(y-%s)) <= %s "
                "ORDER BY dist, id LIMIT 6",
                (ax, ay, ax - _GATHER_RANGE, ax + _GATHER_RANGE, ay - _GATHER_RANGE, ay + _GATHER_RANGE, list(_PLANT_RESOURCES), ax, ay, _GATHER_RANGE))
    nearby_plants = [dict(r) for r in cur.fetchall()]

    # crafted medicines I hold (counts from inventory buffers) — so `heal` knows what's spendable.
    medicines = {m: int(inv.get(m, 0)) for m in _MEDICINE_ITEMS if int(inv.get(m, 0)) > 0}

    # active stimpack buff window, if any (remaining ticks) — never exposes karma/yield_buff.
    buff_until = int(me["buff_until"]) if me["buff_until"] is not None else 0
    buff = {"until": buff_until, "remaining": buff_until - now} if buff_until > now else None
    # toxin state, if the (optional) toxin mechanic is live — surfaced only when actually afflicted.
    toxin_until = int(me["toxin_until"]) if me["toxin_until"] is not None else 0
    toxin = {"until": toxin_until, "remaining": toxin_until - now} if toxin_until > now else None

    # asteroids — only visible/relevant while the agent is in orbit (dock + mine them there).
    asteroids = []
    if _ORBIT_LO <= altitude < _ORBIT_HI:
        cur.execute("SELECT id, x, y, attrs->>'resource' resource, (attrs->>'amount')::int amount, "
                    "(abs(x-%s)+abs(y-%s)) dist FROM entities WHERE type='asteroid' ORDER BY dist, id LIMIT 8",
                    (ax, ay))
        asteroids = [{"id": r["id"], "x": r["x"], "y": r["y"], "resource": r["resource"],
                      "amount": r["amount"], "dist": r["dist"]} for r in cur.fetchall()]

    # SCIENCE LAYER: a crafted `observatory` (lens + chip) computes the world's DETERMINISTIC dynamics ahead of time.
    # Pure functions of the tick → read-only, never touches world state or the hash chain. MVP = the weather (storm).
    forecast = None
    if int(inv.get("observatory", 0)) > 0:
        cur.execute("SELECT (attrs->>'w')::int w, (attrs->>'h')::int h FROM entities WHERE type='market' LIMIT 1")
        mk = cur.fetchone(); W = (mk["w"] if mk and mk["w"] else 156); H = (mk["h"] if mk and mk["h"] else 156)   # mirror engine's storm default (156) EXACTLY so the forecast can never drift from the real storm
        track = [dict(zip(("in", "x", "y", "r"), (k,) + storm_center(now + k, W, H))) for k in range(0, _FORECAST_HORIZON, 3)]
        over_in = next((k for k in range(0, 61)                 # ticks until the storm covers YOUR cell (half mine/chop yield)
                        if (lambda c: abs(ax - c[0]) + abs(ay - c[1]) <= c[2])(storm_center(now + k, W, H))), None)
        forecast = {"storm_track": track, "storm_over_you_in": over_in,
                    "note": "your observatory computes the deterministic weather ahead — mine outside the storm radius for full yield"}

    return {"position": [ax, ay], "inventory": inv, "inventor_points": ipts, "loose_parts": loose,
            "vehicles": vehicles, "orders": orders, "trade_offers": offers, "contracts": contracts, "bounties": bounties, "messages": inbox,
            "nearby_deposits": nearby, "altitude": altitude, "atmosphere_top": 100, "in_space": in_space,
            "hp": hp, "hp_max": hp_max, "downed_until": downed_until,
            "nearby_agents": nearby_agents, "weapons": weapons, "ammo": ammo,
            "alerts": alerts, "last_robbed_by": last_robbed_by,
            "loot": loot, "artifacts": artifacts, "asteroids": asteroids,
            "nearby_plants": nearby_plants, "medicines": medicines, "buff": buff, "toxin": toxin, "forecast": forecast}
