#!/usr/bin/env python3
"""NHA-MMO — agent loop demo: an autonomous agent observes the world, crafts parts from raw
materials, and assembles a working vehicle. The core "your agent plays" loop (the scripted brain
here is where an LLM agent would plug in).

Self-contained over Postgres (engine.py's `entities` schema + vehicles.finalize / BUILD_COST).
Run:  PG_DSN=... python play.py
"""
import os
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from vehicles import BUILD_COST, finalize

DSN = os.environ.get("PG_DSN", "host=127.0.0.1 dbname=nhamoo user=postgres")

CAR = ["frame", "wheel", "wheel", "wheel", "wheel", "engine", "fuel_tank", "cockpit"]


def observe(cur, agent_id):
    """The agent's curated view of the world."""
    cur.execute("SELECT buffers, x, y, (attrs->>'inventor_points')::int pts FROM entities WHERE id=%s", (agent_id,))
    me = cur.fetchone(); inv = me["buffers"]; ax, ay = me["x"], me["y"]; ipts = me["pts"] or 0
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
    cur.execute("SELECT tick,sender,recipient,text FROM messages "
                "WHERE recipient IS NULL OR recipient=%s ORDER BY id DESC LIMIT 15", (agent_id,))
    inbox = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT id, attrs->>'resource' resource, (attrs->>'amount')::int amount, x, y, "
                "(abs(x-%s)+abs(y-%s)) dist FROM entities WHERE type='deposit' AND (attrs->>'amount')::int > 0 "
                "ORDER BY dist LIMIT 6", (ax, ay))
    nearby = [dict(r) for r in cur.fetchall()]
    return {"position": [ax, ay], "inventory": inv, "inventor_points": ipts, "loose_parts": loose,
            "vehicles": vehicles, "orders": orders, "trade_offers": offers, "messages": inbox,
            "nearby_deposits": nearby}


def can_afford(inv, part):
    return all(int(inv.get(r, 0)) >= n for r, n in BUILD_COST.get(part, {}).items())


def act_build(cur, agent_id, part):
    cur.execute("SELECT buffers, x, y FROM entities WHERE id=%s FOR UPDATE", (agent_id,))
    me = cur.fetchone()
    inv = me["buffers"]
    if part not in BUILD_COST:
        return False, f"unknown part {part}"
    if not can_afford(inv, part):
        return False, f"не хватает на {part} (нужно {BUILD_COST[part]}, есть {inv})"
    for r, n in BUILD_COST[part].items():
        inv[r] = int(inv.get(r, 0)) - n
    cur.execute("UPDATE entities SET buffers=%s WHERE id=%s", (Json(inv), agent_id))
    cur.execute("INSERT INTO entities(type,x,y,owner,attrs) VALUES('part',%s,%s,%s,%s)",
                (me["x"], me["y"], agent_id, Json({"part": part})))
    return True, f"собрал {part} (−{BUILD_COST[part]})"


def act_finalize(cur, agent_id, name):
    cur.execute("SELECT id, attrs->>'part' part FROM entities "
                "WHERE type='part' AND owner=%s AND (attrs->>'used') IS NULL", (agent_id,))
    rows = cur.fetchall()
    if not rows:
        return False, "нет свободных деталей"
    parts = [r["part"] for r in rows]
    st = finalize(parts)                                  # схлопывание в одно тело + ТТХ
    cur.execute("INSERT INTO entities(type,x,y,owner,attrs) VALUES('vehicle',0,0,%s,%s) RETURNING id",
                (agent_id, Json({"name": name, "parts": parts, **st})))
    vid = cur.fetchone()["id"]
    cur.execute("UPDATE entities SET attrs = attrs || '{\"used\":true}' WHERE id = ANY(%s)",
                ([r["id"] for r in rows],))
    verdict = " ".join(filter(None, [
        f"ЕДЕТ v={st['v_ground']}" if st["drives"] else "",
        f"ЛЕТИТ v={st['v_air']}" if st["flies"] else ""])) or "НЕ едет/НЕ летит"
    return True, f"собрал машину #{vid} '{name}': {verdict}"


def brain_build(cur, agent_id, blueprint, name):
    """Scripted agent brain (an LLM agent would replace this): build each part, then finalize."""
    print(f"[агент {agent_id}] цель: '{name}' из {len(blueprint)} деталей")
    for part in blueprint:
        ok, msg = act_build(cur, agent_id, part)
        print(f"   {'✓' if ok else '✗'} {msg}")
        if not ok:
            print("   → стоп: не хватило материалов")
            return
    ok, msg = act_finalize(cur, agent_id, name)
    print(f"   ★ {msg}")


def main():
    conn = psycopg2.connect(DSN)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id FROM entities WHERE type='agent' ORDER BY id LIMIT 1")
    row = cur.fetchone()
    if row:
        agent_id = row["id"]
        cur.execute("UPDATE entities SET buffers = buffers || %s WHERE id=%s",
                    (Json({"metal": 60, "crystal": 4}), agent_id))
    else:
        cur.execute("INSERT INTO entities(type,x,y,buffers) VALUES('agent',0,0,%s) RETURNING id",
                    (Json({"metal": 60, "crystal": 4}),))
        agent_id = cur.fetchone()["id"]
    conn.commit()

    print("== observe (до) =="); print("  ", observe(cur, agent_id))
    brain_build(cur, agent_id, CAR, "моя_тачка")
    conn.commit()
    print("== observe (после) =="); print("  ", observe(cur, agent_id))
    conn.close()


if __name__ == "__main__":
    main()
