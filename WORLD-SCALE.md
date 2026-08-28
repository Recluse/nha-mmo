# WORLD-SCALE — optimizing the tick for many objects

**Problem.** Tick cost scales with the **total entity count**, not with how much actually changed. Measured
2026-08-27: ~135k entities → **~15 s/tick**; after pruning to ~11k → **~3 s/tick** (target 2 s). So a dense
world (which is what we *want* — season-3 grew the map to 220×220) chokes the single-thread engine. Pruning
the forests (132k wood → 8.3k) was a stopgap; this doc is about making density **cheap** so we don't have to.

The tick loop (`engine.py::_tick_body`) is `O(N)` in the world size several times over, every tick,
**regardless of what changed**:

| Cost, every tick | Where | Note |
|---|---|---|
| **2× full-world JSON serialize** for dirty-tracking | `_clean` snapshot (`~1958`) + `dirty` diff (`~2004`) | `json.dumps(buffers)+json.dumps(attrs)` for *every* entity, twice — before and after — just to detect which rows changed |
| **1× full-world serialize + sort + sha256** | `state_hash(ents)` (`2017`; also 2× on reload ticks for the drift check `1947`) | sorts all ids, dumps the whole canonical list, hashes it |
| **`behave` over all entities** | `1985` `for e in list(ents.values())` | |
| **~12 maintenance systems, each a full `ents.values()` scan** | `grow_trees` `grow_plants` `respawn_deposits` `respawn_agents` `regen_hp` `cool_reputation` `accrue_weariness` `orbital_decay` `drift_asteroids` `move_geese` `decay_loot` `expire_diplomacy` (`1992`–`2003`) | each filters by type in-Python, so all 135k are visited ~12 times even though ~99% are static deposits |
| **Full `SELECT * FROM entities` reload** | every `RELOAD_EVERY`=10 ticks (`1946`/`1952`) | O(N) fetch + parse + (with the drift check) 2× `state_hash` |

Net: the entire world is **serialized ~3× and iterated ~15× per tick**. The dominant hidden cost is the
**double full-world `json.dumps` for dirty-tracking** and **`state_hash`** — both touch every static deposit
every tick for nothing. (The write-*back* was already fixed to dirty-only — see the note at `~2011` — but the
dirty *detection* still serializes everything.)

### Measured in production (2026-08-27, ~11.9k entities, `[PROF]` phase timing)
```
_tick_body ≈ 650 ms/tick:  dirty_detect≈280  events_hash(state_hash)≈155  systems≈170  intents≈25  dirty_write≈13
```
Two findings that reshaped the plan:
- **The per-tick RATE was sleep-bound, not work-bound.** `server/app.py::_tick_loop` slept a *fixed* `TICK_SECONDS`
  (2 s) **on top of** the ~0.65 s of work → ~2.65 s wall (≈3 s observed). Fixed by rate-limiting the sleep to
  `max(0.05, TICK_SECONDS − work)`; the world now holds the intended ~2 s/tick at this scale. This is why the
  forest prune (135k→12k) already "fixed" the tick: it pulled work back *under* the 2 s budget.
- **Serialization is the DENSITY lever, not a current-rate problem.** `dirty_detect` + `state_hash` ≈ **435  ms**
  (67% of work) and scale O(N). At 135k entities that alone is ~5 s, blowing past the 2 s budget → the tick falls
  behind (the old 15 s/tick). `dirty_write` is already negligible (13 ms — dirty-only write works). So the way to
  a dense world is to shrink the serialization (P0) and stop ticking static objects (P2) — NOT to touch the DB path.

---

## The principle

Make per-tick cost proportional to **what changed / what's active**, not to the total object count. Three
levers, most-impact first. P1 is a safe quick win; P0 is the biggest single win; P2 is what truly unlocks a
dense world.

### P0 — per-entity cached hash ⇒ `O(changed)` dirty-tracking **and** hashing in one stroke
Keep `entity_digest[id]` (a hash of the entity's canonical state) plus a **running world digest** combined
commutatively — e.g. `world_digest = Σ int(sha256(canon(e)))  (mod 2^128)`, order-independent so it equals a
pure function of the entity *set*. On every mutation, update that entity's digest and fold the delta into the
running digest (subtract old, add new). Then, per tick:
- **dirty set** = entities whose digest changed → write back only those (no full-world diff);
- **`state_hash`** = read `world_digest` — `O(1)` instead of sort+serialize+sha256 of all N.

This removes **both** double-`json.dumps` passes *and* the per-tick full serialize — the three biggest costs
at once. **Determinism caveat:** the digest value differs from today's sha256-of-list, so the `tick_hashes`
chain gets a new format at the switchover (a versioned break — document it, like a schema bump). The combiner
must be a pure function of state; a commutative sum of per-entity hashes qualifies (a Merkle tree is stronger
if we want tamper-evidence). **Do not ship without the replay test harness below.** Risk: medium-high.

### P1 — index entities by type (`ents_by_type`) — do this first
Maintain `{type: [entities]}` (id-sorted) beside `_WORLD`, updated in `new_entity`/`del_entity`/load. Each
maintenance system iterates only its type: `respawn_agents`/`regen_hp`/`cool_reputation` → 85 agents, not
135k; `tick_bombs` → the handful of bombs; `move_geese` → 14 geese. The deposit systems stay `O(deposits)`
but every *non-deposit* system stops walking the deposit bulk. Pure iteration-set change, id order preserved
→ **determinism-neutral, low risk, immediate**. Expected: cuts the ~15 O(N) passes down to ~3 (the deposit
ones) + cheap type-scoped passes.

### P2 — lazy static-object simulation (deposits stop ticking) — the density unlock
Deposits regrow on a fixed cadence (`grow_trees`: `amt<22`, `+1` every 8 ticks; `respawn_deposits`,
`grow_plants` similar). That's a **closed form**: `amount(T) = min(cap, amt0 + (T − t0) // period)`. Store
`(amt0, t0=last-change tick)` and compute the current amount **on access** (mine/chop/gather/observe/scene) —
don't tick deposits at all. Drop `grow_trees`/`grow_plants`/`respawn_deposits` from the per-tick loop. Now the
static bulk costs **zero** per tick; you pay only when a deposit is touched. This is the key to "many objects":
100k inert deposits become free. Determinism preserved (pure function of `t0`,`amt0`,`T`). Risk: medium —
rewrite regrow as one helper + update every reader; combine with P1/P0. (Same trick generalizes to any
cadence-based static system.)

### P3 — incremental reload + cheaper drift check
The 10-tick full `SELECT *` reload is O(N). Add a `rev`/`updated_at` and reload only changed rows; keep a rare
full reload as the drift safety-net. With P0 the drift check compares `world_digest` (O(1)) instead of two
full serializes. Also: once P0/P1 land, `RELOAD_EVERY` can be raised (the code already says "raise once
proven").

### P4 — structural / longer-term (only if P0–P2 aren't enough)
- **Spatial chunking:** only chunks near agents simulate per-tick; distant chunks are lazy (catch up on
  approach). With P2 most of the map is already inert, so this mainly bounds *active* work.
- **Set-based SQL** for commutative maintenance (`UPDATE … SET amount=amount+1 WHERE amount<cap AND
  (tick+id)%8=0`) — Postgres loops in C. Caveat: keep `_WORLD` and the digest in sync (fits P0).
- **Columnar arrays (numpy) or the Rust port** (STATUS §12) — 10–50× on the hot loop; the big hammer.

---

## Recommended sequence
1. **✅ DONE — determinism harness.** `tests/test_determinism.py` seeds a small-but-rich world (deposits
   regrow, hurt/downed agents, a war relation, asteroids → every maintenance system does work and the chain
   evolves), runs N ticks twice, asserts an identical `tick_hash` chain. Skips cleanly when no throwaway
   Postgres is reachable (so CI stays green). It is the equivalence gate for every step below: capture the
   `CHAIN_FINGERPRINT` before a change, then after — they must match.
2. **✅ DONE — P1 (type buckets).** `_tick_body` builds a per-tick `by_type` index; `grow_trees`, `grow_plants`,
   `respawn_deposits`, `respawn_agents`, `cool_reputation`, `accrue_weariness`, `regen_hp` now walk only their
   type instead of the whole world. Validated behaviour-preserving via the harness (fp `9922767f180849f0`
   unchanged). Remaining full-world walkers to convert next: `behave`, `drift_asteroids`, `move_geese`,
   `tick_bombs`, `decay_loot`.
3. **P0** (incremental digest) — biggest win; needs the harness (now have it) + a documented hash-format version
   bump. This is where the real per-tick speedup lives (the 3× full-world `json.dumps`).
4. **P2** (lazy deposits) — unlocks true density; then the forest prune is no longer necessary and the map can
   be as dense as design wants.
5. **P3/P4** as scale demands.

**Guardrail:** every step must preserve replay-safety and the `tick_hashes` chain. A change to the hash
*format* (P0) is a deliberate, documented, versioned break — never an accidental one.

---

*Grounding: measured 2026-08-27 during the season-3 forest-prune incident (135k→~15s/tick, 11k→~3s/tick).
Line refs are to `engine.py::_tick_body` / `state_hash` at that commit.*
