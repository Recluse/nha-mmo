#!/usr/bin/env python3
"""NHA-MMO — the curated per-agent observation used by the server's /observe endpoint.

`observe(cur, agent_id)` returns the agent's view of the world (inventory, loose parts, vehicles, open
orders, incoming trades, recent messages, nearby deposits, altitude). Read-only over engine.py's
`entities` schema; the caller passes a psycopg2 RealDictCursor.
"""


def observe(cur, agent_id):
    """The agent's curated view of the world."""
    cur.execute("SELECT buffers, x, y, (attrs->>'inventor_points')::int pts, "
                "(attrs->>'altitude')::int alt, (attrs->>'in_space')::boolean space FROM entities WHERE id=%s", (agent_id,))
    me = cur.fetchone(); inv = me["buffers"]; ax, ay = me["x"], me["y"]; ipts = me["pts"] or 0
    altitude = me["alt"] or 0; in_space = bool(me["space"])
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
    return {"position": [ax, ay], "inventory": inv, "inventor_points": ipts, "loose_parts": loose,
            "vehicles": vehicles, "orders": orders, "trade_offers": offers, "messages": inbox,
            "nearby_deposits": nearby, "altitude": altitude, "atmosphere_top": 100, "in_space": in_space}
