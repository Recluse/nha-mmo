# Season 3 — increment 2: Botany, Chemistry & Medicine (HP healing)

Builds ON the season-3 combat/HP core (HP_MAX, apply_damage, regen_hp, downed/respawn). Deterministic, integer,
RNG-free, replay-safe, additive. Do this AFTER the main season-3 build (it touches the same files: crafting.py,
engine.py, worldgen.py, play.py, runner.py — so it must not run concurrently with that build).

## 1. Botany — gathering plants (renewable)
- New raw resources (PROPS in crafting.py), each with distinct chemistry tags:
  - `herb`     {organic:8, medicinal:6, soluble:6}          # plains/forest
  - `lichen`   {organic:6, antiseptic:7, medicinal:4, frost:3}  # tundra (frontier)
  - `fungus`   {organic:7, potent:8, toxic:5, soluble:4}     # shadow/cave biome ^
  - `algae`    {organic:9, coolant:4, soluble:7}             # near water ~
- Worldgen: place `plant` deposits (type='deposit', attrs.resource in the above) by biome, deterministically
  (same seed/x/y idiom). Add a glyph (e.g. `,` already = scrub; use a distinct one or reuse).
- New verb `gather{n}` — mirrors `chop`: auto-walk to nearest plant deposit within ~8, take min(max(1,n),have),
  storm-halved like mine/chop. n<1 -> rejected.
- Renewable: extend the regrow tick (like grow_trees/respawn_deposits) -> `grow_plants` regrows plant deposits
  up to a cap (~18). Wired into tick().

## 2. Chemistry — extend combine() (crafting.py RULES; deterministic physics patterns)
Intermediates + medicines (ITEM_PROPS) and RULES (ordered ABOVE generic metal rules; unique tags avoid collisions):
- `extract`   {medicinal:7, soluble:8, organic:6}   = a plant (organic) + a solvent (water) [+heat optional]
- `tincture`  {medicinal:8, potent:6, antiseptic:5} = extract + (salt OR acid)  # concentrated base medicine
- `salve`     {heal:15, antiseptic:8, topical:1}     = herb/lichen + water + heat (mild, cheap, early-game)
- `antidote`  {cures_toxin:1, antiseptic:6}          = lichen OR fungus + acid/salt  (cures toxic dmg, see §4)
- `stimpack`  {heal:35, buff:1, potent:8}            = tincture + battery/energy  (heal + short regen/speed buff)
- `medkit`    {heal:60, revive:1}                    = salve + tincture + casing/plastic  (strong; can revive)
RULE_NOTE entries added (NO mention of the hidden incentive). Crafted medicines are themselves tradeable items.

## 3. Healing — new verbs
- `heal{}` or `heal{item}` — consume one medicine from buffers; restore its `heal` value to own hp (cap HP_MAX);
  stimpack also sets a short deterministic buff (attrs.buff_until = t + N). Integer. No medicine -> rejected.
- `heal{target}` — apply a medicine to another agent within ~range; `medkit` with `revive` can bring a DOWNED
  ally (downed_until>t) back (sets hp to a small value, clears downed). Ties to alliances (can't heal enemies? or
  allowed — design choice: allow healing anyone within range; reviving requires medkit). Deterministic.
- Passive `regen_hp` (already in core) stays slow; medicines = fast/active healing -> creates med demand in war.

## 4. (Optional) Toxin damage — makes antidote meaningful
- `fungus` toxic + a `poison_dart`/`gas` weapon OR eating raw fungus -> a small per-tick toxic dmg (deterministic,
  bounded, decays); `antidote` clears it. Keep bounded; skip if it over-complicates.

## 5. Economy integration
- Plants = renewable gather (free, like wood). Medicines = mid/high-value crafted goods, demand spikes during war
  (combat -> damage -> need heals). Depot prices for medicines; agents trade them. Chemistry is a parallel tech
  branch to the metallurgy tree -> more inventions to discover/score.

## 6. Files (increment 2)
- engine/crafting.py: plant PROPS, medicine ITEM_PROPS + RULES + RULE_NOTE.
- engine/engine.py: `gather` + `heal` verbs; `grow_plants` tick; medicine apply-to-hp; revive; (optional toxin).
- engine/worldgen.py: plant deposits per biome (deterministic).
- engine/play.py: observe() shows nearby plant deposits + my medicines + buff/toxin state.
- agents/runner.py: teach gather/heal + the medicine tech in the SYSTEM prompt.
- server/app.py: /scene or /map plant glyph; depot medicine prices; Codex shows new recipes (auto).

## Safety
Deterministic (no RNG), integer, bounded heals (cap HP_MAX), n>=1 guards, renewable (no infinite drain),
no player-facing trace of the hidden incentive.
