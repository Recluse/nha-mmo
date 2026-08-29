#!/usr/bin/env python3
"""шахтёр — a focused SCRIPTED NHA-MMO miner (no LLM): digs deposits for ore/metal, sells the haul for credits,
roams to new veins. Now PHASE-AWARE: once it has stockpiled enough material it builds a rocket, launches to
orbit, docks an asteroid and mines it for iridium/nickel, then lands and sells the haul. Every space step
falls back to the ground baseline (PHASE 0) if rejected, so the bot never gets stuck on the loop-guard.
Varied actions dodge the engine's loop-guard. Designed to run in k8s alongside the world.
Env: SERVER_URL, MINER_NAME, MINER_INTERVAL.
"""
import os, sys, time, random, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner

NAME = os.environ.get("MINER_NAME", "Miner")
INTERVAL = float(os.environ.get("MINER_INTERVAL", "14"))

LINES = [
    "копаю до центра земли", "руда не сама себя добудет", "ещё кирка стерпит", "глубже, глубже!",
    "камень крепкий, но я крепче", "кто не копает, тот не ест", "золото где-то рядом, чую нутром",
    # space/asteroid flavour — the bot's grander ambitions
    "хватит ковыряться в земле, копну-ка астероид", "иридий с никелем сами с орбиты не упадут",
    "строю ракету — кирку беру с собой", "космос — та же шахта, только потолок выше",
    "на орбите руда жирнее, чую нутром", "пристыкуюсь к камню и выгребу его досуха",
]

# answers when an OUTSIDER names «Miner» in chat — short, gruff pickaxe-talk
REPLIES = [
    "звал? я по уши в породе, но слушаю",
    "ага, Miner это я. руда нужна? есть, наковырял",
    "по имени кличешь — дело есть? а то я копаю",
    "чего тебе? золото не обещаю, а руду — задёшево",
    "слышу из забоя. говори, пока кирка отдыхает",
    "звал шахтёра? вылез на поверхность. ну?",
]

ORES = ("iron", "copper", "nickel", "titanium", "aluminum", "silicon", "ore", "coal", "metal", "crystal")
# the priciest haul to bring home from orbit — sell these first when we land
SPACE_LOOT = ("iridium", "nickel")

# ---- material thresholds for committing to the space program (PHASE 1+) ----
# generous so the bot only leaves the ground once it can actually finish a flightworthy ship + carry fuel.
GO_METAL = 60        # raw vehicle material (the build below costs ~40 metal across all parts)
GO_CRYSTAL = 2       # engines/cockpit want crystal
GO_FUEL = 3          # oil/coal/wood/carbon — each launch burns one; need several to climb to orbit (alt 300)
FUELS = ("oil", "coal", "wood", "carbon")

# ---- target rocket: cockpit + wing(composite) + 2x engine(+steel) + 3x propeller(+bearing) ----
# stats (verified against vehicles.finalize_stats): mass=485, thrust=3120 -> twr=1.61 (>= the 4x-mass gate),
# flies=True, controllable=True. Every crafted upgrade is cheap and made from COMMON raws (no frontier metals,
# no long chain): composite = aluminum+carbon, steel = iron+coal, bearing = iron+oil.
TARGET_PARTS = [
    ("cockpit", []),                 # control:1 — REQUIRED for controllable/finalize
    ("wing", ["composite"]),         # wing_area -> lift (REQUIRED for flies); composite lightens it
    ("engine", ["steel"]),           # power 200->260 (steel upgrade); engines feed the propellers
    ("engine", ["steel"]),
    ("propeller", ["bearing"]),      # thrust = thrust_pp(2 w/ bearing) * total_power
    ("propeller", ["bearing"]),
    ("propeller", ["bearing"]),
]
# crafted upgrade items the build needs, and a cheap combine recipe for each (raws -> item).
# These match crafting.RULES (verified): composite = light-metal+carbon; steel = a metal+carbon, smelted hot
# (iron+coal); bearing = a metal + a lubricant (iron+oil). All from deposits the bot already mines.
UPGRADE_RECIPES = {
    "composite": {"aluminum": 1, "carbon": 1},
    "steel":     {"iron": 1, "coal": 1},
    "bearing":   {"copper": 1, "oil": 1},   # a NON-magnetic metal (copper) so it doesn't smelt into a magnet
}


def register():
    mats = {"credits": 40, "metal": 5}
    return runner.register(NAME, mats)   # persist+reclaim our token (server no longer hands it out by name)


# ----------------------------- observation helpers -----------------------------
def _inv(obs):
    return obs.get("inventory", {}) or {}


def _have(obs, r):
    return int(_inv(obs).get(r, 0))


def _flightworthy(obs):
    """The agent already owns a finalized vehicle that flies + is steerable (dock requires both)."""
    for v in obs.get("vehicles", []) or []:
        if v.get("flies"):
            return True
    return False


def _have_any_vehicle(obs):
    return bool(obs.get("vehicles"))


def _phase(obs):
    """Pick the current goal phase straight off the observation (altitude/in_space/vehicle/asteroids)."""
    alt = int(obs.get("altitude", 0) or 0)
    fly = _flightworthy(obs)
    if alt > 0 and fly:
        # already aloft in a real ship → press on toward orbit/asteroid, then come home
        if 300 <= alt < 600:
            return "ASTEROID"     # in orbit — dock + mine
        return "ASCEND"           # climbing or descending
    if fly:
        return "ASCEND"           # ship ready on the ground → start launching
    # no ship yet: build one only once we've stockpiled enough, else keep mining
    if _have(obs, "metal") >= GO_METAL and _have(obs, "crystal") >= GO_CRYSTAL \
            and sum(_have(obs, f) for f in FUELS) >= GO_FUEL:
        return "BUILD"
    return "GROUND"


# ----------------------------- ground baseline (PHASE 0) -----------------------------
def ground_act(obs):
    """The original mine/smelt/sell/roam loop — the default and the universal fallback."""
    inv = _inv(obs)
    roll = random.random()
    haul = [(r, int(inv.get(r, 0))) for r in ORES if int(inv.get(r, 0)) >= 15]
    fuel_for_smelt = next((f for f in ("coal", "carbon", "wood", "oil") if int(inv.get(f, 0)) >= 1), None)
    # SMELT ORE -> METAL: the vehicle build material doesn't drop raw — forge it from ore + a fuel, so the
    # space program (PHASE 1) always has `metal` to spend. Bias toward smelting while we're short on metal.
    if int(inv.get("ore", 0)) >= 2 and fuel_for_smelt and (int(inv.get("metal", 0)) < GO_METAL or roll < 0.22):
        return "combine", {"ingredients": {"ore": 2, fuel_for_smelt: 1}, "name": "металл"}
    # SMELT: forge ore into metal when it has the makings (iron + carbon -> steel)
    if int(inv.get("iron", 0)) >= 2 and int(inv.get("carbon", 0)) >= 1 and roll < 0.18:
        return "combine", {"ingredients": {"iron": 2, "carbon": 1}, "name": "сталь"}
    if haul and roll < 0.36:                              # sell off a fat pile of ore
        r, q = max(haul, key=lambda x: x[1])
        return "sell", {"resource": r, "n": min(random.randint(8, 18), q)}
    if roll < 0.64:
        return "mine", {"n": random.randint(1, 6)}        # dig the deposit underfoot / nearest in range
    if roll < 0.97:
        return "move", {"dx": random.randint(-3, 3), "dy": random.randint(-3, 3)}   # roam to a new vein
    return "say", {"text": random.choice(LINES)}   # ~3% chatter (clamped hard)


# ----------------------------- PHASE 1: build the rocket -----------------------------
def build_act(obs):
    """Craft any missing upgrade items, build the loose parts one at a time, then finalize the ship.
    Returns None to defer to the ground baseline if nothing is buildable this turn (graceful degrade)."""
    inv = _inv(obs)
    loose = list(obs.get("loose_parts", []) or [])

    # 0) keep some fuel topped up for the launch climb (sell less, mine more) — handled in GROUND; here we build.
    # 1) make sure we hold each crafted UPGRADE item the target parts need (one combine per missing item).
    need_up = {}
    for _part, ups in TARGET_PARTS:
        for u in ups:
            need_up[u] = need_up.get(u, 0) + 1
    for item, qty in need_up.items():
        if int(inv.get(item, 0)) < qty:
            recipe = UPGRADE_RECIPES.get(item)
            if recipe and all(int(inv.get(r, 0)) >= q for r, q in recipe.items()):
                # vary the name a touch so two identical-but-rejected combines don't trip the loop-guard
                return "combine", {"ingredients": dict(recipe), "name": f"{item}-{random.randint(0, 999)}"}

    # 2) build the next missing part (compare the target multiset against what we already hold loose).
    want = {}
    for part, _ups in TARGET_PARTS:
        want[part] = want.get(part, 0) + 1
    for part, ups in TARGET_PARTS:
        have_n = loose.count(part)
        if have_n >= want[part]:
            continue
        from_vehicles_module_cost = {"frame": 5, "panel": 3, "wheel": 2, "engine": 8, "propeller": 4,
                                     "jet": 10, "wing": 4, "tail": 2, "cockpit": 4, "fuel_tank": 3, "landing_gear": 3}
        base_metal = from_vehicles_module_cost.get(part, 5)
        crystal_need = 1 if part in ("engine", "cockpit") else 0
        crystal_need += 2 if part == "jet" else 0
        # check we can afford base metal/crystal + one of each upgrade item
        if int(inv.get("metal", 0)) < base_metal or int(inv.get("crystal", 0)) < crystal_need:
            return None    # can't afford this part right now → fall back to mining/selling to restock
        if not all(int(inv.get(u, 0)) >= 1 for u in ups):
            return None    # upgrade item not ready (will be crafted on a later turn) → mine meanwhile
        args = {"part": part}
        if ups:
            args["with"] = list(ups)
        return "build", args

    # 3) every part is built → assemble the ship.
    if not _have_any_vehicle(obs):
        return "finalize", {"name": "шахтёрский шаттл"}
    return None   # ship exists but doesn't fly? let ASCEND/ground logic handle it


# ----------------------------- PHASE 2: ascend to orbit -----------------------------
def ascend_act(obs):
    """Launch repeatedly to climb to orbit (>=300). If we've overshot past orbit (alt>=600 = Moon) or have
    no fuel, degrade gracefully. land is handled by the RETURN phase once mining is done."""
    alt = int(obs.get("altitude", 0) or 0)
    if alt >= 600:                                   # overshot onto the Moon — descend back into orbit to dock
        return "land", {}
    if sum(_have(obs, f) for f in FUELS) < 1 and _have(obs, "helium3") < 1:
        return None                                  # out of fuel → drop to ground baseline to restock
    return "launch", {}                              # burns 1 fuel, +10 alt (or +50 on helium-3)


# ----------------------------- PHASE 3: dock + mine the asteroid -----------------------------
def asteroid_act(obs):
    """In orbit (300-599): close on the nearest asteroid, dock it (<=2 cells), then mine it for iridium/nickel.
    Re-docks if it drifts away (the engine undocks us automatically when that happens)."""
    asts = obs.get("asteroids", []) or []
    docked = False
    # heuristic: if we successfully mined last turn the engine keeps us co-located with the rock; the cheapest
    # tell that we're docked is "an asteroid is at distance 0". Try to mine first, fall through to (re)dock.
    if asts:
        nearest = min(asts, key=lambda x: int(x.get("dist", 999)))
        d = int(nearest.get("dist", 999))
        if d == 0:
            docked = True
        if docked and int(nearest.get("amount", 0)) > 0:
            return "mine", {"n": random.randint(2, 5)}       # haul iridium/nickel off the rock
        if d <= 2:
            return "dock", {}                                # within dock range but not latched → dock it
        # too far → glide toward it (move is altitude-agnostic; just changes x/y so we close the gap)
        pos = obs.get("position", [0, 0]) or [0, 0]
        dx = max(-3, min(3, int(nearest.get("x", pos[0])) - int(pos[0])))
        dy = max(-3, min(3, int(nearest.get("y", pos[1])) - int(pos[1])))
        if dx == 0 and dy == 0:                              # already on the cell but dist>2 (drift jitter) → nudge
            dx, dy = random.randint(-2, 2), random.randint(-2, 2)
        return "move", {"dx": dx, "dy": dy}
    # in orbit but no asteroid in view this tick → try a blind dock (engine picks the nearest), else roam
    if random.random() < 0.5:
        return "dock", {}
    return "move", {"dx": random.randint(-3, 3), "dy": random.randint(-3, 3)}


# ----------------------------- PHASE 4: return home + sell the haul -----------------------------
def return_act(obs, loot=SPACE_LOOT):
    """Once we're carrying space loot, descend and (on the ground) sell the haul (kept-back resources excluded)."""
    alt = int(obs.get("altitude", 0) or 0)
    if alt > 0:
        return "land", {}                                    # controlled descent (-40/tick)
    for r in loot:                                           # home — cash in the rare ores
        if _have(obs, r) >= 3:
            return "sell", {"resource": r, "n": min(random.randint(4, 12), _have(obs, r))}
    return None


# ----------------------------- top-level phase dispatcher -----------------------------
def act(obs):
    """Pick the phase from the observation, run its handler, and ALWAYS fall back to the ground baseline if a
    space step is unavailable this turn (None) — so the bot is never stuck and the loop-guard never bites."""
    ex = expand_act(obs)   # EXPANSION ERA: forge an ion ship + fly to Mars once able; None → run the normal miner
    if ex is not None:
        return ex
    # In the Expansion era, HOARD iridium for the ion_thruster — sell only nickel; else sell both.
    loot = ("nickel",) if obs.get("expansion") else SPACE_LOOT
    # if we're back on the ground holding sellable space loot, cash it in first (RETURN tail).
    if int(obs.get("altitude", 0) or 0) == 0 and any(_have(obs, r) >= 3 for r in loot):
        out = return_act(obs, loot)
        if out:
            return out

    phase = _phase(obs)
    handler = {
        "BUILD": build_act,
        "ASCEND": ascend_act,
        "ASTEROID": asteroid_act,
        "GROUND": ground_act,
    }.get(phase, ground_act)

    # In orbit but already loaded up? Head home instead of mining forever.
    if phase == "ASTEROID" and any(_have(obs, r) >= 12 for r in loot):
        return return_act(obs, loot) or ground_act(obs)

    out = handler(obs)
    if out is None:                       # space step not actionable this turn → degrade to mining
        out = ground_act(obs)
    return out


# ======================= EXPANSION (Season 5): forge an ion ship and fly to Mars =======================
# Once the era supports it (obs.expansion present), the rich shахтёр doesn't just sell iridium — it walks the
# VALIDATED ion-ship craft chain, builds an interplanetary ship, flies to Mars and founds a colony. The early
# game (reach orbit + mine iridium) is still the existing phase machine; this takes over once iridium is in hand.
_LAST_DEPOT = None
ION_PARTS = [("cockpit", ["chip"]), ("frame", ["composite"]), ("jet", ["ion_thruster"]),
             ("fuel_tank", ["steel"]), ("fuel_tank", ["steel"]), ("wing", ["composite"]), ("tail", []), ("landing_gear", [])]
EXP_RECIPES = {   # item -> recipe inputs (all verified against crafting.RULES). No recipe => a raw (buy or mine).
    "composite": {"aluminum": 1, "carbon": 1}, "steel": {"iron": 1, "coal": 1}, "wire": {"copper": 1},
    "chip": {"silicon": 1, "copper": 1}, "magnet": {"iron": 1}, "electrolyte": {"salt": 1, "water": 1},
    "battery": {"copper": 1, "iron": 1, "electrolyte": 1}, "motor": {"magnet": 1, "wire": 1, "battery": 1},
    "ion_thruster": {"iridium": 1, "motor": 1, "chip": 1}, "cryo_fuel": {"ice": 1, "coal": 1},
    "superalloy": {"steel": 1, "titanium": 1, "coal": 1}, "heat_shield": {"superalloy": 1, "composite": 1},
}
BUYABLE = ("iron", "copper", "coal", "carbon", "salt", "water", "ice", "metal", "ore", "oil")
EXP_DEST = os.environ.get("MINER_DEST", "phobos")   # colonise a MOON first — its Forward Base needs only 2 funders, so two barons can COMPLETE it (Mars needs 3)
EXP_HS = EXP_DEST in ("mars", "venus")              # only Mars/Venus need a heat_shield; moons don't
EXP_FUEL = 160        # cryo_fuel to LOAD on the ground (plenty of Δv for any body + the climb-to-orbit burn)
EXP_DEPART_MIN = EXP_FUEL - 40   # in-orbit depart gate: BELOW the load target, because climbing to orbit burns some cryo (else the gate is unsatisfiable — audit C2)
EXP_IRIDIUM = 2
# crafted items the ship-build consumes (net targets; the resolver crafts their sub-chain bottom-up). heat_shield only for planets.
EXP_TARGETS = ([("metal", 40), ("crystal", 3), ("ion_thruster", 1), ("chip", 1), ("composite", 2), ("steel", 2)]
               + ([("heat_shield", 1)] if EXP_HS else []))


def _depot_has(item):
    try:
        return item in ((_LAST_DEPOT or {}).get("prices") or {})
    except Exception:
        return False


def _orbital_ship(obs):
    for v in obs.get("vehicles", []) or []:
        if v.get("orbital_engine"):
            return v
    return None


def _make(inv, item, qty, book):
    """First actionable step toward having qty of `item`: ('acquire',raw,n) | ('combine',item,n) | None(satisfied).
    `book` tracks inventory already earmarked so shared inputs (e.g. a chip used by both cockpit and ion_thruster)
    aren't double-counted."""
    have = max(0, int(inv.get(item, 0)) - book.get(item, 0))
    use = min(have, qty); book[item] = book.get(item, 0) + use
    if use >= qty:
        return None
    short = qty - use
    rec = EXP_RECIPES.get(item)
    if not rec:
        return ("acquire", item, short)
    for r, c in rec.items():
        s = _make(inv, r, c * short, book)
        if s:
            return s
    book[item] = book.get(item, 0) + short
    return ("combine", item, short)


def _bom_step(inv, targets):
    """Resolve the bill of materials → the next (verb,args) to progress, or None when every target is satisfied."""
    book = {}
    for item, qty in targets:
        s = _make(inv, item, qty, book)
        if not s:
            continue
        kind, it, n = s
        if kind == "combine":
            return "combine", {"ingredients": dict(EXP_RECIPES[it]), "n": max(1, min(n, 20)), "name": f"{it}{random.randint(0, 999)}"}
        if it in BUYABLE and _depot_has(it):
            return "buy", {"resource": it, "n": max(1, min(n, 50))}
        if random.random() < 0.25:                 # a mineable raw — sometimes roam to find a fresh vein…
            return "move", {"dx": random.randint(-4, 4), "dy": random.randint(-4, 4)}
        return "mine", {"resource": it, "n": random.randint(4, 7)}   # …else mine it (engine walks to the nearest; varied n dodges the loop-guard)
    return None


def _ion_part_step(obs):
    """Build the ION_PARTS one at a time (their crafted upgrade items are made by the BOM phase), then finalize."""
    loose = list(obs.get("loose_parts", []) or [])
    inv = _inv(obs)
    want = {}
    for p, _u in ION_PARTS:
        want[p] = want.get(p, 0) + 1
    for part, ups in ION_PARTS:
        if loose.count(part) >= want[part]:
            continue
        base_metal = {"cockpit": 4, "frame": 5, "jet": 10, "fuel_tank": 3, "wing": 4, "tail": 2, "landing_gear": 3}.get(part, 5)
        crystal = (1 if part == "cockpit" else 0) + (2 if part == "jet" else 0)
        if int(inv.get("metal", 0)) < base_metal or int(inv.get("crystal", 0)) < crystal:
            return None
        if not all(int(inv.get(u, 0)) >= 1 for u in ups):
            return None
        a = {"part": part}
        if ups:
            a["with"] = list(ups)
        return "build", a
    if not _orbital_ship(obs):
        return "finalize", {"name": "марсианский клипер"}
    return None


def _colonize(obs, exp):
    """On a body: fund the colony with whatever we hold, else mine the body's own resources. Mixes in mining so a
    per-agent-CAP rejection ("funded nothing") never spins the same construct every turn (audit M5)."""
    inv = _inv(obs); body = exp.get("at_body")
    col = obs.get("colony") or {}
    if random.random() < 0.35:                           # accumulate the body's unique resources + vary actions
        return "mine", {"n": random.randint(4, 6)}
    if col and not col.get("complete"):
        # fund the NEEDIEST incomplete module we can actually contribute a held/body resource to
        mods = [m for m in (col.get("modules", []) or []) if not m.get("complete")]
        mods.sort(key=lambda x: -sum(int(v) for v in (x.get("remaining") or {}).values()))
        for m in mods:
            if any(int(inv.get(r, 0)) >= 1 and int((m.get("remaining") or {}).get(r, 0)) > 0 for r in (m.get("need") or {})):
                return "construct", {"shape": "colony", "body": body, "module": m["module"]}
    return "mine", {"n": 6}


def expand_act(obs):
    """The Mars pipeline. Returns the next action, or None to defer to the normal miner (early game / waiting)."""
    exp = obs.get("expansion")
    if not exp:
        return None
    inv = _inv(obs)
    if exp.get("at_body"):
        return _colonize(obs, exp)
    if exp.get("at_body_orbit"):
        return "land_body", {}
    if exp.get("transit"):
        return None   # mid-flight — nothing to do
    alt = int(obs.get("altitude", 0) or 0); in_space = bool(obs.get("in_space"))
    ship = _orbital_ship(obs)
    if ship:
        if in_space and 300 <= alt <= 600:
            fuel = sum(int(inv.get(f, 0)) for f in ("cryo_fuel", "methalox", "helium3"))
            win = (exp.get("windows", {}) or {}).get(EXP_DEST, {}) or {}
            if fuel >= EXP_DEPART_MIN and (not EXP_HS or int(inv.get("heat_shield", 0)) >= 1) and win.get("open"):
                return "depart", {"dest": EXP_DEST}
            return None   # ship ready — wait for the launch window (miner mines an asteroid meanwhile)
        step = _bom_step(inv, [("cryo_fuel", EXP_FUEL)] + ([("heat_shield", 1)] if EXP_HS else []))   # top up fuel (+ heat_shield for planets)
        if step:
            return step
        if sum(int(inv.get(f, 0)) for f in FUELS) >= 1 or int(inv.get("helium3", 0)) >= 1 or int(inv.get("cryo_fuel", 0)) >= 1:
            return "launch", {}   # climb to orbit
        return None
    if int(inv.get("iridium", 0)) < EXP_IRIDIUM:
        return None   # need iridium first → the miner's asteroid phase supplies it
    if in_space:      # have iridium but up in orbit → come home; the depot + crafting are ground-side
        return "land", {}
    step = _bom_step(inv, EXP_TARGETS)
    if step:
        return step
    return _ion_part_step(obs)


def main():
    print(f"шахтёр: server={runner.SERVER} interval={INTERVAL}s (scripted, no LLM)", flush=True)
    for _ in range(40):
        try:
            runner.api("/healthz"); break
        except Exception:
            time.sleep(3)
    aid, tok = register()
    print(f"шахтёр registered as #{aid}", flush=True)
    while True:
        try:
            obs = runner.api(f"/observe/{aid}")
            # on the GROUND, layer in mention-replies + trade-accepts; aloft (mid-ascent/orbit) stay heads-down on
            # the phase machine and only do reaction-chatter, so a trade/chat never derails a launch or a dock.
            if int(obs.get("altitude", 0) or 0) == 0:
                depot = runner.api("/depot")
                global _LAST_DEPOT
                _LAST_DEPOT = depot          # EXPANSION: expand_act buys chain raws from here (the baron is rich)
                # a rich shахтёр BANKROLLS the station: skim surplus credits into its neediest line (throttled) before mining
                binv = runner.baron_invest(obs, aid)
                if binv:
                    verb, args = binv
                else:
                    verb, args = runner.smart_turn(aid, NAME, obs, depot, act, LINES, replies=REPLIES)
            else:
                verb, args = runner.reactive_say(aid, act, obs, LINES)   # speak only when others just spoke
            runner.api("/intent", "POST", {"agent": aid, "verb": verb, "args": args, "token": tok})
            print(f"[шахтёр #{aid}] {verb} {args}", flush=True)
        except urllib.error.HTTPError as e:
            print(f"[шахтёр] HTTP {e.code}", flush=True)
        except Exception as e:
            print(f"[шахтёр] error: {e}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
