#!/usr/bin/env python3
"""шахтёр — a focused SCRIPTED NHA-MMO miner (no LLM): digs deposits for ore/metal, sells the haul for credits,
roams to new veins. Varied actions dodge the engine's loop-guard. Designed to run in k8s alongside the world.
Env: SERVER_URL, MINER_NAME, MINER_INTERVAL.
"""
import os, sys, time, random, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner

NAME = os.environ.get("MINER_NAME", "шахтёр")
INTERVAL = float(os.environ.get("MINER_INTERVAL", "14"))

LINES = [
    "копаю до центра земли", "руда не сама себя добудет", "ещё кирка стерпит", "глубже, глубже!",
    "камень крепкий, но я крепче", "кто не копает, тот не ест", "золото где-то рядом, чую нутром",
]

ORES = ("iron", "copper", "nickel", "titanium", "aluminum", "silicon", "ore", "coal", "metal", "crystal")


def register():
    mats = {"credits": 40, "metal": 5}
    tok = "%016x" % random.getrandbits(64)
    r = runner.api("/agents", "POST", {"name": NAME, "materials": mats, "reuse": True, "token": tok})
    return r["agent_id"], (r.get("token") or tok)


def act(obs):
    inv = obs.get("inventory", {}) or {}
    roll = random.random()
    haul = [(r, int(inv.get(r, 0))) for r in ORES if int(inv.get(r, 0)) >= 15]
    # SMELT: forge ore into metal when it has the makings (iron + carbon -> steel)
    if int(inv.get("iron", 0)) >= 2 and int(inv.get("carbon", 0)) >= 1 and roll < 0.18:
        return "combine", {"ingredients": {"iron": 2, "carbon": 1}, "name": "сталь"}
    if haul and roll < 0.36:                              # sell off a fat pile of ore
        r, q = max(haul, key=lambda x: x[1])
        return "sell", {"resource": r, "n": min(random.randint(8, 18), q)}
    if roll < 0.64:
        return "mine", {"n": random.randint(1, 6)}        # dig the deposit underfoot / nearest in range
    if roll < 0.97:
        return "move", {"dx": random.randint(-3, 3), "dy": random.randint(-3, 3)}   # roam to a new vein
    return "say", {"text": random.choice(LINES)}   # ~3% chatter (clamped hard)


def main():
    print(f"шахтёр: server={runner.SERVER} interval={INTERVAL}s (scripted, no LLM)", flush=True)
    for _ in range(40):
        try:
            runner.api("/healthz"); break
        except Exception:
            time.sleep(3)
    aid, tok = register()
    print(f"шахтёр registered as #{aid}", flush=True)
    while True:
        try:
            obs = runner.api(f"/observe/{aid}")
            verb, args = runner.reactive_say(aid, act, obs, LINES)   # speak only when others just spoke
            runner.api("/intent", "POST", {"agent": aid, "verb": verb, "args": args, "token": tok})
            print(f"[шахтёр #{aid}] {verb} {args}", flush=True)
        except urllib.error.HTTPError as e:
            print(f"[шахтёр] HTTP {e.code}", flush=True)
        except Exception as e:
            print(f"[шахтёр] error: {e}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
