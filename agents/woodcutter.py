#!/usr/bin/env python3
"""дровосек — a focused SCRIPTED NHA-MMO lumberjack (no LLM): chops trees for wood, sells the haul for credits,
roams to fresh forest. Varied actions dodge the engine's loop-guard. Designed to run in k8s alongside the world.
Env: SERVER_URL, WOOD_NAME, WOOD_INTERVAL.
"""
import os, sys, time, random, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner

NAME = os.environ.get("WOOD_NAME", "дровосек")
INTERVAL = float(os.environ.get("WOOD_INTERVAL", "14"))

LINES = [
    "ещё одно дерево — ещё рубль", "лес рубят — щепки летят", "топор не подведёт",
    "кто рано встаёт, тому и бревно", "дерево большое, а я упорный", "руки чешутся — пойду порублю",
    "тук-тук, кто в тереме? уже никто",
]


def register():
    mats = {"credits": 40, "metal": 5}
    tok = "%016x" % random.getrandbits(64)
    r = runner.api("/agents", "POST", {"name": NAME, "materials": mats, "reuse": True, "token": tok})
    return r["agent_id"], (r.get("token") or tok)


def act(obs):
    inv = obs.get("inventory", {}) or {}
    wood = int(inv.get("wood", 0))
    roll = random.random()
    if wood >= 15 and roll < 0.16:                        # cash out a fat woodpile
        return "sell", {"resource": "wood", "n": min(random.randint(8, 18), wood)}
    if wood >= 2 and roll < 0.34:                         # PLANT a sapling -> a renewable wood deposit (sustainable forestry)
        return "plant", {}
    if roll < 0.62:
        return "chop", {"n": random.randint(1, 5)}        # chop wood (auto-walks to the nearest tree in range)
    if roll < 0.97:
        return "move", {"dx": random.randint(-3, 3), "dy": random.randint(-3, 3)}   # roam to fresh forest
    return "say", {"text": random.choice(LINES)}   # ~3% chatter (clamped hard)


def main():
    print(f"дровосек: server={runner.SERVER} interval={INTERVAL}s (scripted, no LLM)", flush=True)
    for _ in range(40):
        try:
            runner.api("/healthz"); break
        except Exception:
            time.sleep(3)
    aid, tok = register()
    print(f"дровосек registered as #{aid}", flush=True)
    while True:
        try:
            obs = runner.api(f"/observe/{aid}")
            verb, args = act(obs)
            runner.api("/intent", "POST", {"agent": aid, "verb": verb, "args": args, "token": tok})
            print(f"[дровосек #{aid}] {verb} {args}", flush=True)
        except urllib.error.HTTPError as e:
            print(f"[дровосек] HTTP {e.code}", flush=True)
        except Exception as e:
            print(f"[дровосек] error: {e}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
