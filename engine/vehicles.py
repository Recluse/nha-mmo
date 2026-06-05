#!/usr/bin/env python3
"""NHA-MMO — сборка машин: схлопывание графа деталей в одно тело с агрегатными ТТХ.

Реализует лёгкую схему из ../PHYSICS-VEHICLES.md: суммируем целочисленные константы деталей →
замкнутые формулы решают, едет/летит ли и как быстро. Без непрерывной физики и джоинт-солвера.

Run:  PG_DSN=... python vehicles.py   # собирает демо: авто + 2 самолёта, финализирует, печатает + пишет в БД.
"""
import os, math
import psycopg2
from psycopg2.extras import Json

DSN = os.environ.get("PG_DSN", "host=127.0.0.1 dbname=nhamoo user=postgres")

# деталь -> целочисленные физ-константы
PART = {
    "frame":        {"mass": 80,  "strength": 200},
    "panel":        {"mass": 30,  "strength": 60},
    "engine":       {"mass": 150, "power": 200},
    "wheel":        {"mass": 25,  "drive": 1, "traction": 120},
    "propeller":    {"mass": 40,  "thrust_pp": 1},   # тяга на единицу мощности двигателя
    "jet":          {"mass": 120, "thrust": 400},
    "wing":         {"mass": 50,  "wing_area": 12},
    "tail":         {"mass": 20,  "wing_area": 3, "maneuver": 5},
    "cockpit":      {"mass": 40,  "control": 1, "maneuver": 3},
    "fuel_tank":    {"mass": 30,  "fuel_cap": 200},
    "landing_gear": {"mass": 35,  "gear": 1},
}
# крафт: материалы, расходуемые на ОДНУ деталь (целочисленно)
BUILD_COST = {
    "frame": {"metal": 5}, "panel": {"metal": 3}, "wheel": {"metal": 2},
    "engine": {"metal": 8, "crystal": 1}, "propeller": {"metal": 4},
    "jet": {"metal": 10, "crystal": 2}, "wing": {"metal": 4}, "tail": {"metal": 2},
    "cockpit": {"metal": 4, "crystal": 1}, "fuel_tank": {"metal": 3}, "landing_gear": {"metal": 3},
}

# crafted items that UPGRADE a part (consumed 1 each, on top of the base cost) → flat stat bonuses.
# This is what ties the physics-crafting tree to vehicles: steel/alloy/motor/chip/glass/bearing matter.
PART_UPGRADES = {
    "frame":     {"steel": {"strength": 150}, "alloy": {"strength": 80, "mass": -30}},
    "wheel":     {"alloy": {"traction": 60, "mass": -8}, "bearing": {"traction": 40}},
    "engine":    {"engine": {"power": 150}, "motor": {"power": 100}, "steel": {"power": 60}},
    "wing":      {"alloy": {"wing_area": 6, "mass": -15}},
    "tail":      {"alloy": {"maneuver": 4, "mass": -8}},
    "propeller": {"bearing": {"thrust_pp": 1}, "alloy": {"mass": -12}},
    "jet":       {"steel": {"thrust": 150, "mass": 20}},
    "cockpit":   {"chip": {"maneuver": 5, "control": 1}, "glass": {"maneuver": 2}, "lens": {"control": 1}},
    "fuel_tank": {"steel": {"fuel_cap": 120}},
}


def part_stats(part, upgrades=()):
    """Base PART stats with crafted-item upgrades applied (flat deltas)."""
    st = dict(PART[part])
    for u in upgrades:
        for k, dv in PART_UPGRADES.get(part, {}).get(u, {}).items():
            st[k] = st.get(k, 0) + dv
    return st

K_V, K_LIFT, G = 90, 1, 10   # коэффициенты-болванка под тюнинг


def finalize(parts):
    """parts: список имён деталей (без апгрейдов) → ТТХ (back-compat wrapper)."""
    return finalize_stats([PART[p] for p in parts])


def finalize_stats(stats_list):
    """stats_list: список per-part stat-словарей (возможно с апгрейдами) → агрегатные ТТХ + вердикт."""
    s = lambda k: sum(d.get(k, 0) for d in stats_list)
    mass      = s("mass")
    power     = s("power")
    traction  = s("traction")
    n_wheels  = sum(1 for d in stats_list if d.get("drive"))
    wing_area = s("wing_area")
    control   = s("control")
    drag      = max(1, mass // 20)                 # лобовое сопротивление (упрощённо ∝ масса)
    drive     = min(power, traction)               # тяга колёс, ограничена сцеплением шин
    thrust    = s("thrust") + sum(d.get("thrust_pp", 0) for d in stats_list) * power
    lift_coef = wing_area * K_LIFT
    v_ground  = math.isqrt(K_V * drive  // drag) if drive  else 0
    v_air     = math.isqrt(K_V * thrust // drag) if thrust else 0
    drives    = bool(control and n_wheels >= 1 and drive > 0)
    flies     = bool(control and lift_coef * v_air * v_air >= G * mass)   # подъёмная на v_max ≥ вес
    return dict(mass=mass, drag=drag, power=power, drive_force=drive, thrust=thrust,
                wing_area=wing_area, lift_coef=lift_coef, controllable=bool(control),
                v_ground=v_ground, v_air=v_air, drives=drives, flies=flies)


def store_vehicle(conn, name, parts, st):
    cur = conn.cursor()
    cur.execute("INSERT INTO entities(type,x,y,buffers,attrs) VALUES('vehicle',0,0,'{}',%s) RETURNING id",
                (Json({"name": name, "parts": parts, **st}),))
    return cur.fetchone()[0]


DEMOS = {
    "car":       ["frame", "wheel", "wheel", "wheel", "wheel", "engine", "fuel_tank", "cockpit"],
    "plane":     ["frame", "wing", "wing", "tail", "propeller", "engine", "fuel_tank", "cockpit", "landing_gear"],
    "bad_plane": ["frame", "propeller", "engine", "fuel_tank", "cockpit", "landing_gear"],   # нет крыльев
    "no_driver": ["frame", "wheel", "wheel", "wheel", "wheel", "engine", "fuel_tank"],        # нет кокпита
}


def main():
    conn = psycopg2.connect(DSN)
    for name, parts in DEMOS.items():
        st = finalize(parts)
        vid = store_vehicle(conn, name, parts, st)
        verdict = []
        if st["drives"]:
            verdict.append(f"ЕДЕТ v={st['v_ground']}")
        if st["flies"]:
            verdict.append(f"ЛЕТИТ v={st['v_air']}")
        if not verdict:
            verdict.append("НЕ едет / НЕ летит")
        print(f"#{vid} {name:<10} mass={st['mass']:<4} drive={st['drive_force']:<4} "
              f"thrust={st['thrust']:<4} wing={st['wing_area']:<3} ctrl={int(st['controllable'])} "
              f"→ {', '.join(verdict)}")
    conn.commit(); conn.close()


if __name__ == "__main__":
    main()
