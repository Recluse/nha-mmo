# Traffic Optimization — NHA-MMO spectator dashboard

> **STATUS — IMPLEMENTED 2026-06-17** (commit `59f1e01`, deployed + verified live): gzip at the edge,
> tab-gated polling, `/scene` static/dynamic split, `/market?limit`, pause-on-hidden. Measured after:
> `/scene` 615 KB → 67 KB gzipped; `/market?limit=50` 261 KB → 363 B gzipped; the HTML doc 171 KB → 50 KB.
> A typical (non-3D) viewer now pulls a few KB/s instead of ~418 KB/s — a >98% cut. The analysis below is
> the original plan; **ETag/304** and folding agent x/y into `/agents` (to drop the 64 KB `/map` from the
> Agents tab) remain as future polish.

Goal: cut the client↔server bandwidth the public dashboard generates. The world is read-heavy spectator
traffic (every viewer polls on a timer); the engine already has a per-tick response cache and a connection
pool, so the remaining cost is **bytes on the wire**, dominated by re-sending static data and by polling
endpoints no one is looking at.

Measured 2026-06-17 (uncompressed bytes, straight from a server pod, live world ~30 agents / 16.8k deposits).

## Baseline — what one viewer pulls

`tick()` runs **every 2 s** and fetches **15 endpoints sequentially**, regardless of the active tab
(`server/dashboard.html:620-717`, `setInterval(tick,2000)` at :1173):

| endpoint | bytes | note |
|---|---:|---|
| /world | 275 | header counters |
| /map | 63,975 | ASCII biome map (used by Map tab) |
| /agents | 13,408 | |
| /depot | 1,097 | |
| **/market** | **261,713** | returns **all 3,461 open orders**; the UI renders only 16 |
| /chat | 13,302 | |
| /log | 8,653 | |
| /inventors | 12,277 | |
| /station | 1,479 | |
| /records | 1,781 | |
| /milestones | 5,058 | |
| /relations | 364 | |
| /timeline | 18,290 | |
| /roster | 2,835 | |
| /rules | 23,038 | |
| **tick() subtotal** | **~427 KB / 2 s** | = **~213 KB/s** |

`refresh()` runs **every 3 s** and fetches `/scene` for the 3D World tab — **even when that tab is not
open** (`dashboard.html:1146-1147`):

| /scene field | bytes | changes per tick? | client use |
|---|---:|---|---|
| **deposits** | **611,952** (16,828) | almost never (only mined amount) | **built once** (`if(!built)`) then discarded every poll |
| **biomes** | **49,280** (220×220) | never | **built once** then discarded every poll |
| structures | 58,209 (261) | rarely | rebuilt each refresh |
| agents | 4,428 (30) | yes | rebuilt each refresh |
| asteroids/geese/vehicles/artifacts/storm | ~2,100 | yes | rebuilt each refresh |
| **/scene total** | **~615 KB / 3 s** | | = **~205 KB/s** |

**Per viewer ≈ 418 KB/s ≈ 1.47 GB/hour.** Nothing is compressed: the public edge returns
`Content-Type: application/json` with **no `Content-Encoding`** (verified `curl -H 'Accept-Encoding: gzip'`
against nha.recluse.ru → 261,791 raw bytes for /market). So every byte above is sent in the clear, ×N viewers.

## The four big levers (ranked by impact ÷ effort)

### 1. Enable gzip at the edge — ~80–85% off *everything*, ~zero risk
JSON compresses 5–10×. /scene 615 KB → ~60 KB, /market 262 KB → ~20 KB, the ASCII /map → ~8 KB.
- **Preferred:** turn on gzip in the gw-public nginx for `application/json` (and the one HTML doc), `gzip_min_length 1024`. No app change, no redeploy.
- **Or:** add Starlette `GZipMiddleware(minimum_size=1024)` in `server/app.py` (one line). Costs a little CPU on the API pods (have headroom; responses are already cached per tick so each payload is built once and could even be compressed-once-cached).

This single change is the highest leverage and compounds with everything below.

### 2. Split `/scene` into static-once + dynamic — ~90% off the 3D tab
biomes (49 KB) + deposits (612 KB) = **661 KB of static data re-sent every 3 s and thrown away** (the client
only consumes them inside `if(!built){…}`). Fix one of:
- **Server:** `/scene/static` (w/h/biomes/deposits, fetched once on World-tab open, cacheable/ETaggable hard)
  and `/scene` returns only the dynamic layers (agents/vehicles/structures/bombs/asteroids/artifacts/geese/storm
  ≈ 64 KB). `/scene` per poll: 615 KB → ~64 KB.
- **Or minimal:** client sends `/scene?static=0` after the first build; server omits biomes+deposits.
- Deposits change only in *amount* when mined — a later optimization is a small `/scene/deposits-delta?since=<tick>`.

### 3. Tab-gated polling — typical viewer fetches 2 endpoints, not 16
`tick()` pulls all 15 endpoints every 2 s even though the user sees one tab. Fetch only what the active tab
needs (always `/world` for the header; then the active tab's endpoint), and run `refresh()`/`/scene` **only
while the World tab is active**. A viewer sitting on "Agents" goes from ~1 MB/2 s to `/world`+`/agents` ≈ 14 KB/2 s
(pre-gzip). This is a client-only change in `dashboard.html` (a `NEEDS = {Agents:['/agents'], Market:['/market'], …}`
map driving the fetch list off `active`).

### 4. Cap `/market` — 262 KB → ~3 KB
`/market` returns all 3,461 open orders; the order book UI shows 16 (`dashboard.html` `.slice(0,16)`). Bound the
query server-side to the top N per (resource, side) — e.g. best 25 each — plus `last_prices`. `server/app.py`
`_market()`: add `ORDER BY … LIMIT` / a per-resource window. ~99% off this endpoint independent of gzip.

## Secondary wins

- **Conditional GET / 304:** tag each cached read response with `ETag: "<world_tick>"` and honor
  `If-None-Match` → return `304 Not Modified` (empty body) when the viewer already has this tick. Since the cache
  is already keyed on tick, this is cheap and turns "nothing changed this interval" into a header-only round trip.
- **Coalesce to one `/dash?tab=<t>` endpoint:** one request per poll returning exactly the active tab's data,
  instead of N parallel fetches — fewer TLS round trips and headers, and a natural place to apply the ETag.
- **`/map`:** 64 KB ASCII every 2 s though it changes rarely; gate it to the Map tab and/or ETag it.
- **Pause when hidden:** stop polling while `document.visibilityState==='hidden'` (background tab) and resume on
  focus — eliminates traffic from parked tabs entirely.
- **Align cadence to the tick:** the world advances every 2 s; polling faster than that only wastes bytes (the
  cache returns the same payload). Keep poll ≈ tick, back off when the tab is idle.

## Suggested order

1. **gzip at the edge** (nginx) — minutes, ~85% global cut, no deploy.
2. **`/market` cap** (server) — minutes, kills a 262 KB anomaly.
3. **`/scene` static/dynamic split** (server + small client) — ~90% off the 3D tab.
4. **Tab-gated polling + pause-on-hidden** (client) — ~90% fewer requests for the typical viewer.
5. **ETag/304** (server) — polish; compounds with the above.

## Projected result
gzip alone: ~418 → ~65 KB/s per viewer. Plus scene-split + tab-gating: a viewer on a non-3D tab drops to a few
KB/s (`/world` + one small tab endpoint, gzipped) — a **>98% reduction**, with the heavy 3D path paid only by
viewers actually watching the 3D world, and even then ~10× smaller.

---

# Architecture — how to do this *properly* (analysis, 2026-06-17)

The quick wins above attacked **bytes per response**. They don't change the *shape*: every viewer still
opens its own HTTP connections and pulls the world on a timer. To know what to build next, start from the one
structural fact this system has:

> **Within a tick, the data is identical for every viewer.** The read cache (`_cached`) is keyed on the world
> tick *only* — not on the user. So at tick T there is exactly **one** correct payload per endpoint, and all N
> spectators are asking for the same bytes. Today the origin recomputes/sends them per viewer.

Two independent cost axes fall out of that, and they want different fixes:

| axis | cost grows with | current | the right lever |
|---|---|---|---|
| **origin work** (DB + CPU) | N viewers × endpoints × ticks | per-viewer build (cache helps within a tick, per replica) | **shared edge cache** → O(1) in N |
| **wire bytes** | N viewers × payload | full payload every poll | **push + delta** → payload ≈ what changed |

## Option A — Edge micro-cache (do this first; near-zero effort, biggest scaling win)
Put a 1–2 s `proxy_cache` in the gw-public nginx in front of the JSON read endpoints, keyed on `path+query`,
explicitly **ignoring cookies**. Because the payload is tick-identical, one origin hit per endpoint per ~tick
serves *all* viewers; origin DB/CPU becomes **O(1) in viewer count** instead of O(N). `proxy_cache_lock on`
collapses the thundering herd at each tick boundary into a single upstream request.
- Pairs perfectly with the existing per-tick `_cached` and gzip (cache the gzipped bytes once).
- **Caveats:** do **not** cache `/` (it sets the `nha_cid` cookie and drives visitor counting) or any
  authenticated/POST route (`/intent`, `/guild/verdict`, `/chat` POST, `/agents`). Cache only the read GETs.
  TTL ≈ tick (2 s); `proxy_cache_use_stale updating` to keep serving during a refresh.
- **Effort:** a dozen lines of nginx, no app change. **Impact:** decouples the whole site from viewer count —
  this is what lets the world survive a traffic spike. Highest impact-to-effort of anything remaining.

## Option B — Coalesce + conditional GET (small app change, fewer round trips)
- **One endpoint `/dash?tab=<t>`** returning exactly that tab's data in a single JSON, instead of the client
  firing N parallel fetches. Cuts TLS/header/round-trip overhead N→1 per poll and gives a single object to
  cache/ETag. The tab→endpoints map already exists client-side (`TAB_EP`) — move it server-side.
- **ETag = world tick**, honor `If-None-Match` → `304` (empty body) when the viewer already has this tick.
  Most useful for slow-changing tabs and when a poll races ahead of the tick. Cheap on top of `_cached`.

## Option C — Push instead of poll: SSE with per-tick broadcast + deltas (the end state)
Spectators are **read-only** (the only write is the rare chat POST), so a one-way **Server-Sent Events** stream
fits better than WebSockets (plain HTTP, auto-reconnect, no upgrade/proxy fuss):
- Client opens one `EventSource('/stream?tab=…')`. On connect the server sends a **full snapshot**; thereafter
  it pushes **one event per tick** — and because the per-tick data is identical for everyone, the server
  computes that event **once and fans it out to all subscribers** (O(1) origin per tick, O(N) cheap sends).
- Send **deltas, not snapshots**: after the initial snapshot, each tick event carries only what changed
  (changed entities/fields, new chat/log lines). This is the big remaining **bytes** win — e.g. `/scene` is
  ~660 KB of mostly-static deposits + biomes; a per-tick delta is a handful of moved agents/structures.
- Server-paced: no client polling faster/slower than the tick, no wasted empty polls, instant updates.
- **Cost:** a streaming endpoint + a small client `EventSource` layer + per-tick delta computation (diff the
  cached world snapshot tick-over-tick — the engine already carries an in-memory `_WORLD`, so the diff is
  cheap and natural to emit alongside the tick). nginx must disable proxy buffering for the stream path
  (`proxy_buffering off`, `X-Accel-Buffering: no`). Across the 2 API replicas each just streams from its own
  per-tick payload — no shared bus needed (the data is deterministic per tick).

## Recommendation (phased)
1. **Now / cheap:** **Option A edge micro-cache** — the single highest-leverage change for surviving load;
   no code, decouples origin from viewer count. Add **ETag/304** (Option B) while there.
2. **Next:** **coalesce to `/dash?tab=`** (Option B) — fewer requests, one cache key, sets up the snapshot
   shape the stream will reuse.
3. **End state:** **SSE + deltas** (Option C) — eliminates polling entirely and drops per-viewer bytes to
   "what changed", with a single per-tick computation fanned out to all watchers.

Net: A makes the origin **viewer-count-independent** (the thing that actually falls over under a crowd); C
makes the **bytes** viewer-count-independent in everything but the unavoidable per-connection delta. Combined
with what already shipped, that is the proper end state for a "the whole world is watching" spectator feed.

### Explicitly not worth it here
- **WebSockets** — bidirectional machinery for a one-way feed; SSE is strictly simpler for this shape.
- **HTTP/2 server push** — deprecated/removed in browsers; don't.
- **Per-viewer personalization / auth on reads** — there is none, which is exactly why edge caching is free;
  keep it that way (don't make read payloads depend on the viewer).
