#!/usr/bin/env python3
"""Season 3 crafting collision proof (SEASON3-PLAN.md STEP 1).

Asserts that the new combat/tech items resolve from the intended ingredient mixes, that the
magnet/slug and casing/gun collisions are GONE, and that every season-2 recipe still resolves to
its original output (no existing recipe newly turns into a weapon/superalloy/etc).

Pure-function test — imports engine.crafting directly, no engine/server/DB needed.
Run:  pytest tests/test_season3_crafting.py    (or:  python tests/test_season3_crafting.py)
"""
import importlib.util
import itertools
import os

_ENG = os.path.join(os.path.dirname(__file__), "..", "engine")
_spec = importlib.util.spec_from_file_location("crafting", os.path.join(_ENG, "crafting.py"))
crafting = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crafting)
combine = crafting.combine

# Items that did not exist before Season 3 — no season-2 ingredient set may resolve into one
# UNLESS that set legitimately includes a Season-3 crafted intermediate (steel is season-2, but
# the intended new recipes are explicitly enumerated below).
S3_ITEMS = {
    "gunpowder", "slug", "barrel", "kinetic_gun", "energy_cell",
    "energy_weapon", "bomb", "superalloy", "cryo_fuel", "ion_thruster",
}


# ---------------------------------------------------------------------------
# 1. COLLISION-PROOF SET — the listed mixes resolve to the intended items
#    (SEASON3-PLAN.md lines 53 + 109).
# ---------------------------------------------------------------------------
def test_iron_alone_still_makes_magnet():
    # iron dense=6 < slug's dense>=8 threshold, so slug rejects and magnet still wins.
    assert combine({"iron": 1}) == "magnet"
    assert combine({"iron": 2}) == "magnet"


def test_iron_carbon_still_makes_steel():
    assert combine({"iron": 1, "carbon": 1}) == "steel"


def test_steel_iron_makes_slug():
    # steel(dense 8, hardness 10) + iron, no heat, <=2 ings -> slug (the kinetic ammo).
    assert combine({"steel": 1, "iron": 1}) == "slug"


def test_barrel_slug_gunpowder_makes_kinetic_gun():
    # the three crafted intermediates carry the unique gunbody/projectile/explosive tags.
    assert combine({"barrel": 1, "slug": 1, "gunpowder": 1}) == "kinetic_gun"


def test_casing_motor_gunpowder_is_not_a_gun():
    # the OLD casing/gun collision: no 'gunbody' (casing is not a barrel) + no 'projectile'
    # => must NOT resolve to kinetic_gun.
    assert combine({"casing": 1, "motor": 1, "gunpowder": 1}) != "kinetic_gun"


def test_gunpowder_casing_makes_bomb():
    # explosive (gunpowder) + container (casing) + reactive (gunpowder) -> bomb.
    assert combine({"gunpowder": 1, "casing": 1}) == "bomb"


def test_steel_coal_stays_steel():
    # superalloy needs dense>=7 AND n_metals>=2 AND heat; a single steel(+coal) has n_metals==1
    # so superalloy rejects and steel still wins.
    assert combine({"steel": 1, "coal": 1}) == "steel"


def test_iridium_titanium_coal_makes_superalloy():
    # two dense metals (iridium dense 9, titanium dense 5 -> mx dense 9>=7) + heat -> superalloy.
    assert combine({"iridium": 1, "titanium": 1, "coal": 1}) == "superalloy"


def test_ice_oil_makes_cryo_fuel():
    # frozen (ice) + energy (oil), no metal -> cryo_fuel.
    assert combine({"ice": 1, "oil": 1}) == "cryo_fuel"


# ---------------------------------------------------------------------------
# 2. INTENDED NEW RECIPES that the plan's economy/verbs rely on.
# ---------------------------------------------------------------------------
def test_gunpowder_from_sulfur_carbon_heat():
    # acid_former (sulfur) + carbon + heat (coal), no metal -> gunpowder.
    assert combine({"sulfur": 1, "carbon": 1, "coal": 1}) == "gunpowder"


def test_energy_cell_from_battery_and_lens():
    # stores_power (battery) + refraction (lens) -> energy_cell (beam ammo).
    assert combine({"battery": 1, "lens": 1}) == "energy_cell"


def test_energy_weapon_from_cell_lens_conductor():
    # charged (energy_cell) + refraction (lens/crystal) + conductivity (copper) -> energy_weapon.
    assert combine({"energy_cell": 1, "crystal": 1, "copper": 1}) == "energy_weapon"


def test_barrel_from_steel_and_casing():
    # hollow (casing) + hardness>=10 (steel) + a metal -> barrel (not a bare casing).
    assert combine({"steel": 1, "casing": 1}) == "barrel"


def test_ion_thruster_from_fusion_motor_semiconductor():
    # fusion (helium3) + power (motor) + semiconductor (silicon/chip) -> ion_thruster.
    assert combine({"helium3": 1, "motor": 1, "silicon": 1}) == "ion_thruster"


def test_dense_slug_from_iridium():
    # iridium alone: dense 9, hardness 10, 1 metal, no heat, 1 ing -> slug.
    assert combine({"iridium": 1}) == "slug"


# ---------------------------------------------------------------------------
# 3. OLD RECIPES still resolve correctly (every canonical season-2 recipe).
# ---------------------------------------------------------------------------
SEASON2_CANON = [
    ({"iron": 1, "copper": 1, "electrolyte": 1}, "battery"),
    ({"magnet": 1, "copper": 1, "battery": 1}, "motor"),
    # iron is magnetic+conductive, so the (earlier) motor rule wins over electromagnet — this is
    # the unchanged season-2 behavior (motor's predicate subsumes electromagnet's for plain iron).
    ({"iron": 1, "copper": 1, "battery": 1}, "motor"),
    ({"silicon": 1, "crystal": 1, "copper": 1}, "solar_cell"),
    ({"silicon": 1, "copper": 1}, "chip"),
    ({"aluminum": 1, "carbon": 1}, "composite"),
    ({"iron": 1, "carbon": 1}, "steel"),
    # two plain (low-dense, non-light, non-carbon) metals + a carbon-free heat source -> alloy.
    # superalloy rejects here: iron/copper dense < 7, so alloy still wins (unchanged from season-2).
    ({"iron": 1, "copper": 1, "oil": 1}, "alloy"),
    # aluminum's light + coal's carbon makes the earlier composite rule win — unchanged season-2 behavior.
    ({"iron": 1, "aluminum": 1, "coal": 1}, "composite"),
    ({"ore": 1, "coal": 1}, "metal"),
    ({"sulfur": 1, "water": 1}, "acid"),
    ({"salt": 1, "water": 1}, "electrolyte"),
    ({"brine": 1, "coal": 1}, "salt"),
    ({"water": 1, "coal": 1}, "steam"),
    ({"oil": 1, "carbon": 1}, "plastic"),
    ({"sulfur": 1, "plastic": 1}, "rubber"),
    ({"wire": 1, "plastic": 1}, "insulated_wire"),
    ({"plastic": 1, "iron": 1}, "casing"),
    ({"iron": 1}, "magnet"),
    ({"silicon": 1, "coal": 1}, "glass"),
    ({"crystal": 1}, "lens"),
    ({"oil": 1, "metal": 1}, "engine"),   # energy (oil) + hard non-magnetic metal frame -> engine
    ({"copper": 1}, "wire"),
    ({"oil": 1, "copper": 1}, "bearing"),  # a metal + oil, no magnetic dominance -> bearing
]


def test_season2_canonical_recipes_unchanged():
    for ings, want in SEASON2_CANON:
        assert combine(ings) == want, f"{ings} -> {combine(ings)} (expected {want})"


# ---------------------------------------------------------------------------
# 4. EXHAUSTIVE non-collision: over ALL season-2 ingredient sets (size 1..3),
#    no mix may resolve into a Season-3 item EXCEPT via the intended tags.
#    Concretely: every Season-3 result must be justified by a Season-3-defining
#    property in the mixture (so we never silently hijack a season-2 recipe into
#    a weapon). This is the machine-checkable form of plan line 109.
# ---------------------------------------------------------------------------
def _props(k):
    return crafting.PROPS.get(k) or crafting.ITEM_PROPS.get(k) or {}


# Season-2 ingredient universe (exclude the new raws/items).
SEASON2_INGS = [
    k for k in list(crafting.PROPS) + list(crafting.ITEM_PROPS)
    if k not in (S3_ITEMS | {"titanium", "ice", "iridium", "nickel"})
]


def _justifies_s3(ings, result):
    """Is `result` (a Season-3 item) legitimately reachable from this mix?
    Each Season-3 recipe is defined by a distinctive property combination; assert the mix
    actually carries it, so we know no season-2 recipe was hijacked by accident."""
    has = lambda p: any(_props(k).get(p, 0) > 0 for k in ings)
    mx = lambda p: max([_props(k).get(p, 0) for k in ings], default=0)
    n_metals = len({k for k in ings if _props(k).get("metal")})
    heat = has("flammable") or has("energy")
    if result == "gunpowder":
        return has("acid_former") and has("carbon") and heat and n_metals == 0
    if result == "kinetic_gun":
        return has("gunbody") and has("projectile") and has("explosive")
    if result == "energy_weapon":
        return has("charged") and has("refraction") and has("conductivity")
    if result == "energy_cell":
        return has("stores_power") and (has("refraction") or mx("energy") >= 9)
    if result == "bomb":
        return has("explosive") and has("container") and has("reactive")
    if result == "barrel":
        return has("hollow") and mx("hardness") >= 10 and n_metals >= 1
    if result == "slug":
        return mx("dense") >= 8 and mx("hardness") >= 9 and n_metals >= 1 and not heat
    if result == "superalloy":
        return mx("dense") >= 7 and n_metals >= 2 and heat
    if result == "cryo_fuel":
        return has("frozen") and has("energy") and n_metals == 0
    if result == "ion_thruster":
        return has("fusion") and has("power") and has("semiconductor")
    return False


def test_no_season2_mix_silently_hijacked_into_a_weapon():
    bad = []
    for r in (1, 2, 3):
        for combo in itertools.combinations(SEASON2_INGS, r):
            ings = {k: 1 for k in combo}
            got = combine(ings)
            if got in S3_ITEMS and not _justifies_s3(ings, got):
                bad.append((combo, got))
    assert not bad, f"unjustified Season-3 results: {bad[:20]}"


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
