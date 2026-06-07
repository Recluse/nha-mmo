#!/usr/bin/env python3
"""NHA-MMO — процедурная генерация мира (лёгкая, детерминированная, без numpy).

Слоёная (как в RESEARCH-NOTES): шум высот + влажности → биом (матрица) → конечные кластерные
залежи, привязанные к биому, с минимальным зазором (дешёвый Poisson-disk). Карта детерминирована
из `seed` → хранить можно seed + дельты, а не сетку. Залежи пишутся как сущности `deposit`.

Run:  PG_DSN=... python worldgen.py [W H seed]
"""
import os, sys, math, hashlib
from collections import Counter
import psycopg2
from psycopg2.extras import Json

DSN = os.environ.get("PG_DSN", "host=127.0.0.1 dbname=nhamoo user=postgres")
SCALE = 9.0   # зум шума

# ---- детерминированный value-noise fBm (без зависимостей) ----
def _h(seed, x, y):
    b = hashlib.blake2b(f"{seed}:{x}:{y}".encode(), digest_size=4).digest()
    return int.from_bytes(b, "big") / 2**32

def _smooth(t):
    return t * t * (3 - 2 * t)

def _vnoise(seed, x, y):
    x0, y0 = math.floor(x), math.floor(y)
    fx, fy = _smooth(x - x0), _smooth(y - y0)
    a = _h(seed, x0, y0) + (_h(seed, x0 + 1, y0) - _h(seed, x0, y0)) * fx
    b = _h(seed, x0, y0 + 1) + (_h(seed, x0 + 1, y0 + 1) - _h(seed, x0, y0 + 1)) * fx
    return a + (b - a) * fy

def fbm(seed, x, y, octaves=4):
    val, amp, freq, norm = 0.0, 1.0, 1.0, 0.0
    for o in range(octaves):
        val += amp * _vnoise(seed + o * 1009, x * freq, y * freq)
        norm += amp; amp *= 0.5; freq *= 2.0
    return val / norm

# ---- биом из (высота × влажность), матрица Уиттакера-лайт ----
def biome(elev, moist):
    if elev < 0.35: return "water"
    if elev > 0.72: return "mountain"
    if moist > 0.60: return "forest"
    if moist < 0.32: return "desert"
    return "plains"

# season 3: «tundra» — холодный фронтир-биом. Классифицируется ТОЛЬКО в новой (frontier) области,
# чтобы детерминизм уже сгенерированного региона остался байт-в-байт прежним (старые кэш-сетки = БД).
# Порог (высокая «высота» + высокая «влажность») вырезает самые холодные мокрые пики; frontier-гейт
# (x>=min_x or y>=min_y) — главная гарантия: ни одна старая клетка не может стать tundra.
# thresholds tuned (seed=42) so tundra is a real-but-minority cold biome inside the frontier (~3% of new
# cells → enough ice/titanium to be reachable) without ever touching the old region — the frontier gate,
# not the threshold, is the determinism guarantee, so this stays free to tune (SEASON3-PLAN OPEN RISKS).
TUNDRA_ELEV = 0.70
TUNDRA_MOIST = 0.50

def biome_at(elev, moist, frontier=False):
    b = biome(elev, moist)
    if frontier and elev > TUNDRA_ELEV and moist > TUNDRA_MOIST:
        return "tundra"
    return b

GLYPH = {"water": "~", "plains": ".", "forest": "#", "desert": ":", "mountain": "^", "tundra": "%"}
# биом → одиночный код-символ для /scene и карт (зеркало GLYPH; tundra='%')
_BIOME_CODE = {"water": "~", "plains": ".", "forest": "#", "desert": ":", "mountain": "^", "tundra": "%"}
# что спавнится, где, с какой вероятностью на клетку и конечным запасом
# metals listed FIRST in each biome so they aren't crowded out by ore/wood (one deposit per cell, first match)
DEPOSITS = {
    "mountain": [("copper", 0.07, 20), ("iron", 0.08, 22), ("aluminum", 0.06, 18), ("ore", 0.07, 25),
                 ("crystal", 0.05, 8), ("coal", 0.07, 22), ("sulfur", 0.04, 12)],
    "plains":   [("iron", 0.03, 14), ("copper", 0.03, 12), ("wood", 0.05, 16), ("carbon", 0.05, 18), ("salt", 0.04, 14)],
    "forest":   [("copper", 0.03, 12), ("wood", 0.11, 22), ("coal", 0.04, 16), ("carbon", 0.05, 16), ("oil", 0.03, 14)],
    "desert":   [("aluminum", 0.03, 14), ("silicon", 0.10, 22), ("salt", 0.06, 16), ("oil", 0.05, 18), ("sulfur", 0.04, 12)],
    "water":    [("water", 0.04, 999), ("salt", 0.05, 18), ("brine", 0.06, 40)],   # sea: water + coastal salt + brine
    # season 3 frontier biome: titanium feeds superalloy; ice (amount<=respawn cap 18) feeds cryo_fuel
    "tundra":   [("titanium", 0.06, 16), ("ice", 0.08, 18), ("iron", 0.03, 12)],
}

def generate(W, H, seed, min_gap=3, min_x=None, min_y=None):
    # min_x/min_y mark the frontier origin (the season-2 square's edge): the «tundra» biome is assigned
    # ONLY to cells with x>=min_x or y>=min_y, so every already-generated cell keeps its exact season-2
    # biome (noise values are identical; the base biome() result is untouched for old cells). When neither
    # bound is given (the default for fresh-world / cache-grid callers), NO cell becomes tundra → output is
    # byte-identical to season 2.  An out-of-range bound (>= W / >= H) ⇒ no frontier ⇒ no tundra.
    fx = W if min_x is None else min_x
    fy = H if min_y is None else min_y
    grid = [[biome_at(fbm(seed, x / SCALE, y / SCALE, 5), fbm(seed + 7919, x / SCALE, y / SCALE, 4),
                      frontier=(x >= fx or y >= fy))
             for x in range(W)] for y in range(H)]
    placed = []
    g2 = min_gap * min_gap
    def far(x, y):
        return all((px - x) ** 2 + (py - y) ** 2 >= g2 for px, py, *_ in placed)
    def try_place(x, y):
        for res, prob, amt in DEPOSITS.get(grid[y][x], []):
            if _h(seed + 31337, x * 1000 + ord(res[0]), y) < prob and far(x, y):
                placed.append((x, y, res, amt, grid[y][x])); return
    # Two ordered passes so the frontier never perturbs the already-generated region's Poisson placement:
    # the OLD region (x<fx AND y<fy) is scanned first in the exact original row-major order, building `placed`
    # identically to the season-2 (no-frontier) run — so every old-region deposit decision is byte-identical.
    # Frontier cells are scanned only afterwards (their gap checks may see old deposits, never the reverse).
    # When ungated (fx==W, fy==H) the first pass covers the whole grid and the second is empty ⇒ original output.
    for y in range(min(fy, H)):
        for x in range(min(fx, W)):
            try_place(x, y)
    for y in range(H):
        for x in range(W):
            if x >= fx or y >= fy:
                try_place(x, y)
    return grid, placed

def ascii_map(grid, deposits, agents=()):
    dmap = {(x, y): ("♣" if res == "wood" else "*") for x, y, res, *_ in deposits}   # ♣ = tree (wood), * = ore/mineral
    amap = {(int(x), int(y)): ch for x, y, ch in agents}        # agents drawn on top of deposits/biome
    return "\n".join("".join(amap.get((x, y), dmap.get((x, y), GLYPH[c])) for x, c in enumerate(row))
                     for y, row in enumerate(grid))

def write_deposits(conn, deposits, seed):
    cur = conn.cursor()
    cur.execute("DELETE FROM entities WHERE type='deposit' AND attrs->>'gen_seed'=%s", (str(seed),))
    for x, y, res, amt, bi in deposits:
        cur.execute("INSERT INTO entities(type,x,y,attrs) VALUES('deposit',%s,%s,%s)",
                    (x, y, Json({"resource": res, "amount": amt, "biome": bi, "gen_seed": str(seed)})))
    conn.commit()

def main():
    W = int(sys.argv[1]) if len(sys.argv) > 1 else 48
    H = int(sys.argv[2]) if len(sys.argv) > 2 else 18
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else 42
    grid, deposits = generate(W, H, seed)
    print(f"== world {W}x{H} seed={seed}  (~=water .=plains #=forest :=desert ^=mountain %=tundra; ♣=tree *=залежь) ==")
    print(ascii_map(grid, deposits))
    print("biomes:", dict(Counter(c for row in grid for c in row)))
    print(f"deposits: {len(deposits)} →", dict(Counter(d[2] for d in deposits)))
    conn = psycopg2.connect(DSN)
    write_deposits(conn, deposits, seed)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM entities WHERE type='deposit' AND attrs->>'gen_seed'=%s", (str(seed),))
    print("→ записано в PG:", cur.fetchone()[0], "залежей (детерминированно из seed)")
    conn.close()

if __name__ == "__main__":
    main()
