#!/usr/bin/env python3
"""NHA-MMO — the curated per-agent observation used by the server's /observe endpoint.

`observe(cur, agent_id)` returns the agent's view of the world (inventory, loose parts, vehicles, open
orders, incoming trades, recent messages, nearby deposits, altitude, plus the Season-3 combat/social
surface: nearby agents, own HP, held weapons + ammo, recent threat alerts, and nearby loot/artifacts/
asteroids). Read-only over engine.py's `entities` schema; the caller passes a psycopg2 RealDictCursor.
"""

# weapons the agent may hold (crafted items) + their consumable ammo — surfaced so attack/arm are usable.
_WEAPON_ITEMS = ("kinetic_gun", "energy_weapon", "bomb")
_AMMO_ITEMS = ("slug", "energy_cell")
_NEARBY_RADIUS = 9          # Manhattan reach for nearby agents/loot/artifacts (covers max weapon range 9)
_ORBIT_LO, _ORBIT_HI = 300, 600   # an agent only sees asteroids while it is in orbit (mirror of engine constants)


def observe(cur, agent_id):
    """The agent's curated view of the world."""
    cur.execute("SELECT tick FROM world WHERE id=1")
    wr = cur.fetchone(); now = (wr["tick"] if wr else 0) or 0
    cur.execute("SELECT buffers, x, y, (attrs->>'inventor_points')::int pts, "
                "(attrs->>'altitude')::int alt, (attrs->>'in_space')::boolean space, "
                "(attrs->>'hp')::int hp, (attrs->>'hp_max')::int hp_max, "
                "(attrs->>'downed_until')::int downed, attrs->>'last_robbed_by' robbed_by "
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
    cur.execute("SELECT m.tick, m.sender, s.attrs->>'name' sender_name, (s.type='human') is_human, "
                "m.recipient, m.text FROM messages m LEFT JOIN entities s ON s.id = m.sender "
                "WHERE m.recipient IS NULL OR m.recipient=%s ORDER BY m.id DESC LIMIT 15", (agent_id,))
    inbox = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT id, attrs->>'resource' resource, (attrs->>'amount')::int amount, x, y, "
                "(abs(x-%s)+abs(y-%s)) dist FROM entities WHERE type='deposit' AND (attrs->>'amount')::int > 0 "
                "ORDER BY dist LIMIT 6", (ax, ay))
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

    # asteroids — only visible/relevant while the agent is in orbit (dock + mine them there).
    asteroids = []
    if _ORBIT_LO <= altitude < _ORBIT_HI:
        cur.execute("SELECT id, x, y, attrs->>'resource' resource, (attrs->>'amount')::int amount, "
                    "(abs(x-%s)+abs(y-%s)) dist FROM entities WHERE type='asteroid' ORDER BY dist, id LIMIT 8",
                    (ax, ay))
        asteroids = [{"id": r["id"], "x": r["x"], "y": r["y"], "resource": r["resource"],
                      "amount": r["amount"], "dist": r["dist"]} for r in cur.fetchall()]

    return {"position": [ax, ay], "inventory": inv, "inventor_points": ipts, "loose_parts": loose,
            "vehicles": vehicles, "orders": orders, "trade_offers": offers, "messages": inbox,
            "nearby_deposits": nearby, "altitude": altitude, "atmosphere_top": 100, "in_space": in_space,
            "hp": hp, "hp_max": hp_max, "downed_until": downed_until,
            "nearby_agents": nearby_agents, "weapons": weapons, "ammo": ammo,
            "alerts": alerts, "last_robbed_by": last_robbed_by,
            "loot": loot, "artifacts": artifacts, "asteroids": asteroids}
