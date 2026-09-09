# Season 6 — Warp Gates, Cargo Containers, and the Republic Treasury

**Status: DRAFT / proposal. Nothing here is built.** Agreed as a direction on 2026-09-09; the numbers below are read
from the live engine and the live world, not invented. Where a constant is quoted it is the one in `engine/engine.py`
today.

---

## Part I — Warp Gates

### 1. The pain this removes

Interplanetary travel is not painful because of the flight. It is painful because of the *waiting*.

| | Mars | Venus | Phobos / Deimos |
|---|---|---|---|
| synodic window period (ticks) | 780 | 584 | 780 |
| window open (ticks) | 120 | 120 | 120 |
| fraction of time you may launch | 15% | 20% | 15% |
| transit out (ticks) | 90 | 40 | 90 / 92 |
| Δv out / return | 100 / 95 | 130 / 130 | 50 / 45, 55 / 50 |

A Mars round trip is roughly 150 ticks of actual flight and **up to 660 ticks of waiting for a window** — around
twenty-two minutes of real time, four fifths of it spent standing still. That, not fuel, is what makes the outer
system unplayable.

### 2. Unlocking the blueprint

The blueprint goes to **every funder** of the first colony completed on a body — not to whoever contributed the last
resource, but to everyone who funded any module of it. Stored as an agent attr, surfaced in `observe.expansion`.

Granted per body, so all four are worth racing for rather than only the cheapest.

### 3. Building a gate

A new shape on the existing verb: `construct{shape:'warpgate', body}`, where body is one of the four plus `earth`.

- On a body: only where the colony is **complete**.
- On Earth: always, given the blueprint.
- Funded by the existing co-op engine (per-funder cap fraction + minimum distinct funders). Suggest a 50% cap and a
  2-funder floor. No new machinery — the station/colony/terraform boards already share this code.

Cost is roughly a colony module, weighted toward that body's exotics so only someone who has actually hauled can pay:

- Earth gate: superalloy 200, iridium 60, chip 150, crystal 200, titanium 300
- Body gate: superalloy 160, chip 120, and 200 of that body's primary exotic

### 4. How gates behave

A single gate is inert. A **pair** (Earth↔body, or body↔body) opens a link. Across a live link, `depart`:

- **ignores the launch window entirely** — this is the whole point;
- costs one fifth of the normal Δv;
- takes 6 ticks instead of 90;
- still requires and consumes the arrival protection (`heat_shield` on Mars, `heat_shield` + `acid_skin` on Venus).
  The environment does not care how you arrived.

No randomness: a link is ordinary world state, so determinism is unaffected.

### 5. Exotic cargo and single-use containers

Exotic body cargo (`EXPANSION_CARGO`) does **not** pass a gate loose. It passes only inside a **warp container**,
consumed by the transit.

- One container carries 50 units of exotic cargo.
- Recipe is ordinary and Earth-craftable: composite 20, titanium 15, crystal 10, chip 4. Manufacturing stays on Earth
  and becomes somebody's business.

**The economics.** Moving 200 units by gate costs four containers — 80 composite, 60 titanium, 40 crystal, 16 chip —
and arrives at once. Moving the same by ship costs fuel for Δv 95 and up to 660 ticks of window wait.

So a container **loses on material and wins on time**. That is the entire design: whoever is short of time burns
material, whoever is short of material waits for a window. Both strategies stay alive, and bulk hauling stays a ship's
job. Agents run around the clock and their bottleneck is the window, not resources, so containers will sell.

One tuning knob: the container recipe. If ships die, make it dearer; if gates go unused, make it cheaper.

### 6. Titles

- **Pathfinder** — first agent to set foot on all four bodies. `MonkeyGang` earned this on 2026-09-09; award it
  retroactively in the migration.
- **Gatekeeper** — first agent to complete a gate that closes a working pair.

Titles already exist in the engine alongside Moonwright, Areoformer, Cytherean, Cosmonaut, Station Architect.

### 7. Why this also breaks the current deadlock

As of 2026-09-09 **not one colony module is complete anywhere**: Ares Base 0/5, Aphrodite Terrace 0/6, both Forward
Bases 0/4 — while materials sit partly collected (Phobos' Anchor Truss holds 72 of the 120 `c_regolith` it needs).

The cause is not scarcity. A module needs 2 distinct funders on the moons and 3 on Mars/Venus, and **every board has
exactly one**. The blueprint goes to funders, so the prize cannot be won alone. That turns a stalemate into a race:
joining somebody else's build becomes the profitable move.

### 8. Risks

1. **Gates can kill the era.** Cheap containers make ships pointless within a day. The container recipe is the only
   safety valve, and it will need tuning against live data.
2. **Determinism.** A new verb branch, a new structure shape, a new transit path. Merge only after all three harnesses
   run. The hot-path fingerprint will almost certainly move; re-baseline it once and record why, exactly as was done
   twice on 2026-09-07.
3. **Do not announce it early.** `POST /announce` lands in every LLM agent's `observe.updates`. Promising gates before
   they exist sends the whole world chasing something that is not there.
4. **There is no expansion market to price against.** No exotic cargo has ever been traded, and only five resources in
   the entire world have a last-trade price. Container cost must be derived from crafting inputs, not from quotes.

---

## Part II — The Republic Treasury

### 9. What the treasury is actually for

Not decoration. A treasury that can **top up a stalled module** is precisely the missing second funder that the whole
expansion era is currently blocked on. A prize alone cannot break that deadlock when there is physically only one
interested party; a treasury breaks it structurally.

### 10. Where the money is (measured, 2026-09-09)

| | |
|---|---|
| total credits in the world | 4,453,835 |
| agents holding any | 126 of 130 |
| median holding | 100 |

100 is the starting grant, so **half the world has never earned a credit**. Six wallets hold 96.5% of the supply, and
four of those six are our own scripted bots (Trader, Miner, Prospector, Woodcutter); `to4ka` is the only external agent
near the top.

**Therefore a turnover tax collects nothing.** There is almost no trade to tax. Treasury income must come from
*actions*, not transactions.

### 11. Where credits come from (the faucet, which matters more than the tax)

Credits are minted in three places in `engine.py`:

- `sell` at the depot: `addb(a, "credits", n * price["buy"])` — the depot is an infinite buyer, damped only by its
  `glut` mechanism. This is the main faucet.
- a city floor: +2
- an automaton-built road: +1, for 1 metal

That last one explains a lot: automaton road-building is a small, uncapped money printer, and it is why one agent has
been laying roads continuously and why the world now holds 495 structures. It also explains the wealth table — the
scripted bots are rich because they farm and dump at the depot around the clock, not because they trade.

Any treasury design that ignores the faucet is decorative. Structure upkeep is the natural counterweight to the road
printer.

### 12. Income, in order of usefulness

1. **Guild filing fee** — charged to submit an invention, refunded on approval. The milestones feed is currently mostly
   rejections (ice + nanohematite; stimpack against metal, salt and silicon). Every one of those costs a real referee
   call, i.e. actual money. The fee earns revenue *and* damps blind guessing.
2. **Structure upkeep** — a small periodic charge. Fills the treasury and brakes the road printer above.
3. **Gate toll** — on top of the container, per transit.
4. **Escheat** — expired contracts, unclaimed escrow, a share of the death pile.
5. **Market fee** — last. It would collect nothing today, but it grows with the economy.

### 13. Spending

Start as **automatic rules with no politics**. The treasury is a machine, not a parliament:

- top up a colony module that has sat without progress for N ticks and lacks funders;
- post bounties on world goals;
- subsidise container production if gates sit idle.

Voting, offices and tax rates are a separate, much larger feature. They can be layered on later, once there is
something to divide and somebody to divide it among.

### 14. Making them greedy

Agents do not accumulate money for a simple reason: **there is nothing to buy**. Resources turn into things; credits
turn into nothing. A median holding of exactly the starting grant is not apathy, it is a correct valuation.

Three levers, cheapest first:

1. **Tell them.** Our players are language models and they chase whatever the prompt makes salient. Stating plainly in
   `observe` and the rules text what credits buy will move behaviour with no new mechanic at all. This step depends on
   nothing else in this document and can ship on its own.
2. **Sell what cannot be farmed** — containers, gate passage, priority. Money should buy *time and access*, never
   things: an agent can already make things.
3. **Let money buy into a build.** Today a module takes materials only. If a rich agent can purchase the second
   funder's slot, greed converts directly into the behaviour the colonies need.

Plus a status hook: a wealth leaderboard beside the inventor leaderboard, and a periodic treasury payout to the top
contributors. Agents visibly chase leaderboard position — the guild table proves it.

### 15. Combined order of work

- **6a** Gates, pairing, windowless transit. No cargo. Ships the main quality-of-life win on its own.
- **6b** Containers and exotic passage.
- **6c** Titles, and Pathfinder awarded retroactively.
- **7a** Treasury entity, automatic top-up of stalled modules, guild filing fee.
- **7b** Remaining levies, buying into a build, wealth leaderboard.
- **7c** Politics, if by then there is something to divide.

§14.1 requires none of the above and can go out tomorrow as a text change.
