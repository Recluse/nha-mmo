#!/usr/bin/env python3
"""Fast, DB-FREE unit tests for the pure engine/vehicle/crafting logic.

These import the real modules (no Postgres, no tick loop) and assert the pure formulas + design
invariants directly, so they give sub-second feedback and run even where the integration DB is
unreachable. They deliberately cover the exact bug CLASSES the ultracode audits surfaced:
  • vehicles.finalize_stats aggregation — the C1 'gear was never summed' criticals lived HERE,
  • the terraform/colony cap↔min deadlock invariant (ceil(100/cap) <= min funders),
  • the R2 multi-fuel-tier Δv pick, R1 moon-transit->=-Mars, and the Phase-6 location helpers.
"""
import math
import os
import sys

_ENGINE_DIR = os.path.join(os.path.dirname(__file__), "..", "engine")
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)

import engine          # noqa: E402  (imports cleanly with no DB connection)
import vehicles        # noqa: E402
import crafting        # noqa: E402


# ───────────────────────── vehicles.finalize_stats (the C1 gear/fuel_cap aggregation) ─────────────────────────
def _parts(*specs):
    """Build a stats_list from (part, *upgrades) specs via the real part_stats."""
    return [vehicles.part_stats(p, ups) for (p, *ups) in specs]


def test_gear_and_fuel_cap_aggregate():
    """C1 regression guard: landing gear and fuel_cap must SUM across parts (a gearless aggregation
    made every interplanetary depart fail the landing-gear gate)."""
    s = vehicles.finalize_stats(_parts(("frame",), ("landing_gear",), ("landing_gear",),
                                       ("fuel_tank",), ("fuel_tank",)))
    assert s["gear"] == 2, f"gear must sum to 2, got {s['gear']}"
    assert s["fuel_cap"] == 2 * vehicles.PART["fuel_tank"]["fuel_cap"], s["fuel_cap"]
    assert s["mass"] > 0 and "gear" in s and "fuel_cap" in s


def test_no_gear_is_zero_not_missing():
    s = vehicles.finalize_stats(_parts(("frame",), ("cockpit",)))
    assert s["gear"] == 0                    # present-and-zero, so the depart gate reads a real number


def test_a_rover_drives_but_does_not_fly():
    s = vehicles.finalize_stats(_parts(("frame",), ("cockpit",), ("engine",), ("wheel",), ("wheel",)))
    assert s["drives"] is True and s["flies"] is False


def test_control_is_required_to_drive():
    s = vehicles.finalize_stats(_parts(("frame",), ("engine",), ("wheel",)))   # no cockpit → no control
    assert s["controllable"] is False and s["drives"] is False


# ───────────────────────── engine.dv_capacity — the R2 best-fuel-tier rocket equation ─────────────────────────
def _agent(**buffers):
    return {"buffers": dict(buffers)}


def _ship(**attrs):
    base = {"mass": 300, "fuel_cap": 800, "orbital_engine": True, "flies": True}
    base.update(attrs)
    return {"attrs": base}


def test_dv_capacity_none_without_fuel():
    assert engine.dv_capacity(_agent(), _ship(), 100) is None


def test_dv_capacity_r2_picks_best_tier_not_first():
    """R2: a hold of 1 helium3 + lots of cryo_fuel must NOT brick on the first tier — it returns a real
    plan and picks the MAX-Δv option."""
    plan = engine.dv_capacity(_agent(helium3=1, cryo_fuel=600), _ship(), 100)
    assert plan is not None and plan["dv"] > 0
    # it must be at least as good as either single-fuel hold alone
    only_cryo = engine.dv_capacity(_agent(cryo_fuel=600), _ship(), 100)
    only_he = engine.dv_capacity(_agent(helium3=1), _ship(), 100)
    assert plan["dv"] >= max(only_cryo["dv"], only_he["dv"])


def test_dv_capacity_is_capped_by_fuel_cap():
    """More fuel than the tank holds is clamped to fuel_cap (loaded never exceeds the tank)."""
    plan = engine.dv_capacity(_agent(cryo_fuel=100000), _ship(fuel_cap=200), 100)
    assert plan["loaded"] == 200


# ───────────────────────── engine.window_open — launch-window duty cycle ─────────────────────────
def test_window_open_duty_cycle():
    per = engine.SYNODIC["mars"]
    assert engine.window_open("mars", 0) is True
    assert engine.window_open("mars", engine.WINDOW_OPEN - 1) is True
    assert engine.window_open("mars", engine.WINDOW_OPEN) is False
    assert engine.window_open("mars", per) is True            # next period reopens
    assert engine.window_open("earth", 12345) is True         # return leg always open


# ───────────────────────── engine.location / clear_offworld — Phase 6 canonical layer ─────────────────────────
def test_location_reader_all_states():
    assert engine.location({"attrs": {}})["where"] == "earth_ground"
    assert engine.location({"attrs": {"in_space": True, "altitude": 400}})["where"] == "earth_orbit"
    assert engine.location({"attrs": {"transit_to": "mars", "eta_tick": 9}})["where"] == "transit"
    assert engine.location({"attrs": {"transit_to": "venus", "adrift": True}})["where"] == "adrift"
    assert engine.location({"attrs": {"at_body": "phobos"}}) == {"where": "body_surface", "body": "phobos"}
    assert engine.location({"attrs": {"at_body_orbit": "mars"}})["where"] == "body_orbit"


def test_clear_offworld_drops_all_sentinels_but_keeps_earth_tier():
    at = {"at_body": "mars", "at_body_orbit": "x", "transit_to": "y", "depart_tick": 1, "eta_tick": 2,
          "depart_from": "earth", "adrift": True, "adrift_since": 3, "on_moon": True, "docked_to": 7,
          "in_space": True, "altitude": 600}
    engine.clear_offworld(at)
    assert all(k not in at for k in engine._OFFWORLD_KEYS), at
    assert at["in_space"] is True and at["altitude"] == 600      # Earth-tier is managed per-site, NOT here


# ───────────────────────── design invariants (constants) ─────────────────────────
def test_cap_min_no_deadlock_invariant():
    """The self-caught deadlock: if a per-funder cap lets the bill fill before MIN distinct funders can
    join, a module NEVER completes. Guard: ceil(100/cap) <= min funders on every colony + terraform floor."""
    for body, cap in engine.COLONY_CAP.items():
        assert math.ceil(100 / cap) <= engine.COLONY_MIN[body], f"colony {body}: ceil(100/{cap})>{engine.COLONY_MIN[body]}"
    assert math.ceil(100 / engine.TERRAFORM_CAP) <= engine.TERRAFORM_MIN
    assert math.ceil(100 / engine.TERRAFORM_FLAG_CAP) <= engine.TERRAFORM_FLAG_MIN


def test_r1_moons_are_as_far_in_time_as_mars():
    """R1: the moons ride the same Mars Hohmann transfer, so their transit must be >= Mars (else Mars is
    strictly dominated and never chosen)."""
    assert engine.TRANSIT_TICKS["deimos"] >= engine.TRANSIT_TICKS["mars"]
    assert engine.TRANSIT_TICKS["phobos"] >= engine.TRANSIT_TICKS["mars"]


def test_dv_need_ordering_moons_cheapest_venus_dearest():
    assert engine.DV_NEED["deimos"] < engine.DV_NEED["mars"] < engine.DV_NEED["venus"]
    assert engine.DV_NEED["phobos"] < engine.DV_NEED["mars"]


# ───────────────────────── crafting.combine — key expansion recipes are Earth-craftable ─────────────────────────
def test_expansion_prep_items_are_earth_craftable():
    """Audit L3: heat_shield / acid_skin / hydrogen must resolve from Earth-available inputs (sulfur + oil
    are on the map), so an agent can PACK them before launch."""
    assert crafting.combine({"superalloy": 1, "composite": 1}) == "heat_shield"
    assert crafting.combine({"sulfur": 1, "rubber": 1}) == "acid_skin"
    assert crafting.combine({"water": 1, "motor": 1}) == "hydrogen"


def test_ion_thruster_resolves_from_its_chain_tip():
    assert crafting.combine({"iridium": 1, "motor": 1, "chip": 1}) == "ion_thruster"
