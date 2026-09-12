#!/usr/bin/env python3
"""NHA-MMO — the curated per-agent observation used by the server's /observe endpoint.

`observe(cur, agent_id)` returns the agent's view of the world (inventory, loose parts, vehicles, open
orders, incoming trades, recent messages, nearby deposits, altitude, plus the Season-3 combat/social
surface: nearby agents, own HP, held weapons + ammo, recent threat alerts, and nearby loot/artifacts/
asteroids, plus the medicine branch: nearby plant deposits, held medicines, and active buff/toxin state).
Read-only over engine.py's `entities` schema; the caller passes a psycopg2 RealDictCursor.
"""

from engine import (storm_center,   # SCIENCE LAYER: reuse the ONE storm formula (no drift) for the observatory forecast; safe — engine never imports play
                    EXPANSION_BODIES, DV_NEED, DV_RETURN, TRANSIT_TICKS, SYNODIC, WINDOW_OPEN, window_open, BODY_LABEL, PRODUCERS,
                    location,   # canonical location reader (Phase 6) — the single authority for "where is this agent"
                    WEAPON_STATS, MEDICINES, PLANT_RESOURCES, GATHER_RANGE, ORBIT_LO, ORBIT_HI,
                    WARP_PLACES, WARP_NEED, WARP_CAP, WARP_MIN, WARP_DV_DIV, WARP_TICKS, WARP_CRATE_CAP,
                    GUILD_FEE, COLONY_MODULES, COLONY_MIN, COLONY_LABEL, EXPANSION_PRICES, BODY_MINE,
                    BODY_ITEMS, TWR_DEPART, GRAVITY, FUEL_VE, CORRECTION_EVERY, TRANSIT_TICKS)

# DERIVED from the engine, not hand-copied. These five used to be duplicated literals sitting right above the
# import that praises "reuse the ONE formula (no drift)" — and `observe` is the agents' ONLY sensory channel, so a
# new medicine/plant/weapon would be accepted by the engine yet invisible to every agent, with nothing failing
# (audit 2026-09-03). All values were identical at the time of this change, so behaviour is unchanged.


def _fair_window(msgs, keep=15, per_sender=3):
    """Trim a newest-first feed to `keep` lines, letting no single sender hold more than `per_sender` of them.

    Collapsing identical repeats is not enough on its own: a shouter that VARIES its threats slips straight past
    it and still owned 12 of 15 lines on the live world. This gives the window back to distinct voices without
    silencing anyone — the loudest sender keeps its newest few lines, the rest of the feed keeps the others.
    """
    out, seen = [], {}
    for m in msgs:
        sid = m.get("sender")
        if seen.get(sid, 0) >= per_sender:
            continue
        seen[sid] = seen.get(sid, 0) + 1
        out.append(m)
        if len(out) >= keep:
            break
    return out


def _collapse_repeats(msgs, keep=15):
    """Fold a sender's consecutive identical lines into one carrying `repeated`.

    The inbox is a fixed-size window, so one agent shouting the same line every few ticks silently evicts every
    other voice — a playtester found the feed was wall-to-wall Barbarian threats with no room left for messages
    from actual players. Collapsing is honest (the count is kept, nothing is invented) and it hands the window
    back to distinct speakers. Newest-first order in, newest-first order out.
    """
    out = []
    for m in msgs:
        prev = out[-1] if out else None
        if prev and prev.get("sender") == m.get("sender") and prev.get("text") == m.get("text"):
            prev["repeated"] = int(prev.get("repeated", 1)) + 1
            prev["tick"] = m.get("tick")          # keep the OLDEST tick of the run, so "since when" reads right
            continue
        out.append(m)
    return out[:keep]




def _preflight(inv, vehicles, altitude, in_space):
    """Can THIS agent depart, and if not, what exactly is missing.

    `observe.expansion` explained where you could go and why, and a playtester reported it was still not enough to
    prepare a first flight: no assembly order, no gear requirement, no round-trip fuel, and nothing checked against
    the inventory actually held. This answers per destination with the agent's own ship and hold — deliberately as
    a CHECKLIST rather than a Δv figure, because the engine owns that arithmetic and two copies of it would drift.
    """
    inv = inv or {}
    ships = [v for v in (vehicles or [])
             if v.get("flies") and v.get("orbital_engine")]
    fuels = {f: int(inv.get(f, 0) or 0) for f in FUEL_VE if int(inv.get(f, 0) or 0) > 0}
    out = {"has_interplanetary_ship": bool(ships),
           "in_earth_orbit": bool(in_space and _ORBIT_LO <= int(altitude or 0) <= _ORBIT_HI),
           "fuel_held": fuels,
           "ship": (ships[0].get("name") if ships else None),
           "destinations": {}}
    for body, items in BODY_ITEMS.items():
        missing_items = [it for it in items if int(inv.get(it, 0) or 0) < 1]
        corr = TRANSIT_TICKS[body] // CORRECTION_EVERY
        blockers = []
        if not ships:
            blockers.append("no ship that flies with an ion_thruster (build parts, then finalize)")
        if missing_items:
            blockers.append(f"missing {', '.join(missing_items)} in your hold — craft before launch, not after")
        if not out["in_earth_orbit"]:
            blockers.append(f"you are not in Earth orbit (altitude {_ORBIT_LO}-{_ORBIT_HI}) — ride an elevator or launch")
        if not fuels:
            blockers.append("no usable fuel held (cryo_fuel or helium3 carry the most per unit)")
        out["destinations"][body] = {
            "needs_in_hold": list(items) or [],
            "needs_landing_gear_on_ship": body in ("deimos", "phobos", "mars"),
            "min_thrust_to_weight": TWR_DEPART[body],
            "course_correction_fuel": corr,     # spare units the crossing burns on top of the departure Δv
            "ready": not blockers,
            "blockers": blockers,
        }
    out["how"] = ("build{part:...} the pieces, finalize{} into one ship (it must FLY and carry an ion_thruster), "
                  "craft the protective items on EARTH, ride an elevator or launch to orbit 300-600, then "
                  "depart{dest}. Carry spare fuel beyond the departure burn: the crossing spends one unit per "
                  f"{CORRECTION_EVERY} ticks on course corrections, and you still need the fuel to come home.")
    return out


def _needs_you(cur, agent_id, inv=None):
    """The one co-op module where THIS agent would be the funder that unblocks it — with the names of the people
    already in it.

    A module needs 2-3 DISTINCT funders by rule, so the scarce thing in this world is not ore but a partner, and
    until now a board showed a COUNT of funders and no names: you could see that three people were building
    something and still have nobody to address. Naming them turns "it needs 48 regolith" into "Miner and Trader
    need one more person here", which is a sentence an agent can act on — and a reason to send the first direct
    message this world has seen in a while.
    """
    cur.execute("SELECT attrs FROM entities WHERE type='structure' AND attrs->>'shape'='colony'")
    best = None
    for row in cur.fetchall():
        at = row["attrs"] or {}
        body = at.get("body")
        if not body or body not in COLONY_MODULES or at.get("complete"):
            continue
        for key, spec in COLONY_MODULES[body].items():
            mod = (at.get("modules") or {}).get(key) or {}
            if mod.get("complete"):
                continue
            contrib = mod.get("contrib") or {}
            if str(agent_id) in contrib:
                continue                       # already mine
            have = mod.get("have") or {}
            short = {r: spec["need"][r] - int(have.get(r, 0))
                     for r in sorted(spec["need"]) if int(have.get(r, 0)) < spec["need"][r]}
            # Two tiers. A module still short of DISTINCT funders is the strongest ask — joining it literally
            # unblocks it — so it always beats one that merely needs more material. Once the funder gap is
            # closed everywhere (as it was within a day of `invest` shipping), fall back to whatever is nearest
            # to finishing, because "who is on it and what is it short of" is still the only actionable social
            # fact on the board. Without this tier the whole block returned null and helped nobody.
            tier = 0 if len(contrib) < COLONY_MIN[body] else 1
            rank = (tier, sum(short.values()))
            if best is None or rank < best[0]:
                best = (rank, body, key, spec["label"], short, sorted(contrib), tier)
    if best is None:
        return None
    _, body, key, label, short, ids, tier = best
    # Ordered, and a funder whose entity is gone still shows as #id rather than vanishing from the list: the
    # bare fetchall() this replaced returned rows in whatever order the planner chose and silently dropped
    # missing ids, so the names did not line up with the funder set they claimed to describe (caught in review).
    names = []
    if ids:
        want = [int(i) for i in ids]
        cur.execute("SELECT id, attrs->>'name' nm FROM entities WHERE id = ANY(%s)", (want,))
        by = {r["id"]: (r["nm"] or ("#" + str(r["id"]))) for r in cur.fetchall()}
        names = [by.get(i, "#" + str(i)) for i in want]
    # THREE buckets, not two. Splitting on "is it purchasable" alone put `nickel` and `superalloy` under "mine it
    # on Phobos", which is simply false — neither exists on that surface; they are an asteroid metal and a craft.
    # An agent would have flown out and dug for something that is not there. What a body actually yields is
    # BODY_MINE, and everything in neither list has to be made or mined at home and carried out.
    here = {r for r, _mult in BODY_MINE.get(body, ())}
    # WHAT CAN *YOU* PUT IN, RIGHT NOW. The first cut described the module and left the agent to work out whether
    # it could help — a playtester had to read a separate endpoint, then guess. This answers with the agent's own
    # wallet and hold: `credits` buys the fixed-price lines (capped by the per-funder ceiling), held materials
    # cover any line, and `would_complete` says whether doing it finishes the module outright.
    inv = inv or {}
    cap_frac = COLONY_CAP[body]
    spec_need = COLONY_MODULES[body][key]["need"]
    budget = int(inv.get("credits", 0) or 0)
    can_buy, can_give = {}, {}
    for r, q in sorted(short.items()):
        cap = (spec_need[r] * cap_frac + 99) // 100        # my ceiling on this line, same rule the engine applies
        price = EXPANSION_PRICES.get(r)
        if price:
            units = min(q, cap, budget // price)
            if units > 0:
                can_buy[r] = units; budget -= units * price
        held = int(inv.get(r, 0) or 0)
        units = min(q, cap, held)
        if units > 0:
            can_give[r] = units
    covered = {r: max(can_buy.get(r, 0), can_give.get(r, 0)) for r in short}
    would_complete = all(covered.get(r, 0) >= q for r, q in short.items())
    buyable = {r: q for r, q in short.items() if EXPANSION_PRICES.get(r)}
    mine_here = {r: q for r, q in short.items() if r not in buyable and r in here}
    haul = {r: q for r, q in short.items() if r not in buyable and r not in here}
    why = ("it is SHORT OF FUNDERS — joining it is what unblocks it"
           if tier == 0 else "its funder count is met; what it lacks now is material")
    how = []
    if buyable:
        how.append(f"buy its fixed-price lines from ANYWHERE with invest{{body:'{body}',module:'{key}',credits:N}} ({buyable})")
    if mine_here:
        how.append(f"mine {mine_here} ON {COLONY_LABEL[body]} itself and fund it there with "
                   f"construct{{shape:'colony',body:'{body}',module:'{key}'}}")
    if haul:
        how.append(f"make or mine {haul} at home and CARRY it out — this body does not produce it")
    if names:
        how.append(f"and tell {', '.join(names)} you are coming — they are already in it")
    return {"body": body, "module": key, "label": label, "colony": COLONY_LABEL[body],
            "still_needs": short, "buyable_now": buyable, "mine_on_site": mine_here, "haul_from_earth": haul,
            "you_can_buy_now": can_buy, "you_can_give_now": can_give, "would_complete_it": would_complete,
            "funders": names, "funders_required": COLONY_MIN[body], "why": why,
            "how": "; ".join(how) or "nothing left to add"}


def _warp_block(cur, here):
    """SEASON 6a — the warp-gate network as the agent sees it. A gate alone does nothing; a PAIR turns `depart`
    between its two ends into a warp: no launch window, a fifth of the Δv, WARP_TICKS to cross. Read straight
    from the world so a half-funded gate is visible and fundable, not a surprise."""
    cur.execute("SELECT attrs FROM entities WHERE type='structure' AND attrs->>'shape'='warpgate'")
    rows = [r["attrs"] for r in cur.fetchall()]
    # Deliberately NOT None when no gate exists yet. An agent cannot build the first gate if the mechanic is
    # invisible until somebody has already built one — the same chicken-and-egg that kept every agent ignoring
    # credits until the rules said out loud what money buys. The `how` line is the discovery path.
    built = {a.get("place") for a in rows if a.get("complete")}
    out = {"open": sorted(built), "linked_from_here": sorted(built - {here}) if here in built else [],
           "how": (f"construct{{shape:'warpgate',place}} — co-op funded like a colony ({WARP_CAP}% cap per funder, "
                   f"{WARP_MIN}+ funders). A body gate needs that body's colony COMPLETE and you standing on it; "
                   f"the Earth gate needs you home. Blueprint = having funded any FINISHED colony. A live pair "
                   f"warps in {WARP_TICKS} ticks at 1/{WARP_DV_DIV} the Δv and ignores the window. Exotic body cargo "
                   f"rides ONLY in single-use warp_container crates ({WARP_CRATE_CAP} units each, spent by the "
                   f"crossing) — craft them from superalloy + chip + crystal."),
           "building": [{"place": a.get("place"), "need": WARP_NEED.get(a.get("place"), {}),
                         "have": {r: int((a.get("have") or {}).get(r, 0)) for r in WARP_NEED.get(a.get("place"), {})},
                         "funders": len(a.get("contrib") or {})}
                        for a in rows if not a.get("complete")]}
    return out

_WEAPON_ITEMS = tuple(WEAPON_STATS)
_MEDICINE_ITEMS = tuple(MEDICINES)
_PLANT_RESOURCES = tuple(PLANT_RESOURCES)
_GATHER_RANGE = GATHER_RANGE
# Same env var app.py reads for ONLINE_TICKS, so the two cannot drift apart: "acted within this many ticks".
_ONLINE_TICKS = int(__import__("os").environ.get("ONLINE_TICKS", "180"))
_ORBIT_LO, _ORBIT_HI = ORBIT_LO, ORBIT_HI
_AMMO_ITEMS = ("slug", "energy_cell")   # no engine constant to derive from — consumed by name in the attack path
_NEARBY_RADIUS = 9          # Manhattan reach for nearby agents/loot/artifacts (covers max weapon range 9) — the BASE sight (fog of war)
# FOG OF WAR is ADDITIVE: base sight is 9 and nobody ever sees less; effort BUYS a wider agent/threat scan.
_RADAR_VISION_BONUS = 8         # a crafted `radar` (a finished magnet + a chip) widens the nearby_agents/threat scan 9 -> 17
_OBSERVATORY_VISION_BONUS = 4   # an `observatory` doubles as a watchtower — a modest sight bump ON TOP of its forecast
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
    # THE INBOX, IN THREE CHANNELS. Measured on the live world 2026-09-12: of the 15 lines an agent actually saw,
    # TWELVE were one shouting agent and two were system decrees — one line in fifteen came from a fellow player.
    # An LLM reading that learns, correctly, that chat is noise, and stops reading it. So: a message addressed TO
    # this agent is fetched SEPARATELY and can never be evicted by a burst of shouting; the public feed is capped
    # per sender so one voice cannot own the window; and every line is tagged with its `channel`, so "the system
    # is flooding" and "someone is talking to me" stop looking like the same thing.
    _MSG_COLS = ("SELECT m.tick, m.sender, s.attrs->>'name' sender_name, (s.type='human') is_human, "
                 "s.type sender_type, m.recipient, m.text FROM messages m LEFT JOIN entities s ON s.id = m.sender ")
    cur.execute(_MSG_COLS + "WHERE m.recipient=%s ORDER BY m.id DESC LIMIT 10", (agent_id,))
    direct = [dict(r) for r in cur.fetchall()]
    cur.execute(_MSG_COLS + "WHERE m.recipient IS NULL ORDER BY m.id DESC LIMIT 120")
    public = _fair_window(_collapse_repeats([dict(r) for r in cur.fetchall()], keep=120), keep=15, per_sender=3)
    for m in direct:
        m["channel"] = "direct"
    for m in public:
        m["channel"] = ("system" if (m.get("sender_type") in ("universe", "oracle"))
                        else ("adviser" if m.get("is_human") else "public"))
    for m in direct + public:
        m.pop("sender_type", None)
    # Slots are RESERVED, not sorted-then-truncated. Sorting the union by tick and cutting to 20 let a burst of
    # fresh public chat push older direct messages off the end — the exact eviction this split exists to prevent
    # (caught in review). Directs take their own slots first, then the public window fills the rest.
    inbox = direct[:10] + public[:10]
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
    # `last_act` is NOT an attr — there is no such field, and reading one reported every neighbour as long dead,
    # which is worse than saying nothing. It comes from the newest EVENT, exactly the way the roster derives it:
    # ORDER BY ev.id DESC LIMIT 1 over the (entity, id) index, never max(tick), which cannot use an index and
    # walked the whole ~9M-row events table per agent (13.3s for ONE, measured in the 2026-09-03 audit).
    cur.execute("SELECT e.id, e.attrs->>'name' name, e.x, e.y, COALESCE((e.attrs->>'hp')::int, 100) hp, "
                "(COALESCE((e.attrs->>'wanted_until')::int,0) > %s) wanted, "
                "(SELECT ev.tick FROM events ev WHERE ev.entity=e.id AND ev.kind <> 'destroyed' "
                " ORDER BY ev.id DESC LIMIT 1) last_act, "
                # `live` says an agent DID something; it does not say it will answer you. A playtester wrote to
                # the most active agent in the world and got nothing back — correctly, since most of these are
                # scripted and never read their inbox. When somebody last SPOKE is the better signal for "is
                # there a conversation to be had here", so both are reported and the agent can choose.
                "(SELECT m.tick FROM messages m WHERE m.sender=e.id ORDER BY m.id DESC LIMIT 1) last_spoke, "
                "(abs(e.x-%s)+abs(e.y-%s)) dist FROM entities e "
                "WHERE e.type='agent' AND e.id<>%s AND (abs(e.x-%s)+abs(e.y-%s)) <= %s ORDER BY dist, e.id LIMIT %s",
                (now, ax, ay, agent_id, ax, ay, vision_radius, agent_limit))
    # `acted_ago`/`live` answer the question a newcomer actually has. A playtest put it exactly: "nearby agents are
    # visible, but there is no way to tell who has done anything recently" — and most of the bodies standing around
    # this world are long abandoned. Finding a partner was harder than reaching orbit, which is the wrong ordering
    # for a game whose every co-op board needs two or three people.
    nearby_agents = [{"id": r["id"], "name": r["name"], "x": r["x"], "y": r["y"],
                      "hp": r["hp"], "wanted": bool(r["wanted"]), "dist": r["dist"],
                      "acted_ago": (now - int(r["last_act"])) if r["last_act"] else None,
                      "live": bool(r["last_act"] and (now - int(r["last_act"])) <= _ONLINE_TICKS),
                      "spoke_ago": (now - int(r["last_spoke"])) if r["last_spoke"] else None,
                      "talks": bool(r["last_spoke"])}    # has this one EVER said anything? most never have
                     for r in cur.fetchall()]
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
    # Only STAGED builds carry a `complete` flag. Everything else — a road, a city block, a monument — is finished
    # the instant it exists, and defaulting those to false reported every completed build as a building site.
    # A playtester built a road, was paid for it, and was still shown `complete:false`.
    cur.execute("SELECT id, x, y, attrs->>'shape' shape, "
                "COALESCE((attrs->>'complete')::boolean, "
                "         attrs->>'shape' NOT IN ('elevator','station','colony','terraform','warpgate')) complete, "
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
            "gates": _warp_block(cur, at_orbit or at_body or "earth"),
            "needs_you": _needs_you(cur, agent_id, inv),   # the board YOU can move right now, what YOU can put in, and who to tell
            "preflight": _preflight(inv, vehicles, altitude, in_space),   # can I actually fly yet, and what is missing
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
            "vehicles": vehicles, "orders": orders, "orders_total": orders_total, "trade_offers": offers, "contracts": contracts, "bounties": bounties, "messages": inbox, "messages_direct": direct,
            "nearby_deposits": nearby, "altitude": altitude, "atmosphere_top": 100, "in_space": in_space,
            "hp": hp, "hp_max": hp_max, "downed_until": downed_until,
            "nearby_agents": nearby_agents, "weapons": weapons, "ammo": ammo,
            "alerts": alerts, "last_robbed_by": last_robbed_by,
            "loot": loot, "artifacts": artifacts, "asteroids": asteroids,
            "nearby_plants": nearby_plants, "medicines": medicines, "buff": buff, "toxin": toxin, "forecast": forecast,
            "updates": updates, "elevators": elevators, "nearby_structures": nearby_structures, "vision": vision,
            # The Guild's filing fee has to be visible BEFORE the first surprise rejection — an agent that
            # discovers a price by being refused learns the wrong lesson (that inventing is broken).
            "guild": {"filing_fee": GUILD_FEE,
                      # Said plainly. The first wording led with "inventing well is free", which buried the two
                      # facts that actually matter to a poor agent deciding whether to try: you pay UP FRONT, and
                      # you only get it back if the Guild grants the recipe. A playtester called that out.
                      "note": (f"combine{{}} with a mixture nobody has made before files it with the Inventors' "
                               f"Guild. You pay {GUILD_FEE} credits AT THE MOMENT YOU FILE, whatever the outcome. "
                               f"If the Guild GRANTS it you get all {GUILD_FEE} back and the recipe becomes world "
                               f"law under your name; if it rejects, the credits are gone to the republic. "
                               f"Historically about four filings in five are rejected, so file a mixture whose "
                               f"physics you can argue for — and pass `name` to say what you think you made.")},
            "expansion": expansion}
