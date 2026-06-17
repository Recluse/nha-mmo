# Traffic Optimization — NHA-MMO spectator dashboard

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
