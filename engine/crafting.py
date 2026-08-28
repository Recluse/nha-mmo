#!/usr/bin/env python3
"""NHA-MMO — emergent crafting from physics.

Resources carry integer physical-property tags; `combine` aggregates a mixture's properties and
matches physics PATTERNS (not fixed recipes) to form new items. Crafted items are themselves
resources with properties, so they compose into a tech tree. Fully deterministic (no LLM referee).
See ../CRAFTING-PHYSICS.md.
"""

# raw resource -> physical properties (0..10)
PROPS = {
    "copper":   {"metal": 1, "conductivity": 9, "ductility": 7, "reactivity": 3},
    "iron":     {"metal": 1, "hardness": 7, "magnetic": 8, "conductivity": 5, "reactivity": 4, "dense": 6},
    "aluminum": {"metal": 1, "conductivity": 7, "light": 8, "reactivity": 6, "ductility": 6},
    "carbon":   {"flammable": 9, "energy": 8, "hardness": 5, "carbon": 9},
    "silicon":  {"semiconductor": 8, "hardness": 6},
    "crystal":  {"refraction": 9, "hardness": 8, "insulator": 7},
    "oil":      {"flammable": 8, "energy": 9, "lubricant": 8},
    "water":    {"solvent": 8, "coolant": 6},
    "salt":     {"ionic": 9, "soluble": 9},
    "sulfur":   {"reactive": 8, "acid_former": 7},
    "coal":     {"flammable": 9, "energy": 9, "hardness": 4, "carbon": 6},  # hot carbonaceous fuel — smelt/steel/heat
    "wood":     {"flammable": 7, "energy": 5, "hardness": 3, "light": 5},  # chopped from trees; fuel + light material
    "ore":      {"ore": 8, "hardness": 3},                            # raw ore — smelt with fuel to get metal
    "brine":    {"solvent": 6, "soluble": 8},                         # sea water — boil off with heat to get salt
    "helium3":  {"flammable": 10, "energy": 10, "fusion": 1, "light": 9},  # lunar super-fuel — mined on the Moon, supercharges launch
    "regolith": {"hardness": 5, "lunar_moldable": 4, "dusty": 1},         # lunar soil — Moon-base material; UNIQUE lunar_moldable tag (NOT generic moldable) so it can't substitute for plastic/casing in Earth recipes (casing/insulated_wire/rubber/medkit)
    # --- season 3 frontier + orbital raws (dense = heavy-metal tag that splits slugs from magnets) ---
    "titanium": {"metal": 1, "hardness": 9, "light": 7, "dense": 5},      # tundra-frontier light-yet-hard metal — feeds superalloy
    "ice":      {"coolant": 9, "solvent": 4, "frozen": 1},                # tundra-frontier frozen volatile — feeds cryo_fuel
    "iridium":  {"metal": 1, "hardness": 10, "dense": 9, "fusion": 1},    # apex orbital metal (asteroid-only) — superalloy + dense slug
    "nickel":   {"metal": 1, "hardness": 6, "magnetic": 5, "dense": 5},   # asteroid metal — magnetic + dense
    # --- season 3 botany: gathered plants (renewable, the medicine/chemistry branch) ---
    # 'organic'/'medicinal'/'antiseptic'/'potent'/'toxic' are medicine-only tags — no season-2/3
    # mix carries them, so the medicine RULES below can never hijack an existing recipe.
    "herb":     {"organic": 8, "medicinal": 6, "soluble": 6},             # plains/forest — base medicinal plant
    "lichen":   {"organic": 6, "antiseptic": 7, "medicinal": 4, "frost": 3},  # tundra frontier — antiseptic, cures wounds
    "fungus":   {"organic": 7, "potent": 8, "toxic": 5, "soluble": 4},    # shadow/cave biome — potent but toxic
    "algae":    {"organic": 9, "coolant": 4, "soluble": 7},               # near water — soluble organic, mild
}

# crafted item -> its own properties (so items can be ingredients in further combines → tech tree)
ITEM_PROPS = {
    "wire":          {"metal": 1, "conductivity": 9, "shaped": 1},
    "electrolyte":   {"ionic": 8, "solvent": 7},
    "battery":       {"stores_power": 1, "energy": 8},
    "magnet":        {"magnetic": 9, "metal": 1},
    "electromagnet": {"magnetic": 10, "powered": 1},
    "motor":         {"power": 1, "kinetic": 1},
    "alloy":         {"metal": 1, "hardness": 9, "light": 5},
    "glass":         {"insulator": 8, "refraction": 5, "transparent": 1},
    "lens":          {"refraction": 10, "focus": 1},
    "chip":          {"semiconductor": 9, "logic": 1},
    "solar_cell":    {"passive_energy": 1, "semiconductor": 5},
    "engine":        {"power": 1, "burns_fuel": 1},
    "steam":         {"energy": 5, "pressure": 6, "hot": 1},
    "metal":         {"metal": 1, "hardness": 6, "dense": 4},              # smelted from ore — the vehicle build material
    "steel":         {"metal": 1, "hardness": 10, "magnetic": 3, "dense": 8},  # iron + carbon, much harder (dense enough to be a slug)
    "acid":          {"acid_former": 9, "reactive": 7, "solvent": 4},      # sulfur + water — corrosive reagent
    "bearing":       {"lubricant": 8, "metal": 1, "low_friction": 1},      # a metal + oil — low-friction part
    "plastic":       {"insulator": 7, "moldable": 8, "light": 6},          # oil + carbon — polymer (insulation/casings)
    "insulated_wire":{"conductivity": 9, "insulator": 8, "shaped": 1},     # wire + plastic — safe conductor
    "casing":        {"hollow": 1, "light": 6, "insulator": 5, "container": 1},  # molded plastic + metal frame — shell/tank
    "composite":     {"light": 9, "hardness": 8, "shaped": 1},              # light metal + carbon — carbon-fibre (strong + light)
    "rubber":        {"elastic": 1, "insulator": 6, "grip": 8, "moldable": 4},  # sulfur + plastic — vulcanised (tyres, seals)
    # --- season 3 combat + tech items (unique tags gunbody/projectile/explosive/charged keep recipes non-colliding) ---
    "gunpowder":     {"flammable": 9, "reactive": 9, "energy": 7, "explosive": 4},   # acid_former + carbon, fired — the propellant
    "slug":          {"metal": 1, "hardness": 9, "kinetic": 1, "dense": 9, "projectile": 1},  # dense hard metal shot — kinetic ammo
    "barrel":        {"metal": 1, "hardness": 8, "shaped": 1, "hollow": 1, "gunbody": 1},     # hollow hard-metal tube — the gun body
    "kinetic_gun":   {"weapon": 1, "kinetic": 1, "hardness": 7, "firearm": 1},        # barrel + slug + gunpowder — fires slugs
    "energy_cell":   {"stores_power": 1, "energy": 9, "charged": 1},                  # battery + high-energy/lens charge — beam ammo
    "energy_weapon": {"weapon": 1, "energy": 1, "beam": 1, "refraction": 6},          # charged cell + lens + conductor — beam gun
    "bomb":          {"weapon": 1, "explosive": 1, "unstable": 1, "energy": 8},       # explosive in a container — single-use AoE
    "superalloy":    {"metal": 1, "hardness": 12, "light": 6, "heat_proof": 1, "dense": 7},  # 2 dense metals melted — apex frame
    "cryo_fuel":     {"energy": 9, "coolant": 8, "frozen": 1},                        # ice + energy source — cold rocket fuel
    "ion_thruster":  {"power": 2, "thrust_field": 1, "light": 1},                     # fusion + power + semiconductor — orbital drive
    "observatory":   {"instrument": 1, "optics": 1, "logic": 1},                      # lens + chip — a forecasting instrument (unlocks obs.forecast: the world's deterministic dynamics, computed ahead)
    "radar":         {"instrument": 1, "sensor": 1, "detects": 1},                     # a finished magnet + chip — a sensor that WIDENS sight (observe.vision radius). No 'logic'/'focus'/'semiconductor' tag on purpose, so a radar can't stand in for a chip/lens; its 'instrument' tag is what the radar rule's `not instrument` guard reads to refuse consuming a finished instrument (observatory/radar) into another radar
    # --- season 3 medicine + chemistry (HP healing branch; 'heal' = HP restored on use, capped HP_MAX engine-side) ---
    # unique tags (organic/medicinal/antiseptic/potent/heal/cures_toxin/topical/buff/revive) keep these
    # recipes off every existing season-2/3 mix (none of which carries an organic/medicinal property).
    "extract":   {"medicinal": 7, "soluble": 8, "organic": 6},                        # plant steeped in water — concentrated base
    "tincture":  {"medicinal": 8, "potent": 6, "antiseptic": 5},                      # extract fixed with salt/acid — strong base medicine
    "salve":     {"heal": 15, "antiseptic": 8, "topical": 1},                         # cooked herb/lichen — mild cheap early-game heal
    "antidote":  {"heal": 8, "antiseptic": 6},                                        # antiseptic plant + acid/salt — a mild antiseptic heal
    "stimpack":  {"heal": 35, "buff": 1, "potent": 8},                                # tincture + a charge — fast heal + short buff
    "medkit":    {"heal": 60, "revive": 1},                                           # salve + tincture in a casing — strong; can revive
}

# human-readable note per rule (for the Codex / agent hints)
RULE_NOTE = {
    "battery": "2 different metals (reactivity gap) + an electrolyte",
    "motor": "a magnet + a conductor + a battery",
    "electromagnet": "magnetic metal + conductor + battery",
    "solar_cell": "semiconductor + insulator + conductor",
    "chip": "semiconductor + conductor",
    "alloy": "2 metals melted with heat",
    "electrolyte": "a solvent + something ionic/acidic (salt or sulfur + water)",
    "magnet": "a magnetic metal, worked",
    "glass": "silicon or crystal + heat",
    "lens": "a highly refractive material",
    "observatory": "a lens + a chip — a forecasting instrument (see the storm's future track in observe.forecast)",
    "radar": "a finished magnet (or electromagnet) + a chip — a sensor that WIDENS your sight (bigger nearby_agents/threat radius in observe.vision)",
    "engine": "fuel (energy) + a hard metal frame",
    "wire": "a ductile conductor metal (copper/aluminum), drawn out",
    "acid": "sulfur + water — a corrosive reagent",
    "bearing": "a metal + oil — a low-friction part",
    "steam": "water heated by a fuel (coal / wood / oil / carbon)",
    "metal": "raw ore smelted with a fuel (ore + coal / wood / oil)",
    "steel": "iron (a metal) + carbon, smelted hard",
    "plastic": "oil + carbon — a polymer (insulation, light casings)",
    "insulated_wire": "wire + plastic — a safe insulated conductor",
    "casing": "plastic + a metal frame — a shell / container / tank",
    "composite": "a light metal (aluminium) + carbon — carbon-fibre, strong and light",
    "rubber": "sulfur + plastic — vulcanised rubber (tyres, seals)",
    "salt": "boil brine (sea water) dry with a fuel — sea salt",
    "gunpowder": "an acid-former (sulfur) + carbon, fired with heat — propellant",
    "slug": "a dense hard metal (steel/iridium/titanium), no heat — kinetic shot",
    "barrel": "a hollow very-hard metal body (steel/superalloy + casing) — the gun body",
    "kinetic_gun": "a barrel + a slug + gunpowder — a firearm that fires slugs",
    "energy_cell": "a power store (battery) charged with a lens or a high-energy fuel — beam ammo",
    "energy_weapon": "a charged cell + a lens (refraction) + a conductor — a beam weapon",
    "bomb": "an explosive (gunpowder) packed in a container (casing) — a single-use charge",
    "superalloy": "two dense metals melted with heat — an apex frame material",
    "cryo_fuel": "ice (frozen) + an energy source, no metal — cold rocket fuel",
    "ion_thruster": "a fusion fuel (helium3/iridium) + a motor (power) + a semiconductor — orbital drive",
    "extract": "a plant (herb/lichen/fungus/algae) steeped in a solvent (water) — a base medicine",
    "tincture": "an extract fixed with salt or acid — a concentrated base medicine",
    "salve": "a medicinal plant + water, cooked with heat — a mild topical heal",
    "antidote": "an antiseptic plant (lichen/fungus) + acid or salt — a mild antiseptic heal",
    "stimpack": "a tincture + a power charge (battery) — a fast heal plus a short buff",
    "medkit": "a salve + a tincture packed in a casing/plastic — a strong heal that can revive",
}


def _props(k):
    return PROPS.get(k) or ITEM_PROPS.get(k) or {}


def aggregate(ings):
    """ings: dict {resource: qty}. Summarize the mixture's physics."""
    metals = sorted({k for k in ings if _props(k).get("metal")})
    reacts = [_props(m).get("reactivity", 0) for m in metals]
    has = lambda p: any(_props(k).get(p, 0) > 0 for k in ings)
    mx = lambda p: max([_props(k).get(p, 0) for k in ings], default=0)
    return {
        "ings": ings,
        "n_metals": len(metals),
        "react_spread": (max(reacts) - min(reacts)) if len(reacts) >= 2 else 0,
        "electrolyte": has("solvent") and (has("ionic") or has("acid_former")),
        "heat": has("flammable") or has("energy"),
        "has": has, "mx": mx,
    }


# (rule_key, predicate) — first match wins; ordered specific (composite) -> primitive
RULES = [
    ("battery",       lambda a: a["n_metals"] >= 2 and a["react_spread"] >= 1 and a["electrolyte"]),
    # electromagnet BEFORE motor. REGRESSION FIX: the old `6 <= mx(magnetic) < 9` window caught plain
    # iron (magnetic 8) so {iron,copper,battery} wrongly made an electromagnet instead of a motor. An
    # electromagnet needs a FINISHED electromagnet-grade core (mx magnetic >= 10 — only the crafted
    # `electromagnet` item, magnetic 10, reaches it). Raw iron (8) AND a finished permanent `magnet` (9)
    # both fall through to the motor rule below, restoring the season-2 behavior the tests assert.
    ("electromagnet", lambda a: a["mx"]("magnetic") >= 10 and a["has"]("conductivity") and a["has"]("stores_power")),
    ("motor",         lambda a: a["has"]("magnetic") and a["has"]("conductivity") and a["has"]("stores_power")),  # raw iron (8) or a permanent magnet (9) + conductor + power
    ("solar_cell",    lambda a: a["has"]("semiconductor") and a["has"]("insulator") and a["has"]("conductivity")),
    ("chip",          lambda a: a["has"]("semiconductor") and a["has"]("conductivity")),
    # --- season 3 combat + tech (finished items win; each predicate tightened to NOT shadow a season-2 recipe) ---
    ("gunpowder",     lambda a: a["has"]("acid_former") and a["has"]("carbon") and a["heat"] and a["n_metals"] == 0),
    ("kinetic_gun",   lambda a: a["has"]("gunbody") and a["has"]("projectile") and a["has"]("explosive")),  # barrel+slug+gunpowder via their unique crafted tags
    ("energy_weapon", lambda a: a["has"]("charged") and a["has"]("refraction") and a["has"]("conductivity")),  # energy_cell + lens + conductor
    ("energy_cell",   lambda a: a["has"]("stores_power") and (a["has"]("refraction") or a["mx"]("energy") >= 9)),  # battery charged by a lens or high-energy fuel
    ("bomb",          lambda a: a["has"]("explosive") and a["has"]("container") and a["has"]("reactive")),  # gunpowder + casing
    ("barrel",        lambda a: a["has"]("hollow") and a["mx"]("hardness") >= 10 and a["n_metals"] >= 1),  # needs steel(10)/superalloy, not a bare casing
    ("slug",          lambda a: a["mx"]("dense") >= 8 and a["mx"]("hardness") >= 9 and a["n_metals"] >= 1 and not a["heat"] and len(a["ings"]) <= 2),  # steel/iridium/titanium shot; bare iron (dense 6) does NOT match -> magnet still wins
    ("superalloy",    lambda a: a["mx"]("dense") >= 7 and a["n_metals"] >= 2 and a["heat"] and not a["electrolyte"]),  # two dense metals melted
    ("cryo_fuel",     lambda a: a["has"]("frozen") and a["has"]("energy") and a["n_metals"] == 0),  # ice + an energy source
    ("ion_thruster",  lambda a: a["has"]("fusion") and a["has"]("power") and a["has"]("semiconductor")),  # fusion fuel + motor + chip/silicon
    ("observatory",   lambda a: a["has"]("focus") and a["has"]("logic")),  # lens(focus) + chip(logic) — a forecasting instrument. Unique tags → no collision; BEFORE lens/glass so lens+chip resolves here
    # radar: a FINISHED magnet (9) / electromagnet (10) + a CHIP. Raw iron is magnetic 8 so it can't reach this
    # ({iron,chip} still smelts to a chip). The chip is pinned by BOTH `logic` AND `semiconductor`: the observatory
    # also carries `logic` (with `optics`, not `focus`), so `logic` alone would let a magnet+observatory become a
    # radar with no chip — requiring `semiconductor` (which the observatory lacks) blocks that, and `not instrument`
    # additionally bars any finished instrument (observatory OR another radar) from being consumed into one. Before
    # this rule {magnet,chip} fell through to the bottom `magnet` rule (re-forging the magnet, wasting the chip).
    ("radar",         lambda a: a["mx"]("magnetic") >= 9 and a["has"]("logic") and a["has"]("semiconductor") and not a["has"]("instrument")),
    # --- season 3 medicine (ABOVE the generic acid/electrolyte/steam/salt rules so they win; every
    #     predicate REQUIRES a medicine-only tag (organic/medicinal/antiseptic/potent/heal) so it can
    #     never resolve a season-2/3 mix — those carry zero organic/medicinal properties) ---
    ("medkit",        lambda a: a["has"]("heal") and a["has"]("medicinal") and (a["has"]("container") or a["has"]("moldable"))),  # salve(heal) + tincture(medicinal) + casing/plastic
    ("stimpack",      lambda a: a["has"]("medicinal") and a["has"]("potent") and a["has"]("stores_power")),  # tincture + a charge (battery)
    ("tincture",      lambda a: a["mx"]("medicinal") >= 7 and (a["has"]("ionic") or a["has"]("acid_former"))),  # extract (medicinal>=7) fixed with salt/acid
    ("antidote",      lambda a: (a["has"]("antiseptic") or a["has"]("toxic")) and (a["has"]("ionic") or a["has"]("acid_former"))),  # lichen/fungus + salt/acid
    ("salve",         lambda a: a["has"]("medicinal") and a["has"]("solvent") and a["heat"]),  # a medicinal plant + water, cooked
    ("extract",       lambda a: a["has"]("organic") and a["has"]("solvent")),  # any plant + a solvent (water) — the base, heat optional
    ("composite",     lambda a: a["mx"]("light") >= 6 and a["has"]("carbon") and a["n_metals"] >= 1),
    ("steel",         lambda a: a["n_metals"] >= 1 and a["has"]("carbon") and a["heat"] and not a["electrolyte"]),
    ("alloy",         lambda a: a["n_metals"] >= 2 and a["heat"] and not a["electrolyte"]),
    ("metal",         lambda a: a["has"]("ore") and a["heat"]),
    ("acid",          lambda a: a["has"]("solvent") and a["has"]("acid_former") and a["n_metals"] == 0),
    ("electrolyte",   lambda a: a["has"]("solvent") and (a["has"]("ionic") or a["has"]("acid_former")) and a["n_metals"] == 0),
    ("salt",          lambda a: a["has"]("soluble") and a["has"]("solvent") and a["heat"] and a["n_metals"] == 0 and not a["has"]("ionic")),
    ("steam",         lambda a: a["has"]("coolant") and a["heat"] and a["n_metals"] == 0 and not a["electrolyte"]),
    ("plastic",       lambda a: a["has"]("lubricant") and a["has"]("carbon") and a["n_metals"] == 0),
    ("rubber",        lambda a: a["has"]("reactive") and a["has"]("moldable") and a["n_metals"] == 0),
    ("insulated_wire",lambda a: a["has"]("shaped") and a["has"]("moldable")),
    ("casing",        lambda a: a["has"]("moldable") and a["n_metals"] >= 1),
    ("magnet",        lambda a: a["mx"]("magnetic") >= 6 and a["n_metals"] >= 1 and len(a["ings"]) <= 2),
    ("glass",         lambda a: (a["has"]("semiconductor") or a["has"]("refraction")) and a["heat"]),
    ("lens",          lambda a: a["mx"]("refraction") >= 8 and not a["heat"]),
    ("engine",        lambda a: a["has"]("energy") and a["mx"]("hardness") >= 6 and a["n_metals"] >= 1),
    ("bearing",       lambda a: a["has"]("metal") and a["has"]("lubricant")),
    ("wire",          lambda a: a["mx"]("conductivity") >= 6 and a["has"]("ductility") and a["has"]("metal")),
]


def combine(ings):
    """ings: {resource: qty>0}. Returns the matched rule_key, or None (inert mixture)."""
    a = aggregate(ings)
    for key, pred in RULES:
        try:
            if pred(a):
                return key
        except Exception:
            pass
    return None
