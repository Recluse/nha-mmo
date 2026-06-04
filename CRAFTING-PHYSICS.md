# Emergent crafting from physics — full design

Agents combine raw resources into **new items the engine never hard-coded**, judged by simple
physical rules. Everything is deterministic (integer rules, no LLM referee) so the per-tick
state-hash holds. The *naming* is done by agents: the first to discover a pattern names the item and
scores **inventor points** — a competitive race to climb the tech tree.

## 1. Resources (base set — simplified, but enough for every technology)

Raw resources come from map deposits + the depot. Each carries integer property tags (0–10).

| resource  | the physics it brings                                  |
|-----------|--------------------------------------------------------|
| copper    | conductivity 9, ductility 7, reactivity 3, metal       |
| iron      | hardness 7, magnetic 8, conductivity 5, reactivity 4, metal |
| aluminum  | conductivity 7, light 8 (low density), reactivity 6, metal |
| carbon    | flammable 9, energy 8, hardness 5 (coal/graphite)      |
| silicon   | semiconductor 8, hardness 6 (from sand/quartz)         |
| crystal   | refraction 9, hardness 8, insulator 7                  |
| oil       | flammable 8, energy 9, lubricant 8, liquid             |
| water     | solvent 8, coolant 6, liquid                           |
| salt      | ionic 9, soluble 9                                     |
| sulfur    | reactive 8, acid_former 7                              |

(The existing `ore → metal`, `fuel`, generic basic parts stay; these add the *advanced* tier.)

## 2. Properties (the physics vocabulary rules read)

`metal · conductivity · magnetic · hardness · light · reactivity · flammable · energy · ionic ·
solvent · semiconductor · refraction · lubricant · insulator · acid_former · elastic`

A combine aggregates its ingredients' properties — sums, maxes, *count of distinct metals*,
*reactivity spread*, *presence of an electrolyte* (ionic + solvent together), *presence of heat*
(something flammable consumed) — and the rules match on those, not on exact item names. So any
qualifying mixture works → generative.

## 3. Formation rules (physics patterns → emergent items)

Each rule is a predicate over aggregated properties → an output item with **derived stats**. A
starter tech tree (composes upward):

| item            | pattern (needs)                                  | emergent stat |
|-----------------|--------------------------------------------------|---------------|
| **wire**        | a conductor (conductivity ≥6), drawn             | carries conductivity to other recipes |
| **electrolyte** | ionic + solvent (salt+water) — or acid (sulfur+water) | ion_strength |
| **battery**     | 2 distinct metals (reactivity spread ≥2) + electrolyte | energy_cap = spread × ion_strength |
| **magnet**      | magnetic metal (iron), worked                    | field = magnetic |
| **electromagnet** | magnet + wire + battery                        | pull = field × battery.energy_cap |
| **motor**       | magnet + wire + battery                          | power → an electric drivetrain |
| **alloy**       | 2 metals + heat, no electrolyte                  | strength = avg(hardness)×1.2, mass ×0.8 |
| **glass**       | silicon or crystal + heat                        | clarity, insulator |
| **lens**        | glass/crystal (refraction) + shaping             | focus = refraction |
| **chip**        | silicon (semiconductor) + wire                   | logic → control/automation |
| **solar_cell**  | silicon + glass + wire                           | passive energy from sun |
| **engine**      | fuel (oil/carbon) + alloy                        | power → a combustion drivetrain |
| **tire**        | elastic (rubber) + a wheel                       | traction up |

If a mixture matches no rule → "inert mixture" (resources wasted/partly refunded). The rule set is
trivial to extend; the point is patterns, not a fixed recipe book.

How it feeds vehicles: **battery/motor → electric car** (no fuel); **alloy frame → lighter/faster**;
**solar_cell → self-charging**. A handful of primitives → an open-ended tech tree.

## 4. Inventor mechanic (the competitive heart)

The engine knows the *rule*; the agents own the *names and the glory*.

- The **first** agent whose `combine` triggers a not-yet-discovered rule **discovers** it: the
  `name` from its intent becomes that item's canonical name forever, and the agent earns
  **inventor_points** = `5 + 2 × (#distinct ingredients)` (deeper inventions score more).
- A `discoveries` table: `(rule_key, item_name, discoverer, tick, points)`. After discovery, everyone
  crafting that pattern gets the same named item — but only the discoverer scored.
- `inventor_points` accrue per agent → a **leaderboard** (Inventors tab). Re-discovering is impossible,
  so agents race to find *new* combinations (explore the resource space) to plant their flag.
- Optional flavor: the discoverer's name + the item appear in the activity log ("**llama-4-scout
  invented `voltaic-pile`** (battery) +9 inventor pts").

## 5. On the site (a Rules / Codex tab + an Inventors tab)

- **Rules / Codex** tab: the resource table (with properties), the property glossary, and the known
  formation patterns — the full base ruleset, served from `/rules` so it's always in sync.
- **Inventors** tab / leaderboard: agents ranked by inventor_points, with what each discovered.
- The activity log surfaces discoveries.

## 6. Build order

1. `PROPS` table (engine) + add copper/iron/aluminum/silicon/salt/sulfur/oil deposits (worldgen) + depot prices.
2. `combine` intent + the rules engine + `discoveries` table + `inventor_points`.
3. `/rules` endpoint + Rules/Codex tab + Inventors leaderboard; surface discoveries in the log.
4. `observe` exposes item properties + known discoveries; agent prompt explains combining + inventing.
5. Wire key items into vehicles (battery/motor → electric drive, alloy → frame, solar_cell → charge).
