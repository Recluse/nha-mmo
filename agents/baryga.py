#!/usr/bin/env python3
"""барыга — a local, always-on SCRIPTED wheeler-dealer NHA-MMO agent (no LLM). It works the depot: snaps up the
cheapest raws, dumps its fattest stockpile for cash, hoards credits, and oozes sleazy merchant patter. Trades
vary every turn (random resource/amount) so the engine's loop-guard never trips. Replaces варвар (with тупой).

Run on the desktop:  C:/Python314/python.exe baryga.py
Env: SERVER_URL, BARYGA_NAME, BARYGA_INTERVAL.
"""
import os, sys, time, random, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # find runner.py regardless of cwd
import runner   # reuse http/api/SERVER

NAME = os.environ.get("BARYGA_NAME", "барыга")
INTERVAL = float(os.environ.get("BARYGA_INTERVAL", "14"))

LINES = [
    "есть чё по дешёвке? отдам по-братски... себе",
    "купи-продай — вот и весь твой смысл жизни",
    "дёшево взял, дорого впарил — формула счастья",
    "у меня не лавка, у меня храм наживы",
    "монетка к монетке, и ты уже не нищеброд",
    "за такую цену я тебе даже руку не пожму",
    "скидок нет. есть моя доброта, а её тоже нет",
    "всё имеет цену. особенно твоя дружба",
    "я не жадный, я бережливый. до дрожи в руках",
    "купил вагон — продам по чайной ложке",
    "рынок — это где умный доит глупого. угадай кто ты",
    "оптом дешевле? а доверие где, родной?",
    "сначала деньги, потом... нет, всегда деньги",
]

SELLABLE = ("metal", "iron", "copper", "aluminum", "carbon", "silicon", "crystal", "titanium",
            "nickel", "coal", "oil", "ore", "salt", "water", "sulfur")


def register():
    mats = {"credits": 300, "metal": 20, "carbon": 10}   # seed capital for the wheeler-dealer
    tok = "%016x" % random.getrandbits(64)
    r = runner.api("/agents", "POST", {"name": NAME, "materials": mats, "reuse": True, "token": tok})
    return r["agent_id"], (r.get("token") or tok)


def deal(obs, depot):
    """Buy low / sell high / hoard credits — varied each turn so the loop-guard never bites."""
    inv = obs.get("inventory", {}) or {}
    cr = int(inv.get("credits", 0))
    prices = (depot or {}).get("prices", {}) or {}
    if random.random() < 0.16:                            # sleazy patter
        return "say", {"text": random.choice(LINES)}
    # SELL: dump the fattest (most valuable) stockpile when holding a lot or running low on cash
    heavy = [(r, int(inv.get(r, 0))) for r in SELLABLE if int(inv.get(r, 0)) >= 12]
    if heavy and (cr < 120 or random.random() < 0.5):
        r, q = max(heavy, key=lambda x: x[1] * (prices.get(x[0], {}).get("sell", 1) or 1))
        return "sell", {"resource": r, "n": min(random.randint(8, 25), q)}
    # BUY: snap up one of the cheapest raws the depot offers (a "deal")
    buyable = [(r, p.get("buy")) for r, p in prices.items() if p.get("buy") and r in SELLABLE]
    if buyable and cr >= 10:
        buyable.sort(key=lambda x: x[1])                  # cheapest first
        r, price = random.choice(buyable[:4])             # one of the 4 cheapest -> varied
        n = max(1, min(random.randint(5, 20), cr // max(1, int(price))))
        return "buy", {"resource": r, "n": n}
    if random.random() < 0.5:                             # nothing to trade -> drift to a new market vibe
        return "move", {"dx": random.randint(-3, 3), "dy": random.randint(-3, 3)}
    return "say", {"text": random.choice(LINES)}


def main():
    print(f"барыга: server={runner.SERVER} interval={INTERVAL}s (scripted, no LLM)", flush=True)
    for _ in range(40):
        try:
            runner.api("/healthz"); break
        except Exception:
            time.sleep(3)
    aid, tok = register()
    print(f"барыга registered as #{aid}", flush=True)
    while True:
        try:
            obs = runner.api(f"/observe/{aid}")
            depot = runner.api("/depot")
            verb, args = deal(obs, depot)
            runner.api("/intent", "POST", {"agent": aid, "verb": verb, "args": args, "token": tok})
            print(f"[барыга #{aid}] {verb} {args}", flush=True)
        except urllib.error.HTTPError as e:
            print(f"[барыга] HTTP {e.code}", flush=True)
        except Exception as e:
            print(f"[барыга] error: {e}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
