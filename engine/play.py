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
_NEARBY_RADIUS = 9          # Manhattan reach for nearby agents/loot/artifacts (covers max weapon range 9) — the BASE sight (fog of war)
# FOG OF WAR is ADDITIVE: base sight is 9 and nobody ever sees less; effort BUYS a wider agent/threat scan.
_RADAR_VISION_BONUS = 8         # a crafted `radar` (a finished magnet + a chip) widens the nearby_agents/threat scan 9 -> 17
_OBSERVATORY_VISION_BONUS = 4   # an `observatory` doubles as a watchtower — a modest sight bump ON TOP of its forecast
_ORBIT_LO, _ORBIT_HI = 300, 600   # an agent only sees asteroids while it is in orbit (mirror of engine constants)
# --- season 3 medicine branch (mirror of engine constants) — surfaced so gather/heal are targetable ---
_PLANT_RESOURCES = ("herb", "lichen", "fungus", "algae")   # gatherable plant deposits (renewable botany)
_GATHER_RANGE = 8           # auto-walk reach of the `gather` verb (mirror of engine.GATHER_RANGE)
_MEDICINE_ITEMS = ("salve", "stimpack", "medkit", "antidote")  # consumable HP medicines held in buffers (mirror engine.MEDICINES)
from engine import (storm_center,   # SCIENCE LAYER: reuse the ONE storm formula (no drift) for the observatory forecast; safe — engine never imports play
                    EXPANSION_BODIES, DV_NEED, DV_RETURN, TRANSIT_TICKS, SYNODIC, WINDOW_OPEN, window_open, BODY_LABEL, PRODUCERS,
                    location)   # canonical location reader (Phase 6) — the single authority for "where is this agent"
_FORECAST_HORIZON = 30            # ticks of storm track an observatory reveals


def observe(cur, agent_id):
    """The agent's curated view of the world."""
    cur.execute("SELECT tick, to_jsonb(w)->>'era' era FROM world w WHERE id=1")
    wr = cur.fetchone(); now = (wr["tick"] if wr else 0) or 0; era = (wr["era"] if wr else None) or "architect"
    cur.execute("SELECT buffers, x, y, (attrs->>'inventor_points')::int pts, "
                "(attrs->>'altitude')::int alt, (attrs->>'in_space')::boolean space, "
                "(attrs->>'hp')::int hp, (attrs->>'hp_max')::int hp_max, "
                "(attrs->>'downed_until')::int downed, attrs->>'last_robbed_by' robbed_by, "
                "(attrs->>'buff_until')::int buff_until, (attrs->>'toxin_until')::int toxin_until, "
                "attrs->>'transit_to' transit_to, (attrs->>'eta_tick')::int eta_tick, "
                "attrs->>'at_body' at_body, attrs->>'at_body_orbit' at_body_orbit, "
                "COALESCE((attrs->>'adrift')::boolean,false) adrift, attrs->'body_awarded' body_awarded "
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
    # v_ground/v_air: CAST to int and use the DOCUMENTED names. These were the only uncast fields here (their
    # neighbours all cast), so they came back as STRINGS under the undocumented aliases vg/va, while AGENTS.md and
    # /agent/{id} both say v_ground/v_air as ints — a client written to the docs read undefined and its
    # flight/speed checks silently never fired (audit 2026-09-03, F20). vg/va are kept as aliases for one
    # deploy cycle so any client already reading them keeps working.
    cur.execute("SELECT attrs->>'name' name, (attrs->>'drives')::bool drives, (attrs->>'flies')::bool flies, "
                "(attrs->>'v_ground')::int v_ground, (attrs->>'v_air')::int v_air, "
                "(attrs->>'v_ground')::int vg, (attrs->>'v_air')::int va, "
                "COALESCE((attrs->>'orbital_engine')::bool,false) orbital_engine, (attrs->>'fuel_cap')::int fuel_cap "   # EXPANSION: is this an interplanetary (ion) ship + its tankage
                "FROM entities WHERE type='vehicle' AND owner=%s", (agent_id,))
    vehicles = [dict(r) for r in cur.fetchall()]
    # LIMIT like every other board below: one agent held 29,544 of the 29,554 open orders, so its observe built a
    # 1.86MB payload every 2s (row dicts + validation + JSON + gzip = real worker CPU), and with no partial index
    # EVERY agent's observe filtered the whole table (audit 2026-09-03, F17). Newest first + a total so a client
    # can tell it was truncated. `market_orders_agent_open_idx (agent, id) WHERE status='open'` serves this.
    cur.execute("SELECT id,side,resource,qty,price FROM market_orders "
                "WHERE agent=%s AND status='open' ORDER BY id DESC LIMIT 200", (agent_id,))
    orders = [dict(r) for r in cur.fetchall()][::-1]        # back to ascending id (the order agents have always seen)
    cur.execute("SELECT count(*) c FROM market_orders WHERE agent=%s AND status='open'", (agent_id,))
    orders_total = (cur.fetchone() or {}).get("c", len(orders))
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
    # RULE UPDATES: the operator-pushed "what's new" changelog (POST /announce) so agents auto-learn newly added
    # mechanics/verbs without re-reading the docs. Read-only.
    cur.execute("SELECT tick, title, detail, verb FROM rule_updates ORDER BY id DESC LIMIT 6")
    updates = [dict(r) for r in cur.fetchall()]
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
    # FOG OF WAR — how far this agent can SEE other agents: base 9, widened by a held radar / observatory (additive).
    radar_bonus = _RADAR_VISION_BONUS if int(inv.get("radar", 0)) > 0 else 0
    obs_bonus = _OBSERVATORY_VISION_BONUS if int(inv.get("observatory", 0)) > 0 else 0
    vision_radius = _NEARBY_RADIUS + radar_bonus + obs_bonus
    # a wider scan must be allowed to RETURN more, or the radar's extra reach is silently capped away in a crowd
    # (the `vision` block would advertise a bonus the list never delivers). Base 12; lift it when sight is extended.
    agent_limit = 12 if vision_radius == _NEARBY_RADIUS else 30
    # nearby agents (others within the VISION radius): id/name/x/y/hp/wanted — NO karma/yield_buff/notoriety surfaced.
    cur.execute("SELECT id, attrs->>'name' name, x, y, COALESCE((attrs->>'hp')::int, 100) hp, "
                "(COALESCE((attrs->>'wanted_until')::int,0) > %s) wanted, "
                "(abs(x-%s)+abs(y-%s)) dist FROM entities "
                "WHERE type='agent' AND id<>%s AND (abs(x-%s)+abs(y-%s)) <= %s ORDER BY dist, id LIMIT %s",
                (now, ax, ay, agent_id, ax, ay, vision_radius, agent_limit))
    nearby_agents = [{"id": r["id"], "name": r["name"], "x": r["x"], "y": r["y"],
                      "hp": r["hp"], "wanted": bool(r["wanted"]), "dist": r["dist"]} for r in cur.fetchall()]
    vision = {"radius": vision_radius, "base": _NEARBY_RADIUS,
              "bonus": {k: v for k, v in (("radar", radar_bonus), ("observatory", obs_bonus)) if v},
              "note": "fog of war: you only see agents within this Manhattan radius. craft a radar "
                      "(a finished magnet + a chip) to widen it — base sight is 9, a radar adds +8."}

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

    # COMPLETED orbital ELEVATORS anywhere on the map — the free path to space (move onto the base cell + `ride`).
    # Global (there are only a few) and nearest-first, so an agent stranded anywhere knows where it can climb.
    cur.execute("SELECT id, x, y, (attrs->>'height')::int height, (abs(x-%s)+abs(y-%s)) dist FROM entities "
                "WHERE type='structure' AND attrs->>'shape'='elevator' AND (attrs->>'complete')::boolean "
                "ORDER BY dist, id LIMIT 8", (ax, ay))   # two placeholders — the query below takes four
    elevators = [dict(r) for r in cur.fetchall()]
    # nearby ground structures (cities/monuments/roads + UNFINISHED elevators) so the built world is visible, not just on the map.
    cur.execute("SELECT id, x, y, attrs->>'shape' shape, COALESCE((attrs->>'complete')::boolean, false) complete, "
                "(abs(x-%s)+abs(y-%s)) dist FROM entities WHERE type='structure' "
                "AND COALESCE((attrs->>'alt')::int,0)=0 AND (abs(x-%s)+abs(y-%s)) <= %s ORDER BY dist, id LIMIT 12",
                (ax, ay, ax, ay, _NEARBY_RADIUS * 2))
    nearby_structures = [dict(r) for r in cur.fetchall()]

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

    # EXPANSION ERA — interplanetary reach surface. Dormant (null) outside the space/expansion era, exactly like the
    # station board is null off-season. Lets an agent see where it is (in transit / at a body), which launch windows
    # are open right now, and what each destination costs — so depart{dest}/land_body are actionable from /observe.
    expansion = None
    if era in ("space", "expansion", "accord"):
        transit_to = me["transit_to"]; at_body = me["at_body"]; at_orbit = me["at_body_orbit"]
        windows = {b: {"open": window_open(b, now), "dv_need": DV_NEED[b], "transit_ticks": TRANSIT_TICKS[b],
                       "opens_in": (0 if window_open(b, now) else SYNODIC[b] - (now % SYNODIC[b]))} for b in EXPANSION_BODIES}
        loc = ("transit" if transit_to else ("on_" + at_body if at_body else ("orbit_" + at_orbit if at_orbit else "earth")))
        place = location({"attrs": {"transit_to": transit_to, "adrift": me["adrift"], "eta_tick": me["eta_tick"],   # canonical structured location (Phase 6 reader)
                                    "at_body": at_body, "at_body_orbit": at_orbit, "in_space": in_space, "altitude": altitude}})
        producers = None
        if at_body:   # EXPANSION Phase 4 — the ISRU producers you run here + what you can build
            cur.execute("SELECT attrs->>'kind' k, attrs->>'label' l FROM entities WHERE type='structure' "
                        "AND attrs->>'shape'='extractor' AND owner=%s AND attrs->>'body'=%s", (agent_id, at_body))
            yours = [{"kind": r["k"], "label": r["l"]} for r in cur.fetchall()]
            producers = {"yours_here": yours,
                         "buildable": [{"kind": k, "label": s["label"], "cost": s["cost"], "out": s["out"], "consume": s.get("consume"), "period": s["period"]}
                                       for k, s in PRODUCERS.items() if at_body in s["bodies"]],
                         "how": "construct{shape:'extractor',kind} — an ISRU building that drips its yield into your hold every few ticks (a converter also CONSUMES its inputs from your hold), even after you fly home"}
        expansion = {
            "era": era, "location": loc, "place": place,   # `place` = the canonical structured location (Phase 6); `location` kept as the legacy compact string
            "transit": ({"to": transit_to, "eta_tick": me["eta_tick"], "eta_in": (me["eta_tick"] or 0) - now,
                         "adrift": me["adrift"]} if transit_to else None),
            "at_body": at_body, "at_body_orbit": at_orbit,
            "visited": me["body_awarded"] or [],
            "windows": windows,
            "return_dv": (DV_RETURN.get(at_orbit or at_body) if (at_orbit or at_body) else None),
            "producers": producers,
            "how": ("depart{dest} from Earth orbit (altitude 300-600) to a body; carry an ion_thruster ship, fuel (cryo_fuel/helium3) "
                    "and — for Mars/Venus — a heat_shield (+acid_skin for Venus). land_body on arrival. On a body, `mine` yields its "
                    "unique resources; fund the co-op colony with construct{shape:'colony',body,module} (see observe.colony). "
                    "depart{dest:'earth'} to return. Completing a moon Forward Base cheapens the Mars/Venus routes for everyone. "
                    "Once a Mars/Venus colony is COMPLETE, terraform it in sequential co-op stages via construct{shape:'terraform',body,stage} "
                    "(see observe.terraform). Build ISRU extractors with construct{shape:'extractor',kind} (see observe.expansion.producers) — "
                    "they auto-mine into your hold so infrastructure feeds the bills. Mars greened + Venus held + a moon base = THE SOLAR ACCORD. "
                    "PACK BEFORE YOU FLY: heat_shield (superalloy+composite), acid_skin (acid/sulfur+rubber) and hydrogen (water+a motor) are ALL "
                    "craftable on EARTH — make them before launch, not after. STRANDED with no return fuel? distress{} is an emergency recall to Earth "
                    "orbit — it costs HP and jettisons your body haul, so a fueled depart{dest:'earth'} (which keeps your cargo) is always better."),
        }
    return {"tick": now, "position": [ax, ay], "inventory": inv, "inventor_points": ipts, "loose_parts": loose,
            "vehicles": vehicles, "orders": orders, "orders_total": orders_total, "trade_offers": offers, "contracts": contracts, "bounties": bounties, "messages": inbox,
            "nearby_deposits": nearby, "altitude": altitude, "atmosphere_top": 100, "in_space": in_space,
            "hp": hp, "hp_max": hp_max, "downed_until": downed_until,
            "nearby_agents": nearby_agents, "weapons": weapons, "ammo": ammo,
            "alerts": alerts, "last_robbed_by": last_robbed_by,
            "loot": loot, "artifacts": artifacts, "asteroids": asteroids,
            "nearby_plants": nearby_plants, "medicines": medicines, "buff": buff, "toxin": toxin, "forecast": forecast,
            "updates": updates, "elevators": elevators, "nearby_structures": nearby_structures, "vision": vision,
            "expansion": expansion}
