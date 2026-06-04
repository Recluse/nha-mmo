# Emergent crafting from physics (design)

Agents shouldn't be limited to a fixed parts list — they should **combine raw resources into NEW
items the engine never hard-coded**, judged by simple physical rules. Copper + aluminum + water +
salt → an electric battery, because that's a galvanic cell. Everything stays deterministic (integer
rules, no LLM referee) so the per-tick state-hash is preserved.

## 1. Resources carry physical properties

Each resource/material has integer property tags (a `PROPS` table). Properties — not item names —
are what rules match on, so the system is generative.

| resource  | properties                                   |
|-----------|----------------------------------------------|
| copper    | metal, conductivity 9, reactivity 3          |
| aluminum  | metal, conductivity 7, reactivity 6          |
| iron      | metal, conductivity 5, magnetic 1, react 4   |
| crystal   | hardness 8, refraction 6                     |
| water     | solvent 5, liquid 1                          |
| salt      | ionic 8, soluble 8                           |
| fuel      | flammable 9, energy 7 (acts as "heat")       |
| ore       | metal_ore 1                                  |

New raw resources (copper / aluminum / iron / salt) get their own map deposits (worldgen) and depot
prices, so agents can gather them.

## 2. The `combine` intent

`combine {"ingredients": {"copper":1, "aluminum":1, "water":2, "salt":1}, "name": "battery"}`

The engine:
1. checks the agent owns the ingredients (else reject),
2. aggregates the ingredients' properties (sums, maxes, counts of distinct metals, electrolyte
   presence = ionic + solvent together, …),
3. matches them against **discovery rules** (below),
4. on a match → consumes the ingredients and creates a new `item` entity owned by the agent, with
   **emergent stats derived from the inputs**; on no match → "inert mixture" (small refund or waste).

## 3. Discovery rules (physics patterns, generative)

Each rule is a predicate over the aggregated properties → an output template with derived stats:

- **battery** — ≥2 distinct metals with `|reactivity_diff| ≥ 2` **and** an electrolyte (something
  ionic + a solvent) → `energy_cap = reactivity_diff × electrolyte_strength × min(metal_qty)`.
  (A galvanic cell; dissimilar metals in an electrolyte make voltage.)
- **alloy** — ≥2 metals + heat (fuel), no electrolyte → `strength = avg(hardness)×1.2`, `mass×0.8`.
  (Metallurgy: stronger, lighter.)
- **electromagnet** — magnetic metal (iron) + a battery + a conductor (copper) →
  `pull = magnetic × battery.energy_cap`.
- **lens** — refractive (crystal) + heat → `focus = refraction`, enables optics / solar boost.
- **wire** — a conductor drawn out → `conductivity` carrier for other recipes.

Rules match **patterns**, not exact item lists, so any qualifying combination works and players
discover them. The set is easy to extend.

## 4. New items feed back into the world (emergent tech tree)

Produced items become inputs to vehicles and to further `combine`s:

- a **battery** → an **electric drivetrain** (a vehicle that drives without burning fuel),
- an **alloy frame** → lighter/stronger vehicle (higher v, more payload),
- an **electromagnet** / **lens** → future subsystems (rail launchers, solar focus).

So a few primitives + a few physical rules grow into an open-ended tech tree the agents explore —
"no limit on imagination," but grounded in physics.

## 5. Why deterministic (no LLM referee)

The tick loop hashes the whole world each tick for replay/audit. An LLM judging combinations would be
non-deterministic and break that. Property tags + integer rules keep `combine` fully deterministic
while still feeling generative.

## Build order
1. `PROPS` table + add copper/aluminum/iron/salt to worldgen deposits + depot.
2. `combine` intent + the rules engine (start with battery / alloy / electromagnet / lens).
3. Surface item properties in `observe` + the agent prompt so agents can experiment.
4. Wire key items into vehicles (battery → electric drive, alloy → frame).
