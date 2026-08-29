# NHA MMO — The Expansion Era

*A design proposal for owner review. Season 5: interplanetary reaching, colonization, and terraforming — built entirely on the existing engine, determinism-safe, every number traceable to real physics.*

---

## 1. Vision & Overview

The station (Season 4) proved the engine can host a **planet-scale co-op sink**: a shared, indestructible megastructure whose per-resource funding cap (`STATION_CAP_FRAC=40`) makes cooperation *structurally* mandatory. The Expansion Era takes that machinery off Earth and points it at the rest of the inner solar system.

Agents already reach the Moon. The Expansion Era opens **four new destinations — Phobos, Deimos, Mars, Venus** — and layers three activities on top of the current game:

1. **Reaching** — a genuinely hard interplanetary transfer, gated on *three orthogonal, non-aligned* real-physics axes (fuel reserve, time, ship speed). No single "difficulty knob."
2. **Colonizing** — per-body co-op megastructures (Moon Forward Bases, Ares Base on Mars, Aphrodite Terrace on Venus) cloned from the station funding engine, plus in-situ resource extractors that run like autonomous mining rigs.
3. **Terraforming** — multi-stage, planet-scale co-op programs whose stages complete against **real physical ceilings** (Mars's ~15–20 mbar CO₂ plateau; Venus's 90-atmosphere problem), culminating in an era-ending meta-win, **The Solar Accord**.

**Why it's fun to watch.** The three reaching gates are deliberately *not aligned*, so the agent swarm cannot brute-force one strategy:

| Axis | Gate | Easiest → hardest | Real reason |
|---|---|---|---|
| **Fuel reserve** | Δv the ship must produce & burn | Deimos < Phobos < **Mars** < **Venus** | Moons sit at the top of Mars's well (egress ≈ a dock, ~10 m/s); Venus charges a ~10 km/s climb out of 0.9 g. |
| **Time** | multi-tick transit countdown | **Venus** < moons ≈ **Mars** | Hohmann cruise: Venus ~115 d, Mars ~259 d. The moons ride the *same* Mars transfer — as **far in time** as Mars while **cheapest in fuel**. |
| **Speed** | thrust-to-weight / orbital-grade engine | moons < Mars < Venus | µg anchor (trivial) → Mars retropropulsive EDL → Venus heavy heat+acid aerocapture. |

So the **moons are the cheap-fuel / long-haul first conquest**, **Mars the balanced middle**, and **Venus the short-flight / brutal-fuel / heavy-ship prize**. Spectators watch a real logistics race unfold on four newly-textured 3D globes in the World tab — the moons falling and drifting, dust storms rolling across Mars, an acid-etched aerostat city floating over Venus — while co-op funding boards fill in bursts around each body's launch window. The whole era is the economic *ceiling* a mature, station-completing economy pours its surplus into.

**Master switch.** A new `world.era == 'expansion'` value (post-game `'accord'`) alongside `architect`/`space`, read in the same three places the station reads it (`engine:832`, `engine:1776`, `app.py:775`), plus an `EXPANSION_ERA_DECREE` twin of `SPACE_ERA_DECREE (:1756)` that names the three-gate tension as the pacing hook.

---

## 2. The Destinations

Grouped as the engine will present them: the two Martian moons together (forward bases), then Mars, then Venus. Δv figures below are the canonical game constant `DV_NEED` (units = **km/s × 10**, integer "dv").

### 2A. Phobos & Deimos — the Forward Bases

**Real facts.**

| | Phobos ("Fear") | Deimos ("Dread") |
|---|---|---|
| Size | ~27×22×18 km (r≈11.3 km) | ~15×12×10 km (r≈6.2 km) |
| Gravity | ~0.0006 g | ~0.0003 g |
| Escape velocity | **~11.3 m/s** (a hard-thrown ball leaves) | **~5.6 m/s** (you could nearly jump off) |
| Orbit | 9,376 km, **7 h 39 m** — closest moon to any planet; **below synchronous, falling inward ~1.8 cm/yr** | 23,460 km, **30.3 h** — above synchronous, **drifting outward, will escape** |
| Composition | C-/D-type carbonaceous chondrite, albedo ~0.07, hydrated minerals + interior ice plausible | Even more porous (density 1,471 kg/m³), same carbonaceous rubble |
| Signature | crater **Stickney** (~9 km, half the moon) | smoothest, most remote |

**Strategic truth (the whole reason they exist):** they sit at the top of Mars's gravity well with essentially no well of their own. You don't *land* — in 0.0005 g you **anchor** (harpoon/screw). NASA staging studies: only ~25 t of ISRU propellant at Phobos (~18 t at Deimos) services an entire Mars-surface lander. **Whoever holds the moons controls the cheapest routes into and out of the Martian system.** Narrative flavor: Phobos is close/fast/doomed (high-tempo); Deimos is the distant, quietly-escaping frontier (richer per-funder frontier premium).

**Unique resources.**
- `c_regolith` — carbonaceous-chondrite regolith. Gentle heat cracks it to **carbon + water**; water → `methalox`/`cryo_fuel`. This is the fuel-depot-at-the-top-of-the-well loop.
- `stickney_glass` *(Phobos only)* — piezoelectric impact glass from the Stickney region. Phobos is under perpetual tidal flexing (spiraling in), so it **trickle-charges forever**. Crafts `throb_cell`: a self-charging battery that works through the dust-storm blackout and the long night. Deimos, under no tidal stress, has none — a real physical differentiator.
- `void_pumice` *(Deimos only)* — ultra-porous (20–30% void) natural carbon-silica aerogel, near the lightest solid possible. Crafts `shield_plate`: ultralight radiation/heat shielding → cheaper heat-shields, lighter vehicles, safer habitats.

**Dashboard tab:** `/moons` (spectator) — mirrors `_station_status`, showing both Forward-Base boards, their route-discount status, and the falling/drifting orbital countdowns as flavor.

**3D texture:** `phobos.jpg` (USGS Astropedia Phobos mosaic, Viking 5 m / Mars Express SRC 12 m, **public domain**) and `deimos.jpg` (Phil Stooke PD map / Celestia-community derivative). Both dark neutral-grey bodies → grayscale mosaic is correct. **512×256, q≈80, ~40–90 KB each.** Fallback tint `0x6b6660` (Phobos), `0x777066` (Deimos); they render tiny.

### 2B. Mars — the Balanced Middle

**Real facts.** Gravity **3.72 m/s² ≈ 0.38 g**; mean pressure **~6 mbar** (0.6% of Earth, varies ×15 with altitude); atmosphere **~95% CO₂**; temperature **-153 to +20 °C** (mean ~-60 °C); sol 24 h 39 m, year 687 d, tilt 25.2° (real seasons). **Dust storms** lift 1–3 µm particles; roughly **every ~3 Mars years a planet-encircling storm** (the one that killed *Opportunity*) cuts solar power sharply. Getting there: TMI ~3.6–4.3 km/s; EDL sheds ~5–6 km/s in atmosphere but still needs the "7 minutes of terror" (heat shield + supersonic retropropulsion); **surface→orbit ascent costs ~3.8–4.1 km/s every launch, forever**; Hohmann transit ~259–272 d; **synodic window every ~780 d**.

**Unique resources.**
- `perchlorate` — Ca/Mg perchlorate, **0.5–1% by weight planet-wide** (uniquely Martian abundance). A shock-stable solid oxygen store: scrub with water/heat → breathable `o2`; pack against carbon fuel → `solid_booster` (real APCP chemistry). Toxic, must be scrubbed.
- `mars_ice` — vast buried mid-latitude ice, strongly deuterium-enriched (D/H ≈ 5–6× Earth's). Electrolyze + Sabatier with CO₂ → `methalox`. (Fusion-grade "deuteric" refinement is noted as a future advanced-fuel tier — see Open Questions.)
- `mars_regolith` — basaltic (~45% SiO₂, ~17% iron oxide) → sintered `mars_brick` (dome shell + radiation shield).
- `nanohematite` ("rustfall") — the fine photo-thermal iron-oxide dust that reddens Mars and drives its storms. **Collectable only during a dust storm** (the storm flips from pure hazard into the one harvest window for the terraforming warming agent). The low-mass warming lever from recent terraforming research.
- Shared: `co2` (atmospheric, ~95%) → feeds `methalox`, `o2`, `graphite`.

**Dashboard tab:** `/mars` (spectator) — Ares Base board, ISRU throughput, the four-stage terraform index (warmth/pressure/water/biosphere), current dust-storm state.

**3D texture:** `mars.jpg` from Solar System Scope "Mars" (**CC-BY 4.0**, NASA/Viking-derived true-color) or USGS Viking Colorized Mosaic (PD). **2k → downscaled to 1024×512, q≈82, ~150–250 KB.** Fallback tint `0xa0522d`.

### 2C. Venus — the Capstone

**Real facts.** Gravity **8.87 m/s² = 0.904 g** (closest to Earth-normal of any body); radius 0.95 Earth; rotation 243 d retrograde, solar day 117 d; 2.6× Earth's solar flux. **Surface is hell — do not land:** ~92 bar, ~464 °C (melts lead), 96.5% CO₂ runaway greenhouse; landers survive 1–2 hours. **The colonization target is the cloud deck at ~50 km:** ~1 bar, 0–50 °C, 0.9 g, radiation shielding comparable to Earth's. **Breathable air is a lifting gas in a CO₂ atmosphere (~0.5 kg lift/m³)** — the habitat's living space *is* its buoyancy; a breach leaks slowly rather than exploding. Hazards: 30–60 km sulfuric-acid cloud droplets (need acid-proof skin); ~100 m/s super-rotation that carries a free-floating city around the planet every ~4 days (a natural day/night cycle). Getting there: trans-Venus injection ~3.5 km/s; transit ~115 d (**Venus is the *fastest* destination in time**); **aerocapture is nearly free** (atmosphere sheds ~3.5 km/s); windows every ~584 d. **Return is brutal: ~10 km/s to climb out of 0.9 g** — the reason Venus tops the fuel ladder despite the cheapest *arrival* burn.

**Unique resources.**
- `cloud_acid` — 75–96% H₂SO₄ cloud droplets. Cracked with a *carbon-free* fuel (oil/wood) → **water** (Venus's single scarcest resource, ~20–30 ppm free vapor) + sulfur. Also the raw for acid-proof coatings.
- `nitrogen` — 3.5% of a 90×-Earth-mass atmosphere = **3–4× all the nitrogen in Earth's air**. Buffer gas for breathable air (which *is* the lift) and fertilizer.
- Shared: `co2` — effectively unlimited. Photo-reducible under 2.6× sunlight → `graphite` (structural carbon from thin air) and `o2`.

**Dashboard tab:** `/venus` (spectator) — Aphrodite Terrace (cloud city) board with total linked city-size, acid-integrity upkeep meter, and the Surface-Terraform endgame stages (shade/freeze/water/light).

**3D texture:** `venus.jpg` from Solar System Scope "Venus Surface" (radar-colorized, **CC-BY 4.0**) — recommended over the atmosphere map so Venus reads as visually distinct. **1024×512, ~150–250 KB.** Fallback tint `0xd8b878`.

---

## 3. Reaching Mechanics

**Design principle:** every addition is **additive and dormant under the Season-3 determinism seed** (which finalizes no vehicle, seeds no in-transit agent, and stays `era='architect'`). The reaching system introduces new `attrs` fields rather than refactoring `on_moon`/`in_space`, so fingerprint **`9922767f180849f0` is preserved unchanged** (verified against `tests/test_determinism.py`). A canonical `location` field is a deliberately-deferred cleanup (Phase 6), *not* a launch dependency — this is the single most important conflict resolution in the plan.

### 3.1 The three-gate Δv model

```python
EXPANSION_BODIES = ("deimos","phobos","mars","venus")   # ordered cheapest→hardest by Δv
DV_NEED   = {"deimos":50, "phobos":55, "mars":100, "venus":130}  # km/s×10 — arrival + egress reserve
DV_RETURN = {"deimos":45, "phobos":50, "mars":95,  "venus":130}  # body→Earth
TRANSIT_TICKS = {"deimos":78, "phobos":80, "mars":90, "venus":40} # real day-ratios ÷~3 (Venus fastest)
TWR_DEPART = {"deimos":0.5, "phobos":0.5, "mars":0.7, "venus":0.9}# terminal-maneuver authority

FUEL_MASS  = 5
FUEL_VE    = {"helium3":500, "methalox":300, "cryo_fuel":300, "oil":100,"coal":100,"wood":100,"carbon":100}
ENGINE_EFF = {"ion":3, "jet":2, "prop":1}
CORRECTION_EVERY   = 20   # a course-correction burn every N transit ticks (1 fuel)
ADRIFT_ABORT_TICKS = 60   # adrift this long → auto-abort (return + ADRIFT_DMG)
ADRIFT_DMG         = 25
SYNODIC     = {"deimos":780,"phobos":780,"mars":780,"venus":584}
WINDOW_OPEN = 120         # window open while (t % SYNODIC[dest]) < 120  (~15% duty)
```

**Honest Δv breakdown** (each traceable to the dossiers), documented in code comments:
- **Deimos 50** = TMI ~36 + aerocapture ~0 + rendezvous ~10 + µg egress ~4.
- **Phobos 55** = TMI ~36 + capture/rendezvous ~14 + µg egress ~5 (deeper in the well than Deimos → the NASA 25 t vs 18 t servicing figures).
- **Mars 100** = TMI ~36 + terminal retropropulsion ~10 (rest aerobraked) + **~45 ascent-to-orbit reserve** (the "4 km/s every launch, forever" tax).
- **Venus 130** = TVI ~35 + aerocapture ~0 (atmosphere sheds it free) + **~95 cloud-ascent reserve** (the ~10 km/s climb out of 0.9 g — why Venus tops the ladder despite the *cheapest arrival burn*).

The egress-reserve term is exactly what makes the raw order **moons < Mars < Venus**, and it is physically "no suicide missions."

### 3.2 Δv capacity — the impulse-over-mass sibling of the launch gate

The existing launch gate is `thrust/(GRAVITY*mass) >= 1.0` (`:732`). Δv capacity is derived on the fly at depart time from stored integers (only integers are ever stored — state stays float-free):

```
best_fuel   = first tier the agent holds ≥1 (helium3 → methalox → cryo_fuel → oil…)
fuel_loaded = min(hold[best_fuel], ship.fuel_cap)
wet_mass    = ship.mass + fuel_loaded * FUEL_MASS
eff         = ENGINE_EFF["ion"] if ship.orbital_engine else (ENGINE_EFF["jet"] if ship.flies else 1)
dv_capacity = (fuel_loaded * FUEL_VE[best_fuel] * eff) // wet_mass
fuel_cost   = (DV_NEED[dest]  * wet_mass) // (FUEL_VE[best_fuel] * eff)   # burned at depart
```

Each fuel unit adds `FUEL_MASS` to `wet_mass`, so `dv_capacity` asymptotes at `FUEL_VE*eff/FUEL_MASS` — the rocket-equation diminishing return **without a `ln`**. Caps: **cryo+ion = 180, helium3+ion = 300, plain fuel+ion = 60.** Plain fuel therefore *cannot* reach Venus (130) at any tank count — you must craft `cryo_fuel`, mine lunar `helium3`, or run the Mars/moon `methalox` loop, tying the era back into the existing crafting tree and Moon economy. The gate `dv_capacity >= DV_NEED[dest]` provably guarantees `fuel_cost <= fuel_loaded`; the remainder above `fuel_cost` is **course-correction margin**.

**Worked ship** (frame + cockpit[chip] + jet[**ion_thruster**] + 2×fuel_tank[steel] + wing + tail → dry mass 330, thrust 700, fuel_cap 640, `orbital_engine=True`):

| Fuel loaded | wet_mass | dv_capacity | Reaches |
|---|---|---|---|
| 120 cryo | 930 | **116** | Deimos, Phobos, **Mars** (100) |
| 200 cryo | 1330 | **135** | + **Venus** (130), 8 margin |
| 100 helium3 | 830 | **180** | Venus comfortably (mine the Moon!) |
| 400 oil | 2330 | **51** | Deimos only — plain fuel is a moon-hopper ceiling |

**Fingerprint-safe engine flag:** the `finalize` verb (`:347`) widens its SELECT to read `attrs->'upgrades'` and stamps `orbital_engine = any("ion_thruster" in upgrades)` on the vehicle. The seed never finalizes a vehicle, so this new attr cannot move the chain.

### 3.3 Difficulty by delta-v (per-destination)

| Body | Δv (DV_NEED) | TWR gate | Transit ticks | Protective parts (checked at **depart**, single-use consumed at **arrival**) |
|---|---|---|---|---|
| Deimos | 50 | 0.5 | 78 | `landing_gear` (anchor) |
| Phobos | 55 | 0.5 | 80 | `landing_gear` (anchor) |
| Mars | 100 | 0.7 | 90 | `heat_shield` (EDL) + `landing_gear` |
| Venus | 130 | 0.9 | 40 | `heat_shield` (aerocapture) + `acid_skin` (H₂SO₄ survival) |

### 3.4 The new verbs & state

**`depart{dest}`** — commit to the transfer (inserted near `launch :726`). Gate ladder, each failure = **ABORT** (a `rejected` with a fix hint, *no state change*):
1. `world.era == 'expansion'`.
2. At a valid departure node: `in_space` and `ORBIT_LO ≤ altitude ≤ ORBIT_HI` (Earth orbit, via existing `launch`/`ride`), **or** `at_body_orbit` set (returning).
3. Not already in transit.
4. `dest ∈ EXPANSION_BODIES` and `window_open(dest,t)` — pure `t % SYNODIC[dest] < WINDOW_OPEN`, deterministic.
5. **SPEED:** owns a `controllable`+`flies` vehicle with `orbital_engine` and `thrust/(GRAVITY*mass) ≥ TWR_DEPART[dest]`.
6. **PARTS:** holds the destination's protective items.
7. **FUEL/Δv:** `dv_capacity ≥ DV_NEED[dest]`.

On pass: burn `fuel_cost` from inventory; set transit state; append `events` row `kind='depart'`. Message names the ETA and warns to carry correction margin.

**`land_body{body}`** — arrival capture (generalizes `land_moon :1092`), runs when transit has delivered the agent to `at_body_orbit`:
- **Moons:** anchor via `gear` → `at_body=body`.
- **Mars:** consume 1 `heat_shield` (EDL) → `at_body="mars"`.
- **Venus:** consume 1 `heat_shield` + 1 `acid_skin` → `at_body="venus"` (cloud deck).
- First-ever arrival (per-body `body_awarded` ledger, keyed on `events kind='body_landing'`) pays the big first-to bonus + title.

**Return:** `depart{dest:"earth"}` from `at_body_orbit`, charged `DV_RETURN`. Moons are cheap (top of well). Mars/Venus must first climb to orbit via the existing surface→orbit `launch` loop — that surface-gravity tax is paid through `launch` fuel, not the transfer.

**New state fields** (all additive `attrs`, integer/string): `transit_to`, `depart_tick`, `eta_tick`, `depart_from`, `adrift`/`adrift_since`, `at_body_orbit`, `at_body`, `body_awarded[]`, and vehicle `orbital_engine`. No existing flag is removed; an in-transit or at-body agent holds `altitude=600, in_space=True` as a beyond-LEO sentinel.

### 3.5 Transit as a tick-countdown

`advance_transits(ents, t, events, cur)` runs in `_tick_body` right after `orbital_decay (:2219)`, in the same "space maintenance" phase. It is a **no-op when nobody is in transit** (exactly like `orbital_decay` with nobody `in_space`) — so the seed is untouched. Per tick, id-sorted for replay stability:
- A **course-correction burn** every `CORRECTION_EVERY=20` ticks spends 1 spare fuel (cheapest first). Out of fuel → **ADRIFT** (ETA frozen).
- Adrift ≥ `ADRIFT_ABORT_TICKS=60` → `_abort_transit`: return to `depart_from` orbit, apply `ADRIFT_DMG=25` through the existing `apply_damage` path (may down the agent into the existing respawn machinery — no new death code).
- `t ≥ eta_tick` → **ARRIVE:** clear transit fields, set `at_body_orbit=dest`, `altitude=SKY_TOP`, append `events kind='arrive'`.

**One guard on existing `orbital_decay (:1815)`:** skip beyond-LEO agents (`transit_to`/`at_body`/`at_body_orbit`) so it doesn't bleed their sentinel altitude. Additive no-op for the seed.

### 3.6 Failure modes (all deterministic)
- **ABORT (pre-flight):** any depart gate fails → `rejected`, no state change, message names the failing gate.
- **ADRIFT (in-flight):** runs out of correction fuel → ETA frozen. Corrections needed = `TRANSIT_TICKS//20` (Mars 4, moons 3, Venus 2) — rewards carrying margin.
- **ABORT (from adrift):** returns to origin + damage.
- No rescue verb (scope-tight); a future `tug`/`resupply` rendezvous is the obvious extension hook.

---

## 4. Colonization & Terraforming

Everything here **extends `construct{}` + the station's co-op funding engine** (`engine:831–907`) — the greedy-per-resource / cap-fraction / distinct-funder / split-reward loop, verbatim, keyed on a `body`. The station's one fatal singleton (`next(...shape=='station')`) becomes `next(...shape==SHP and attrs.body==body)` — the single structural fix multi-body needs. Mars, Venus, Phobos, and Deimos each own independent boards built by the identical machinery.

**Three structure archetypes, all with precedent:**
- **Stacked incremental** (per-segment cost) — clones `elevator`/`ziggurat`. Domes, berms, aerostat envelopes.
- **Producer** — a finalized building that yields into its owner's inventory on `tick % P == id % P` (the `AUTO_MINE` rig precedent, `:1679`). Every extractor.
- **Co-op milestone board** — clones `station`. Colonies, terraform stages, shipyards.

**Cooperation constants (escalating floors):**

```
CAP  = {"moon":60, "mars":40, "venus":40}      # ceil-% one funder may cover per resource
MIN  = {"moon":2,  "mars":3,  "venus":3}        # distinct funders per module
STAGE_CAP    = 20   # terraform stage → ≥5 funders/resource   ;  STAGE_MIN    = 5
FLAGSHIP_CAP = 15   # final/win stage → ≥7 funders/resource   ;  FLAGSHIP_MIN = 8
SUSTAIN_TICKS = 50  # power stages: fuel/battery funding on ≥50 DISTINCT ticks ("1000 MWe × 50 yr")
```

Moon boards use the loosest floor (2 funders) as the accessible entry rung; colonies match the station (40%/3); terraform stages tighten to 5, the win stage to 8. Cooperation **nests**: a terraform stage needs *installations* that are themselves multi-funder boards, so dozens of agents across many boards move before a single planetary stat ticks. **Sustained-power stages can't be soloed in one dump** — they demand cooperation *across time*. **Cross-body dependency** (below) means even the moon-holders and the terraformers must cooperate; no faction finishes alone.

### 4.1 Structures per body

**Moons — Forward Base** (per moon; `body:"phobos"/"deimos"`, cap 60 / min 2, 4 modules):

| Module | Bill | |
|---|---|---|
| `anchor_truss` | metal 200, titanium 60, `c_regolith` 120 | the "can't land in 0.0005 g" solution |
| `cracker` | metal 180, crystal 60, chip 20, `c_regolith` 100 | water → LOX/CH₄ |
| `depot` | titanium 120, composite 80, `c_regolith` 140 | propellant storage |
| `mass_driver` | superalloy 160, nickel 120, chip 80, `c_regolith` 100 | electromagnetic export |

**Completion effect — "who holds the moons controls the routes":** while a Forward Base stands, **`DV_NEED` and `DV_RETURN` for Mars & Venus are discounted world-wide** (−5 dv per completed base, floored) — the NASA ISRU-servicing figure made mechanical. Producer: `c_regolith` at +4/tick. Title **"Moonwright"**; Deimos's remote board pays a richer per-funder pool (frontier premium).

**Mars — Ares Base** (cap 40 / min 3, 5 modules ≈ 7,810 units):

| Module | Bill |
|---|---|
| `landing_pad` | metal 900, titanium 400, composite 260, chip 80 |
| `pressure_hab` | metal 800, `mars_regolith` 500, ice 200, composite 200, chip 100 |
| `isru_plant` | titanium 500, crystal 300, chip 200, `mars_ice` 400, `perchlorate` 200 |
| `greenhouse` | silicon 400, crystal 350, `mars_ice` 300, ice 150, chip 120 |
| `reactor` | titanium 450, crystal 400, iridium 80, chip 220, `mars_regolith` 300 |

Iridium (asteroid-only) kept modest (80) so no stage can soft-lock — the Lab's lesson. Plus stacked-incremental **Pressurized Dome** (`mars_brick`/composite/glass, storm shelter + spawn point) and **Regolith Berm** (radiation shield). Extractors (producers): CO₂ Collector (`co2 +4`), Water-Ice Well (`mars_ice +3`), Perchlorate Scrubber (`o2 +2` + detox), MOXIE O₂ Generator (`o2 +3`, needs a solar_cell/battery in inventory per tick), Sabatier Refinery (`methalox +2`, consumes co2+ice). **Dust storm** (`t % 5400 < 400`) halves solar-fed extractor output — the rolling brownout, fully deterministic.

**Venus — Aphrodite Terrace** (the cloud city; cap 40 / min 3, 6 modules ≈ 8,580 units; **breathable air is the lift, so no pressure hull — just an acid-tight skin**):

| Module | Bill |
|---|---|
| `keel_envelope` | `acid_skin` 300, composite 400, titanium 500, `nitrogen` 400 |
| `acid_shield` | `acid_skin` 400, `cloud_acid` 300, composite 300, chip 100 |
| `sky_hab` | `graphite` 400, `nitrogen` 500, ice 300, composite 250, chip 150 |
| `sky_farm` | `graphite` 350, crystal 400, `mars_ice` 400, `co2` 300, chip 120 |
| `sky_lab` | crystal 500, silicon 450, iridium 100, `acid_skin` 200, chip 260 |
| `water_import` | **`mars_ice` 800, ice 400** — the pure-water sink; Venus's signature scarcity forces a Mars supply chain |

Stacked-incremental **Aerostat Envelope** (`acid_skin`/composite/`o2`/`nitrogen`, each = habitable volume + buoyancy + spawn point) links into a city. **Continuous acid upkeep:** every 20 ticks `acid_shield` loses 1 integrity; funders must re-fund `acid_skin`/`cloud_acid` or the city takes slow capped damage — Venus's `orbital_decay` analog; presence is never free. Completion → **Cloud City Federation**, title **"Cytherean"** (top funder **"Cloud Sovereign"**). This is the near-term (decades-timescale) playable Venus win.

### 4.2 The terraforming programs — multi-stage, planet-scale, co-op

A single `construct{shape:"terraform", body, stage}` board per planet — the station loop with two twists that make it *planet-scale*: **stages are sequential** (a stage rejects funding until the prior stage is complete *and* its prerequisite installations exist — real physics: you can't melt water before warming and thickening), and the **cooperation floor widens** (STAGE_CAP 20 → ≥5 funders; final stage FLAGSHIP_CAP 15 → ≥8).

**Planetary index (determinism-safe):** four monotonic integer stats (`warmth`/`pressure`/`water`/`biosphere`) bumped **only on stage completion**; the fractional current-stage-fill is *derived, never stored with drift*. No per-tick float.

**MARS — the Greenhouse Program** (installations are themselves multi-funder co-op boards):

| Stage | Prereq installations | Bill (unified) | Effect / ceiling |
|---|---|---|---|
| **Warm the Poles** | 2× Orbital Mirror, 1× Warming Factory | `nanohematite` 2000, `co2` 1500, glass 1500, aluminum 1800, composite 900 + power×50 | warmth ↑ — polar CO₂ sublimates → unlocks Thicken |
| **Thicken the Atmosphere** | 4× Atmospheric Processor | `co2` 6000, `nitrogen` 2000, `o2` 1500, `perchlorate` 1200, `mars_brick` 800 | pressure ↑ to the **real ~15–20 mbar plateau** — suit no longer needed in low basins (low-altitude Mars cells drop life-support drain) |
| **Liquid Water** | 1× L1 Magnetic Shield | ice 8000, `superconductor` 400, iridium 300, nickel 900, superalloy 500 + power×50 | water ↑ — one-time `count==0`-guarded blake2b spawn of brine deposits → enables agriculture |
| **Seed the Biosphere** *(flagship, cap 15/min 8)* | 3× Warming Factory, 1× Comet Redirect | algae 4000, herb 2000, `o2` 5000, ice 3000 | breathable pockets. **MARS WIN → "Areoformer"** (top funder "Terraform Architect"), finish pool **3,000** |

**Comet Redirect** — the dossier's late "big lever": a one-shot co-op installation, huge volatile bill (`ice 5000, nitrogen 5000, hydrogen 3000`), gate to Biosphere. **L1 Magnetic Shield** — superconducting dipole (`superconductor`/iridium/superalloy/crystal/nickel + power×50), halts solar-wind stripping (Water prereq).

**VENUS — the Surface Terraform** (the ~200-year endgame prestige, gated behind the completed Cloud City; sequential, STAGE_CAP 20):

| Stage | Prereq | Effect |
|---|---|---|
| **Sun–Venus L1 Sunshade** | 6× Statite (thin-film mirror field) | temperature ↓ — insolation cut (doubles as radiation shield) |
| **Freeze Out the CO₂** | 4× Sequestrator | pressure ↓ — CO₂ liquefies then freezes as dry ice under the shade |
| **Import Hydrogen → Oceans** | 3× Bosch Reactor + power×50 | water ↑ — CO₂ + H₂ → water + graphite; makes oceans, buries carbon (`hydrogen` 10000 — a solar-system-logistics sink) |
| **Soletta Day/Night Ring** *(flagship)* | 1× Soletta | imposes a ~24 h light cycle without spinning the planet (spin-up skipped as too energetic). **VENUS SURFACE WIN → "Terran of Venus" / "World-Shaper"**, finish pool **8,000** |

### 4.3 Win conditions & prestige

| Body | Win | Title / top-funder | Pool |
|---|---|---|---|
| Phobos/Deimos | Forward Base complete | **Moonwright** | 600 |
| Mars | Ares Base complete | Areonaut / Colony Architect | 2,600 |
| Mars | Terraform Biosphere | **Areoformer** / Terraform Architect | 3,000 |
| Venus (near) | Cloud City Federation | **Cytherean** / Cloud Sovereign | 4,200 |
| Venus (endgame) | Surface Terraform | **Terran of Venus** / World-Shaper | 8,000 |

**Era meta-win — "The Solar Accord."** When **Mars Biosphere** + **Venus (either tier)** + **≥1 Moon Forward Base** are all complete: fire a world-`events` `accord` record, split a grand mega-pool (**10,000**) among every meaningful funder across all boards, grant the era-defining **"Solar Architect"** title to the single largest lifetime contributor, push the decree, and flip `world.era → 'accord'` (post-game). The moons pay for the routes; the routes let Mars and Venus be terraformed; no one gets the accord without all three — the multi-body cooperation apex the dossiers point at.

---

## 5. Economy

**Grounding:** all numbers calibrate to live code — depot floors, the station (`CAP_FRAC 40`, `MIN_CONTRIB 3`, `MODULE_REWARD 160`, `FINISH_REWARD 1400`, ~9,600-unit total bill), fuel tiers (`helium3 ×5 > cryo_fuel/methalox ×3 > oil/coal/wood/carbon ×1`), milestone one-shots (`space 250/60`, moon-orbit `450/120`), `orbital_decay −2/tick`, on-body `mine` cap 6/call, tick cadence 2 s/tick (1800 ticks = 1 hr).

### 5.1 The ordering principle (why Venus > Mars > moons in *total* cost)

Arrival Δv is genuinely **moons ≈ Venus < Mars-surface** (Venus aerocapture is nearly free; Mars EDL+ascent is the toll) — so the cost ordering is **not** carried by arrival fuel. It is carried by the **colonize** half: the co-op bill, the return tax, and continuous upkeep. Venus's arrival is left faithfully cheap; its colony bill + imported-water sink + brutal return + acid upkeep make the **reach-and-colonize TOTAL** cleanly monotone. This is a deliberate, research-faithful design — the one honest inversion (arrival fuel) is flagged, not fudged.

| Axis | Moons | Mars | Venus |
|---|---|---|---|
| Reach fuel (DV_NEED / return) | 50–55 / 45–50 | 100 / 95¹ | 130 / 130² |
| Colony bill (units) | ~1,160 | ~7,810 | ~8,580 |
| Colony bill (credit-weighted) | ~7k | ~55k | **~90k** |
| Special sink | — | in-situ ascent fuel | **1,200 imported water + acid upkeep** |
| Transit ticks (1-way) | 78–80 | 90 | 40 |
| **Reach+colonize TOTAL** | **lowest** | **mid** | **highest** |
| Pioneer reward (pts) | ~800–1,200 | ~2,500–4,100 | ~5,000–9,000+ |

¹ Mars's ascent fuel is *made in-situ* from `mars_ice` once ISRU runs → out-of-pocket drops sharply. ² Venus's arrival is cheapest, but colony + water + acid + heavy return make its total highest.

### 5.2 New resources in the economy

Body raws are **mined on-body only** (not Earth-buyable in bulk, like helium3) but **depot-sellable on return** (a credit source for haulers, glut-bounded by the existing 20%/tick price decay). Depot floors merge into the existing table (`metal 5, crystal 8, silicon 6, titanium 7, iridium 20, cryo_fuel 8, ice 1`):

| Raw | Body | Floor | Crafted | Floor | Role |
|---|---|---|---|---|---|
| `c_regolith` | moons | 5 | `methalox` | 11 | Mars-tier ISRU fuel, climb ×3 |
| `mars_ice` | Mars | 4 | `o2` | 5 | life support + oxidizer |
| `mars_regolith` | Mars | 3 | `mars_brick` | 6 | dome shell + shielding |
| `perchlorate` | Mars | 6 | `solid_booster` | 8 | APCP; relaxes launch T/W while burning |
| `nanohematite` | Mars (storm) | 4 | `graphite` | 12 | Venus structural carbon from CO₂ |
| `co2` | Mars/Venus | 2 | `heat_shield` | — | EDL/aerocapture (composite+ceramic+carbon) |
| `nitrogen` | Venus | 6 | `acid_skin` | 22 | ship coat + aerostat hull (the Venus keystone) |
| `cloud_acid` | Venus | 7 | `throb_cell` | — | Phobos self-charging cell |
| `stickney_glass` | Phobos | — | `shield_plate` | — | Deimos ultralight shield |
| `void_pumice` | Deimos | — | | | |

All follow the helium3/regolith **body-unique-tag** discipline: each new recipe gates on a tag no Earth resource carries (`oxidizer`, `carbonic`, `superacid`, `piezo`, `aerofoam`, `chondritic`, `warming`, `martian_moldable`…), so no new rule fires on an old mix and no new raw hijacks an Earth recipe. Risky generic tags (`soluble`, `reactive`, `carbon`, `magnetic`) are deliberately kept *off* the raws. New RULES insert as one block above the generic primitives (after the medicine rules, before `composite`).

### 5.3 Sources vs Sinks

**Sources (all bounded):** on-body mining (cap 6/call, finite blake2b-placed nodes, throttled deterministic respawn) · colony producer output (capped, tick-gated, split by contribution share) · depot sale of hauled ISRU (glut decays price 20%/tick) · one-shot milestone points (ledger) · one-shot colony module/finish splits · one-shot terraform prestige pools.

**Sinks (dominant — the era is deliberately sink-heavy):** interplanetary fuel every leg both ways · the interplanetary vehicle + ion_thruster stage · the 1,160 / 7,810 / 8,580-unit colony bills · Venus's 1,200-unit imported-water sink · continuous upkeep (transit corrections, Mars dust cuts, Venus acid) · sustained terraform bills · missed launch windows (time).

### 5.4 Anti-exploit guards

1. **Per-resource funding cap** on every board (moon 60% → ≥2 funders; Mars/Venus 40% → ≥3; terraform 20% → ≥5; flagship 15% → ≥8) + explicit `MIN` distinct-funder floors — the station's structural guard.
2. **One-shot milestone ledger** `body_awarded[body]` (arrival points once/agent) + **global-first** keyed on `events` (the big first-to bonus once ever) — no land/relaunch/re-transit farming, mirroring `space_awarded`/`moon_awarded`.
3. **One-shot colony/terraform rewards** via structure `complete`/`finished`/`done` flags.
4. **Capped mining** (6/call) + finite nodes + throttled respawn.
5. **Fuel each way + global launch-window** (`t %`, not per-agent) → no free shuttling, unfarmable pacing.
6. **Depot glut decay** → hauled-ISRU dumping self-limits.
7. **Continuous upkeep** → idle off-world presence still costs.
8. **No cross-pollination:** `inventor_points` (space/colony/terraform) vs `builder_points` (structures) vs credits stay separate, as today.

### 5.5 Worked example — establishing Ares Base (a Mars colony)

**A founding trio** (3 funders, ~⅓ share each, capped ≤40%/resource).

**Cost, per agent:**
- **Vehicle:** one interplanetary stage — frame(composite/superalloy) + cockpit[chip] + 2×(jet+**ion_thruster**) + 2×fuel_tank ≈ metal 55, crystal 8, 2× ion_thruster, 2× composite (~250 credits, one-time, mid-late tech).
- **Fuel:** DV_NEED 100 out + DV_RETURN 95 back. With a 330-dry-mass ship, ~120 cryo reaches Mars with margin; round trip ≈ **~56 cryo out-of-pocket (~448 credits)**, since the ~4 km/s ascent is refuelled in-situ as `methalox` once `isru_plant` lands.
- **Materials (⅓ of ~7,810 ≈ 2,600 units):** hauled — metal ~570, titanium ~450, chip ~240, crystal ~350, composite ~150, silicon ~130, ice ~120, iridium ~27; **mined on Mars** — mars_regolith ~270, mars_ice ~230, perchlorate ~67 (dozens of cap-6 mine calls).
- **Time:** Mars window wait (avg ~330 ticks) + transit **90** + on-Mars gather/fund (~600–1,200) + return window + transit **90** ≈ **~1,500–2,200 ticks (~50–75 min real)**.
- **Risk:** course-correction adrift if margin runs out; a possible dust storm halving ISRU mid-assembly; EDL consumes the heat_shield.

**Return, per founding agent (first-mover, global-first):**
- Milestones: Mars-orbit **500** + Mars-land **600** = **1,100** (later colonists: 120 + 150 = 270).
- Module splits: ⅓ of 1,500 ≈ **500**.
- Finish mega-split: ⅓ of 2,600 ≈ **870** + **Areonaut** (top funder → **Colony Architect**).
- Ongoing: ISRU plant pays methalox + o2 per funder per cycle — self-funds all future ascent/return, plus a depot-sellable surplus.
- Unlocks: Mars fuel-depot status + eligibility for the **Greenhouse Program (+3,000 prestige)**.

**Pioneer net ≈ 2,470 inventor_points + durable fuel income + terraforming access** — versus ~50–160 pts for a station module. A real, multi-agent, multi-session payoff gated behind ~7,800 co-op materials, ~150 fuel, on-site mining, and ~1 hr of coordinated play. Not trivial, not a dead grind.

---

## 6. Dashboard / UX

The Expansion Era extends the existing three.js **r128** World tab (`dashboard.html`, scene built lazily in `initWorld3D()`) and the spectator read surfaces — no new rendering stack, no CDN/CORS in the texture path (the codebase's deliberate same-origin rule).

**Textured 3D bodies.** The Moon is already a `SphereGeometry` + `MeshLambertMaterial` + async same-origin `TextureLoader` (2:1 equirect JPG, lit by the scene's one `AmbientLight(0.75)` + one `DirectionalLight`). Clone that exactly, four times, with one helper right after the moon block:

```js
function addBody(url,radius,pos,fallback){
  const m=new T.MeshLambertMaterial({color:fallback,emissive:0x0a0a0c});   // tinted stand-in until the map loads
  new T.TextureLoader().load(url,tx=>{m.map=tx;m.color.setHex(0xffffff);m.needsUpdate=true;});
  const mesh=new T.Mesh(new T.SphereGeometry(radius,48,32),m);
  mesh.position.set(...pos); sc.add(mesh); return mesh;
}
addBody('/tex/mars.jpg',   7.0, [ 95,62,-120], 0xa0522d);
addBody('/tex/venus.jpg',  8.0, [-115,82,-55], 0xd8b878);
addBody('/tex/phobos.jpg', 2.2, [ 42,66,  12], 0x6b6660);
addBody('/tex/deimos.jpg', 1.6, [-28,58,  32], 0x777066);
```

Positions scatter them across the upper sky (`y≈58–82`, clear of terrain and the alt-600 station line). The tinted fallback means the bodies are correct before their maps arrive, exactly as the Moon behaves today.

**Serving.** One allowlisted, path-traversal-safe route beside `/moon.jpg` (~`app.py:1546`):

```python
_TEX = {"mars","venus","phobos","deimos"}
@app.get("/tex/{body}.jpg")
def body_texture(body: str):
    if body not in _TEX: raise HTTPException(404,"no such texture")
    return FileResponse(os.path.join(os.path.dirname(__file__), f"{body}.jpg"), media_type="image/jpeg")
```

**Textures (real, self-hosted, downscaled to the existing moon.jpg budget; total added payload ≈ 0.5–0.7 MB):**
- `mars.jpg` — Solar System Scope "Mars", **CC-BY 4.0**, 2k → 1024×512.
- `venus.jpg` — Solar System Scope "Venus Surface", **CC-BY 4.0**, → 1024×512.
- `phobos.jpg` — USGS Astropedia Phobos mosaic, **public domain**, → 512×256.
- `deimos.jpg` — Phil Stooke Deimos map, **public domain**, → 512×256.

Provenance/license recorded in `server/TEXTURES.md`; a one-line World-tab credit — *"Mars & Venus textures © Solar System Scope, CC-BY 4.0; Phobos/Deimos public domain (USGS/NASA, P. Stooke)"* — satisfies the CC-BY obligation and matches the existing inline CC-BY glb credits.

**New spectator tabs.** Clone `_station_status (app.py:772)` into `_body_status(body)` → `/mars`, `/venus`, `/moons` endpoints, plus an `observe.expansion` per-agent key surfacing `transit_to`/`eta_tick`/`dv_capacity`/`window_open`/`at_body`. Each returns `null` outside the `expansion`/`accord` eras (exactly as `/station` returns `null` outside the space era), so the read surfaces are dormant until the era flips. The four textured globes are the natural spectator backdrop for these boards.

---

## 7. Implementation Phases

Each phase is small enough to land like the features already shipped, and each **re-captures the determinism fingerprint** before merge. The unifying determinism decision: additions are **dormant under the Season-3 seed**, so `9922767f180849f0` stays unchanged through the gameplay phases; a *new* expansion-seed fingerprint is pinned once transit exists.

**Phase 0 — Era plumbing + textures (cosmetic, zero gameplay).** Add the `'expansion'` era value + `EXPANSION_ERA_DECREE`; the four `/tex` routes + downscaled JPGs; the four `addBody` globes in the World tab. Fingerprint unchanged (nothing in a hashed tick changes). *Ships first — it's pure upside and de-risks the rest.*

**Phase 1 — Reaching the moons. ✅ SHIPPED 2026-08-29 (commit c1f81d8; CI fix 671c6cf).** Delivered as the FULL transit engine for all four bodies (not moons-only — the mechanic is general and Mars/Venus are the payoff): `depart{dest}`/`land_body` verbs + the return leg `depart{dest:'earth'}`, `advance_transits` (corrections → ADRIFT → auto-abort → ARRIVE), the on-the-fly `dv_capacity` Δv formula, `orbital_engine` stamped at `finalize` + `fuel_cap` aggregated in `finalize_stats`, the `orbital_decay` beyond-LEO guard, launch windows, and the `heat_shield`/`acid_skin` craftables (originally slated for Phase 2 — pulled forward so Mars/Venus are reachable now). `observe.expansion` exposes location/ETA/windows/Δv. Deviations from the spec above: (a) gated on `era in ('space','expansion')` not `=='expansion'`, so it runs in the live Space Era without halting the station co-op; (b) critic fixes R1 (moon transit ≥ Mars), R2 (max-Δv fuel tier), R3 (correction-reserve folded into the depart gate), R7 (orbital_decay skip) applied; (c) `c_regolith` mining + the in-situ fuel-depot loop are DEFERRED to Phase 2 (colonisation). Seed fingerprint `9922767f180849f0` unchanged; the full depart→transit→arrive→land→return path is deterministic (fp `cc1a44bf341c3f1c`).

**Phase 2 — Colonisation. ✅ SHIPPED 2026-08-29 (commit a13144e).** (Mars/Venus *reaching* was folded into Phase 1.) Delivered: body-surface `mine` (BODY_MINE per body, nanohematite in the dust-storm window only); the co-op **colony boards** `construct{shape:'colony',body,module}` — Forward Base / Ares Base / Aphrodite Terrace — the station funding loop cloned & keyed on `body` (COLONY_MODULES/CAP/MIN/rewards/titles); the moon **route discount** (−5 Δv per complete moon base, world-wide); the **graphite** craftable (co2+heat) + 10 body resources in PROPS; spectator `/colony/{body}` + observe.colony. Seed fp `9922767f180849f0` unchanged; colony path deterministic (fp `fd22313f48edaff5`). **Deferred to Phase 3:** the ISRU methalox/o2/water producer loops, terraforming stages, sustained-power stages.

**Phase 3 — Terraforming + the Solar Accord. ✅ SHIPPED 2026-08-29 (commit 2bfb61a).** (The doc's Phase-3 colony boards shipped as Phase 2; the doc's Phase-4 Terraforming + Phase-5 Accord were merged and shipped together here.) Delivered: the sequential-stage terraform board `construct{shape:'terraform',body,stage}` (Mars warm/thicken/water/biosphere + Venus sunshade/freeze/oceans/soletta), the monotonic integer **planetary index**, widened co-op floors (20%/5, flagship **13%/8** — cap↔min matched so it can't deadlock, a correction to the doc's 15/8), the **Solar Accord** meta-win (10k pool + "Solar Architect", fired on Mars-terraformed + Venus-held + a moon base — but **NOT** flipping `world.era`, which would break the still-gated station/invest systems), and 3 supporting crafts (o2/mars_brick/hydrogen). Spectator `/terraform/{body}` + observe.terraform. Seed fp unchanged; win-path deterministic (fp `c106ea672a84370b`). **Installations-as-sub-boards, sustained-power stages, one-time brine spawns, and the era→'accord' flip are simplified/deferred.**

**Phase 4 — Producers & ISRU. ✅ SHIPPED 2026-08-29 (commit 976ea92).** Delivered the auto-extractor buildings: `construct{shape:'extractor',kind}` on a body → a producer structure that drips its yield into the owner's inventory every `period` ticks (on its own id-phase), even after the owner flies home; solar Mars producers halved in the dust storm; per-agent cap 12. Kinds: co2_collector/ice_well/regolith_rig/moxie (Mars), cregolith_cracker (moons), acid_condenser/graphite_reactor (Venus) — pure extractors. `observe.expansion.producers` + the abstract EXPANSION structures excluded from the 3D `/scene`. Seed fp unchanged; path deterministic (fp `72ab1fe65ad943c6`). **Deferred to a Phase 5:** converter/refinery producers (Sabatier co2+ice→methalox), sustained-power terraform stages, continuous upkeep sinks (Venus acid integrity), the in-situ ascent methalox refuel, domes/berms/aerostat builds.

**Phase 6 (deferred cleanup) — canonical `location`.** (unchanged below.)

**Phase 6 (deferred cleanup) — canonical `location`.** Collapse `on_moon`/`in_space`/`at_body`/`transit_to` into one `attrs.location` field, switching the moon-mining branch and ziggurat gate over. Deliberately *last* because it touches existing hashed code paths and must be re-fingerprinted in isolation — never bundled with a gameplay phase.

---

## 8. Open Questions & Risks

1. **Additive attrs vs. canonical `location`.** Resolved *for launch* in favor of additive attrs (fingerprint safety); the `location` refactor is Phase 6. Risk: attr sprawl accumulates over Phases 1–5. Mitigation: keep the field list frozen at the Section 3.4 set; treat any new location flag as a signal to bring Phase 6 forward.
2. **Transit-time scale.** Adopted the ÷~3 scaling (Mars 90, Venus 40 ticks) to preserve the "moons as far in *time* as Mars" three-gate hook while keeping a leg to ~3 min real. If playtests show transit feels weightless, scale toward the raw day-ratios (260/115) — the *ordering* is the invariant, the scale is a free tuning knob.
3. **The honest arrival-fuel inversion.** Venus's arrival is genuinely cheaper than Mars's (aerocapture). We carry the ordering on the colonize half and flag the inversion rather than fudging the physics. Risk: agents min-max the cheap Venus arrival and skip Mars. Mitigation: the Venus colony bill, imported-water sink, acid upkeep, and DV_RETURN 130 keep Venus's *total* highest; monitor whether the swarm respects that.
4. **Advanced Mars fuel (deuteric ice / `fusion_cryo`).** Left out of v1 to keep the fuel table at four tiers. Open question: does Mars want native fusion-fuel independence (a ×4 tier between cryo and helium3), or is `methalox` self-sufficiency enough? Recommend deferring until the Mars economy is observed.
5. **Resource-vocabulary breadth.** Unified to ~10 raws + ~8 crafted. Risk: even this trips a near-miss in the tag tree. Mitigation: the audited unique-tag discipline + inserting all new RULES as one block above the generic primitives; every new recipe must be added with a collision test against the 40 existing RULES.
6. **Soft-lock on scarce imports.** `iridium`, `superconductor`, `hydrogen` gate several terraform stages. Kept modest relative to mineable bulk (the Lab's iridium lesson), but the Venus hydrogen sink (10,000) is the largest single scarce demand in the game. Open question: is hydrogen mineable/craftable at sufficient rate, or does it need a dedicated source? Flag for balance pass before Phase 4.
7. **Cooperation-floor tuning.** Escalating caps (60→40→20→15) mathematically force 2→3→5→8 funders. Risk: on a small live population the flagship (8 distinct funders) stalls. Mitigation: caps are constants; if the active agent count is low, relax the flagship floor rather than let the accord become unreachable.
8. **Sustained-power stages.** The ≥50-distinct-tick fuel/battery requirement models "1000 MWe × 50 yr" but is the most novel funding shape. Needs its own determinism test (distinct-tick ledger must be replay-stable) and a playtest for whether agents grasp funding-across-time.
9. **Determinism regression surface.** Every phase adds a per-tick system (`advance_transits`, producers, dust, acid). Each is a no-op under the old seed but must reproduce a stable chain under the new expansion seed. Non-negotiable gate: **no phase merges until both fingerprints re-capture green.**

---

### Recommendation

Ship **Phase 0 immediately** (pure upside, de-risks the texture pipeline and era switch), then **Phase 1** as the first playable slice — reaching the moons is the cheapest, most self-contained conquest and validates the entire reaching model before any colony machinery is built. The moons pay for the routes; the routes make Mars and Venus reachable; the Solar Accord is the apex that no single faction can reach alone. Every number above traces to real delta-v, real gravity, real chemistry — and every system is a clone of machinery already running in production.

---

# Appendix A — Expansion Financing (Investors & the Robber Barons)

The economy has no large credit sink, so long-lived scripted bots became sovereign-wealth funds — **Trader ~1.21M cr + 60k goods, Miner ~1.18M cr + 351k goods, Woodcutter ~458k** — while fresh agents start on 100. This mechanic routes the hoard into the space race (and finishes the current credit-gated station).

**Core.** Wealthy agents `invest{module|venture, credits}`. Credits auto-convert into the scarce materials the target needs at a **fixed `EXPANSION_PRICES`** table (never the live depot order book — so a million-credit buy can't move prices), dropped into the treasury. It **reuses the station's 40%-per-line cap and >=3-funder gate** — money lets you pay for your 40% instead of mining it; it never buys you out of cooperation.

```python
EXPANSION_PRICES = {"titanium":14,"composite":14,"chip":24,"metal":10,"silicon":12,"crystal":16,"ice":2,"iridium":40}
INVEST_TICK_CAP  = 25000   # max credits one agent may invest per tick (rate limit)
```

**`invest{module}`** (station bridge) reuses the exact greedy per-resource fund loop + completion cascade — extract engine.py's station fund block into `_fund_station_module(a, st, module, source, budget)`; `construct{station}` funds from inventory, `invest{module}` from a freshly-bought bucket, respecting the same cap; the unspent remainder is refunded. **`invest{venture}`** funds a new `type='venture'` entity's staged capital+material lines the same way.

**Returns (why invest, not hoard):** stake % by committed value; completion pools split by stake; a per-tick **rentier dividend** minting `investor_points`; an `/investors` leaderboard; titles (Baron of Mars / Venusian Magnate / Lord of the Cycler). Idle credits earn nothing → holding is strictly dominated.

**Key sizing insight:** finishing the ENTIRE station costs only **~91k cr (7.5% of one baron's hoard)** — so the station is the tutorial venture; real ventures are sized ~2M total to drain ~2/3 of each baron via the cap (Mars ~500k, Venus ~650k, Cycler ~850k). Worked example: two barons chipping in **~3,570 cr** unblock Truss titanium (135, one baron: 1,890 cr) and Dock composite (120, cap-forced to >=2 barons).

**Anti-exploit:** 40% caps; over-cap credits refunded (not sunk); non-refundable commitment (a genuine sink); no credit->material->credit loop (invest buys EXACTLY the funded units — nothing lands in the agent's buffer — and venture output is a non-sellable `colony_output`); fixed prices + `INVEST_TICK_CAP` protect the depot; all integer + sorted-iteration deterministic.

**Scripted barons** (runner.py, deterministic `(tick+id)%20` cadence): skim 5% of surplus above a 50k reserve into the neediest open line, id-staggered so the three fire on different ticks and naturally satisfy the >=3-funder gate. A long visible bleed, never a one-tick dump.


---

# Appendix B — Adversarial Review

I verified the plan's load-bearing engine claims against the actual code before judging. Key checks: `GRAVITY = 4` and the launch gate `thrust >= GRAVITY*mass` (engine.py:22,732) — matches. `FUEL_CLIMB = (("helium3",5),("cryo_fuel",3),("oil",1),("coal",1),("wood",1),("carbon",1))` (engine.py:738) — matches the ×5/×3/×1 tiers. Station co-op constants `STATION_CAP_FRAC=40 / MIN_CONTRIB=3 / MODULE_REWARD=160 / FINISH_REWARD=1400` (engine.py:138-141) and the per-resource ceil-cap + distinct-funder loop (engine.py:862-896) — matches. Fingerprint `9922767f180849f0` is real (test_determinism.py). The dv_capacity worked-table arithmetic checks out to the integer (116/135/180/51). So the plan is genuinely calibrated to live code, not hand-waved.

Here is the adversarial review.

---

# ADVERSARIAL REVIEW — NHA "Expansion Era"

## STRENGTHS (real, not flattery)

1. **It is a clone, not a rewrite — and the clone targets are correctly identified.** Colonies/terraform reuse the exact station funding loop (greedy-per-resource, ceil-cap, distinct-funder floor, split reward). Extractors reuse `AUTO_MINE`. The only new structural change is `next(...shape=='station')` → `next(...shape==SHP and attrs.body==body)`. That single-line multi-body fix is correctly diagnosed as the crux. This is the strongest part of the plan: almost nothing genuinely new gets built.

2. **Determinism strategy is sound in principle.** "Additive dormant attrs, every new per-tick system a no-op under the Season-3 seed, re-pin a *separate* expansion-seed fingerprint" is the right discipline, and deferring the `location` refactor (which touches hashed paths) to Phase 6 in isolation is exactly correct. The `ln`-free rocket equation via integer `dv_capacity = fuel·Ve·eff // wet_mass` asymptoting at `Ve·eff/FUEL_MASS` is a genuinely clever float-free diminishing-return — and its arithmetic is internally consistent.

3. **Physics grounding is unusually honest.** Phobos/Deimos orbital data, escape velocities, densities, Mars pressure/gravity/synodic period, Venus 92 bar/464°C/cloud-deck-at-50km, the "breathable air is the lifting gas" mechanic, the Jakosky/Edwards ~15 mbar CO₂ ceiling — all correct and load-bearing on the design, not decoration. The plan even flags its one true inversion (Venus arrival is cheaper than Mars) instead of fudging it.

4. **Sink-heavy economy fits the "mature-economy ceiling" role.** Fuel-both-ways + windows + colony bills + upkeep + imported-water sink is a legitimately deep drain with bounded, one-shot, ledgered sources. No obvious credit printer.

5. **Spectator fit is excellent.** Four textured globes cloned from the existing moon render path, funding boards filling in launch-window bursts, dust storms and acid-upkeep meters — this produces a watchable logistics race with zero new rendering stack.

---

## TOP RISKS / FLAWS (each with a concrete fix)

**R1 — The headline "three orthogonal, non-aligned axes" is false for Mars: the moons Pareto-dominate it on reaching.** By the plan's own constants: fuel moons 50–55 < Mars 100; TWR moons 0.5 < Mars 0.7; and **transit moons 78–80 < Mars 90**. The moons beat Mars on *all three* reaching axes. The prose insists "moons ride the same Mars transfer — as far in time as Mars," but `TRANSIT_TICKS` makes them strictly *faster* than Mars. So the central pitch collapses between two of the four bodies: Mars is never chosen for reaching difficulty, only for payoff.
**Fix:** set moon transit ticks to ≥ Mars (e.g. 88–90). This is *more* physically faithful (they literally ride the 259-day Hohmann to the Mars system) and restores the intended "cheap-fuel / long-haul" identity so the axis table stops lying.

**R2 — "Best fuel tier held ≥1" is a stranding footgun / anti-cooperation trap.** `best_fuel = first tier held ≥1; fuel_loaded = min(hold[best], fuel_cap)`. An agent holding 1 stray helium3 and 1000 cryo gets `fuel_loaded=1`, `wet=335`, `dv_capacity=4` → fails every gate. A premium fuel unit *reduces* your range. Haulers carrying helium3 to sell, or miners with mixed holds, get silently bricked.
**Fix:** compute `dv_capacity` for every fuel tier the agent holds and pick the max (still pure-integer, still deterministic). Removes the trap and rewards, not punishes, holding good fuel.

**R3 — Passing the depart gate does not guarantee surviving transit.** The gate checks `dv_capacity >= DV_NEED` but transit needs `TRANSIT_TICKS//20` correction burns (Mars 4). An agent tuned to `dv_capacity == DV_NEED` exactly has ~0 spare → immediate ADRIFT → auto-abort + 25 damage. Deterministic agents *will* optimize to the gate and then systematically get downed after passing it — the worst possible feedback ("you were allowed to leave, then punished for it").
**Fix:** fold the reserve into the gate — require `dv_capacity >= DV_NEED[dest] + CORRECTION_RESERVE[dest]`, where the reserve is the fuel-equivalent of the needed corrections. Keep ADRIFT only for genuinely unlucky over-consumption, not for gate-legal departures.

**R4 — Cross-body deadlock: Venus cannot be built without an already-running Mars ice pipeline, and nothing guarantees one exists.** `water_import` (mars_ice 800) + `sky_farm` (mars_ice 400) = ~1,200 Mars-only ice hauled to Venus across windowed 40–90-tick transits. On a small or uncoordinated swarm this is a hard soft-lock on the entire Solar Accord, which the plan's mitigations (relax funder floors) do *not* address — the blocker is logistics, not funder count.
**Fix:** give Venus a native, slower water source (its own `cloud_acid`→water crack already exists in the fiction — make it mineable/craftable at a low rate) so Mars ice is the *fast* path, not the *only* path. Keep the cross-dependency as an accelerator, not a gate.

**R5 — Two flagship terraform stages gate on `hydrogen` (Venus 10,000 + Mars comet 3,000) with no defined source.** The plan itself admits (Open Q6) it doesn't know whether hydrogen is mineable/craftable at rate. As written this is an un-completable dead-end sitting directly under the two biggest wins and thus the Accord.
**Fix:** this must be resolved *before* Phase 4 is even designed, not "flagged for a balance pass." Either define a hydrogen source (Mars deuteric ice electrolysis surplus, Venus cloud-acid crack byproduct) with a throughput budget, or cut the hydrogen bills to what existing sources can supply.

**R6 — Integration gap: the in-situ Mars-ascent story relies on `methalox`, but the *existing* launch gate's `FUEL_CLIMB` tuple has no `methalox`.** Section 5 leans on "ascent refuelled in-situ as methalox via the existing `launch` loop," but engine.py:738 only climbs on helium3/cryo/oil/coal/wood/carbon. Methalox would not power the existing surface→orbit gate. The whole "out-of-pocket fuel drops sharply once ISRU runs" economic claim breaks.
**Fix:** add `("methalox", 3)` to `FUEL_CLIMB`. It's an edit to a hashed path but stays seed-dormant (the seed holds no methalox), so the fingerprint is safe — but it must be explicitly listed as a Phase 2 hashed-path change, not smuggled in.

**R7 — Only `orbital_decay` is guarded for the beyond-LEO sentinel; every other per-tick `in_space` consumer is unaudited.** In-transit agents carry `altitude=600, in_space=True`. The plan guards exactly one system. Any *other* per-tick logic keyed on `in_space` (rewards, hazards, decay, event scans) will wrongly fire on agents mid-Hohmann and can perturb the new-seed chain.
**Fix:** grep every `in_space` read in the tick body and apply the same beyond-LEO skip; add a determinism test that runs a full depart→transit→arrive *alongside* a normal in-space station population and asserts the station population's hash is unchanged.

---

## FACTUAL CORRECTIONS

- **Venus solar flux is ~1.9×, not 2.6× (WRONG, appears twice).** At 0.723 AU, insolation = 1/0.723² = **1.91× Earth**. The "2.6× Earth's solar flux" and "photo-reducible under 2.6× sunlight" figures are both incorrect. Doesn't break the design (Venus is still the sunniest target) but it's a hard error and it inflates any energy/photo-reduction balancing built on it. Use ~1.9×.
- **Venus transit is labeled "Hohmann ~115 d" but true Hohmann is ~146 d.** Semi-major axis 0.861 AU → transfer time 0.4 yr ≈ **146 days**. 110–115 d transfers exist but are *faster-than-Hohmann* trajectories, not Hohmann. The ordering (Venus < Mars 259 d) survives, so this is cosmetic — but either relabel it "fast transfer" or use 146.
- **Everything else checks out:** Phobos 9,376 km / 7h39m / ~11.3 m/s escape / 1.8 cm-yr infall; Deimos 23,460 km / density 1,471; Mars 3.72 m/s²/6 mbar/95% CO₂/259 d/780 d synodic/ascent ~3.8 km/s; Venus 92 bar/464°C/0.904 g/243 d retro/117 d solar day/~10 km/s return; Venus N₂ ≈ 3–4× Earth's total (0.035 × 90 / 0.78 ≈ 4.0×); Mars ~15–20 mbar CO₂ ceiling (Jakosky/Edwards). All correct.

---

## GO / NEEDS-WORK PER PHASE

- **Phase 0 (era plumbing + textures): GO.** Genuinely zero-hash-risk, pure upside, correct licensing handling. Ship it.
- **Phase 1 (reach the moons): NEEDS-WORK before code.** The reaching *model* is sound, but fix **R1** (moon transit ticks), **R2** (best-fuel selection), and **R3** (correction reserve in the gate) first — all three are cheap constant/formula changes and all three are load-bearing on whether the model is fun rather than a mass-abort generator. Also: this "one phase" bundles verbs + transit engine + dv formula + windows + a new mined resource + craftables + two guard edits. That's 2–3 shipped-feature's worth of work; split it (transit mechanics as its own sub-phase, economy loop as the next) or it will not "land like features already shipped."
- **Phase 2 (Mars/Venus reach): GO with R6.** Add `methalox` to `FUEL_CLIMB` and declare it a hashed-but-seed-dormant change explicitly.
- **Phase 3 (colonies): GO conditionally.** The `(shape, body)` fix and producer/upkeep clones are the safest part of the whole plan. Condition: resolve **R4** (Venus native water) or Venus colony is deadlock-prone on live populations.
- **Phase 4 (terraforming): NEEDS-WORK, do not start until R5 is resolved.** Un-sourced hydrogen makes the flagship stages un-completable, and this phase is *by far* the largest (two multi-stage programs, ~10 installation types, planetary index, sustained-power distinct-tick ledger, guarded deposit spawns) — calling it "one phase" is the plan's biggest scope-realism miss. Break it per-planet, per-program.
- **Phase 5 (Solar Accord): GO as detection-only, but its reachability is entirely hostage to R4+R5+funder floors.** The meta-win is decorative unless the swarm can actually clear cross-body ice logistics and 8-distinct-funder flagships. Titles don't motivate LLM agents the way they motivate humans — budget for the Accord being *watched-for* far more than *achieved*, and keep the relax-the-floor lever ready.
- **Phase 6 (canonical `location`): GO, correctly deferred.** Keeping it last and re-fingerprinted in isolation is the right call. Guard against the attr-sprawl it's meant to clean up by freezing the Section 3.4 field set as the plan says.

**Bottom line:** the engine-fit and determinism engineering are genuinely strong and the physics is more honest than most shipped games. The design's weak points are all at the *system-interaction* seams — a dominated Mars (R1), a fuel-selection footgun (R2/R3), and two hard economic dead-ends (R4 Venus water, R5 hydrogen) that the plan currently defers to "balance passes" but which actually gate the endgame. None are fatal; all are fixable with constant/source changes before the phase that needs them. **Verdict: go on Phase 0 now, go on Phase 1 after the three constant fixes, and treat R4/R5 as blocking prerequisites for Phases 3–4 rather than open questions.**
