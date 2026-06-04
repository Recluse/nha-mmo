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

GLYPH = {"water": "~", "plains": ".", "forest": "#", "desert": ":", "mountain": "^"}
# что спавнится, где, с какой вероятностью на клетку и конечным запасом
DEPOSITS = {
    "mountain": [("ore", 0.18, 25), ("crystal", 0.05, 8)],
    "plains":   [("fuel", 0.08, 20)],
    "forest":   [("fuel", 0.10, 15)],
    "water":    [("water", 0.04, 999)],
}

def generate(W, H, seed, min_gap=3):
    grid = [[biome(fbm(seed, x / SCALE, y / SCALE, 5), fbm(seed + 7919, x / SCALE, y / SCALE, 4))
             for x in range(W)] for y in range(H)]
    placed = []
    g2 = min_gap * min_gap
    def far(x, y):
        return all((px - x) ** 2 + (py - y) ** 2 >= g2 for px, py, *_ in placed)
    for y in range(H):
        for x in range(W):
            for res, prob, amt in DEPOSITS.get(grid[y][x], []):
                if _h(seed + 31337, x * 1000 + ord(res[0]), y) < prob and far(x, y):
                    placed.append((x, y, res, amt, grid[y][x])); break
    return grid, placed

def ascii_map(grid, deposits, agents=()):
    dmap = {(x, y): res[0].upper() for x, y, res, *_ in deposits}
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
    print(f"== world {W}x{H} seed={seed}  (~=water .=plains #=forest :=desert ^=mountain; O/C/F/W=залежь) ==")
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
