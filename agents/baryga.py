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
# EXPANSION ERA: the Trader is a rich baron too — in the space/expansion era it stops just dealing and JOINS the
# colonisation effort, reusing the miner's PROVEN build-ship→fly→colonise pipeline. Point it at a body via BARYGA_DEST
# (default 'phobos' → a 3rd funder on the moon the other two barons target, so the Forward Base completes robustly).
# MINER_DEST must be set BEFORE importing miner (miner reads it at import to pick EXP_DEST/EXP_HS).
os.environ.setdefault("MINER_DEST", os.environ.get("BARYGA_DEST", "phobos"))
import miner   # reuse the colonisation pipeline (miner.act chains expand→build→ascend→iridium→ship→depart→colonise)

NAME = os.environ.get("BARYGA_NAME", "Trader")
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

# answers when an OUTSIDER names «Trader» in chat — short, in-character, merchant patter
REPLIES = [
    "звал? у меня для тебя есть предложение, от которого ты не откажешься",
    "да-да, слушаю. с чем пожаловал, с деньгами надеюсь?",
    "по имени зовёшь — значит, торговать хочешь. показывай товар",
    "я весь внимание. но учти: время — деньги, а твоё — моё",
    "кому Trader, а кому и кошелёк с ножками. говори по делу",
    "обращайся, родной. скидку не дам, но советом — задёшево",
]

SELLABLE = ("metal", "iron", "copper", "aluminum", "carbon", "silicon", "crystal", "titanium",
            "nickel", "coal", "oil", "ore", "salt", "water", "sulfur")

# --- PATRON BOOK: standing bids for goods the DEPOT REFUSES TO BUY -------------------------------------------
# The depot only ever buys RAW materials. Nothing crafted — steel, chips, motors, composite — has any
# counterparty in the world at all, so manufacturing paid nothing beyond guild points and the goods just piled
# up (2026-09-09: 50,221 units of steel held across 12 agents, unsellable by construction). A rich Trader
# standing in the book as a BUYER is the only price signal those goods will ever get.
# Deliberately only goods ABSENT from the depot's price list: bidding on something the depot also sells would
# open a free-money loop (buy from the depot, flip it straight back to us).
BID = {"steel": (400, 12), "electrolyte": (200, 12), "engine": (40, 45), "plastic": (100, 10),
       "wire": (60, 8), "alloy": (50, 14), "composite": (40, 16), "motor": (30, 40),
       "magnet": (40, 18), "chip": (40, 26), "glass": (40, 8), "battery": (20, 30),
       "insulated_wire": (30, 20), "frame": (10, 20)}
PATRON_FLOOR = 200_000     # keep this much working capital before playing patron
OPEN_ORDER_CAP = 40        # ...and never sit on more open orders than this — see the housekeeping branch below


def register():
    mats = {"credits": 300, "metal": 20, "carbon": 10}   # seed capital for the wheeler-dealer
    return runner.register(NAME, mats)   # persist+reclaim our token (server no longer hands it out by name)


def deal(obs, depot):
    """барыга — a market-maker now: mines free goods to sell, dumps stockpiles to the depot for cash, and lists stock
    on the AGENT market ABOVE the depot buy-price (so OTHER agents pay the spread, not барыга). It does NOT buy from
    the depot to resell — that always loses the spread, which is exactly how the miner out-traded the 'trader'.
    Varied each turn so the loop-guard never bites. (Chatter is handled by runner.reactive_say, not here.)"""
    inv = obs.get("inventory", {}) or {}
    cr = int(inv.get("credits", 0))
    prices = (depot or {}).get("prices", {}) or {}
    held = [(r, int(inv.get(r, 0))) for r in SELLABLE if int(inv.get(r, 0)) >= 3]
    rr = random.random()
    board = obs.get("contracts") or []
    # A) opportunistic: fulfill SOMEONE ELSE'S open contract if I already hold the goods and the credit reward beats
    #    liquidating them at the depot — a wheeler-dealer never turns down a paying job.
    for c in board:
        if c.get("mine"):
            continue
        want = c.get("want") or {}
        rc = int((c.get("reward") or {}).get("credits", 0))
        if want and rc > 0 and all(int(inv.get(r, 0)) >= int(q) for r, q in want.items()):
            liq = sum(int(q) * int((prices.get(r, {}) or {}).get("sell", 1) or 1) for r, q in want.items())
            if rc > liq:                                   # paying more than I'd get dumping the goods → take the spread
                return "fulfill", {"contract_id": c["id"]}
    # A2) HOUSEKEEPING — cancel down an over-long book before adding to it. This bot posted a sell order almost
    #     every turn for a month and essentially none of them ever filled: 29,544 open orders had accumulated by
    #     2026-09-09, the oldest ~29 days old, each holding real goods in escrow and together forcing a hard cap
    #     on the /market payload to stop it melting the endpoint (audit 2026-09-03, F18 — the cap treated the
    #     symptom, this treats the cause). `cancel` refunds the escrow, so nothing is lost. Only half the turns,
    #     so a long backlog does not stop the bot trading entirely while it drains.
    own = obs.get("orders") or []
    over = int(obs.get("orders_total", 0)) > OPEN_ORDER_CAP
    if over and own and rr < 0.5:
        return "cancel", {"order_id": own[0]["id"]}
    # A3) PATRON — stand in the book as a BUYER of manufactured goods (see BID above for why this matters).
    if cr >= PATRON_FLOOR and not over and rr < 0.30:
        mine_buy = {o.get("resource") for o in own if o.get("side") == "buy"}
        cands = [r for r in BID if r not in mine_buy and r not in prices]
        if cands:
            r = random.choice(cands)
            qty, px = BID[r]
            return "order", {"side": "buy", "resource": r, "qty": qty, "price": px}
    # B) occasionally SEED the board with a supply job — pay credits for raws so the contract market has real demand
    #    (keeps at most 2 of my own open; escrows a reward but keeps a 20-credit reserve; auto-refunds if unclaimed).
    if cr >= 60 and sum(1 for c in board if c.get("mine")) < 2 and rr < 0.15:
        want_res = random.choice(("iron", "copper", "metal", "carbon", "crystal", "coal", "wood", "silicon"))
        n = random.randint(5, 15)
        reward = min(cr - 20, n * random.randint(2, 4))
        if reward >= n:                                    # only worth posting if the pay is decent per unit
            return "contract", {"reward": {"credits": reward}, "want": {want_res: n}, "deadline_ticks": 600}
    # 1) cash out a stockpile to the depot — guaranteed income (the goods were mined for free)
    if held and (cr < 60 or max(q for _, q in held) >= 12 or rr < 0.42):
        r, q = max(held, key=lambda x: x[1] * (prices.get(x[0], {}).get("sell", 1) or 1))
        return "sell", {"resource": r, "n": min(random.randint(6, 20), q)}
    # 2) work the agent market: list stock just ABOVE the depot's buy-price — capture the spread from other agents.
    #    Gated on `over`: posting yet another sell into an already-unsold book is what produced the 29k backlog.
    if held and not over and rr < 0.60:
        r, q = max(held, key=lambda x: x[1])
        bp = int((prices.get(r, {}) or {}).get("buy", 2) or 2)
        return "order", {"side": "sell", "resource": r, "qty": min(q, random.randint(3, 6)), "price": bp + 1 + random.randint(0, 2)}
    # 3) restock the shelves himself — mine free goods to sell (the real profit engine, same as the miner)
    if rr < 0.90:
        return "mine", {"n": random.randint(2, 6)}
    return "move", {"dx": random.randint(-3, 3), "dy": random.randint(-3, 3)}


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
            miner._LAST_DEPOT = depot     # let the reused colonisation pipeline buy chain raws from the depot (the baron is rich)
            # a rich barыga BANKROLLS the space race: skim surplus credits into the neediest station line (throttled).
            binv = runner.baron_invest(obs, aid)
            if binv:
                verb, args = binv
            else:
                # EXPANSION ERA: colonise via the miner pipeline; otherwise wheel-and-deal. smart_turn still layers in
                # mention-replies first (Trader answers when named); `eager` trade-accepts only when NOT colonising, so a
                # trade never derails a launch. The turn fn falls back to dealing whenever a colonise step isn't actionable.
                def _turn(o):
                    if o.get("expansion"):
                        try:
                            c = miner.act(o)
                            if c:
                                return c
                        except Exception:
                            pass
                    return deal(o, depot)
                verb, args = runner.smart_turn(aid, NAME, obs, depot, _turn,
                                               LINES, replies=REPLIES, eager=not obs.get("expansion"))
            runner.api("/intent", "POST", {"agent": aid, "verb": verb, "args": args, "token": tok})
            print(f"[барыга #{aid}] {verb} {args}", flush=True)
        except urllib.error.HTTPError as e:
            print(f"[барыга] HTTP {e.code}", flush=True)
        except Exception as e:
            print(f"[барыга] error: {e}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
