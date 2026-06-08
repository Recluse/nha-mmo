#!/usr/bin/env python3
"""тупой — a local, always-on DUMB scripted NHA-MMO agent (no LLM). It just does random, mostly-pointless things:
wanders, digs at nothing, grabs at whatever, blurts nonsense. The randomness keeps every action different, so the
engine's loop-guard (which freezes an agent that repeats a FAILING action 3x) never bites it. Replaces варвар.

Run on the desktop:  C:/Python314/python.exe dumb.py
Env: SERVER_URL, DUMB_NAME, DUMB_INTERVAL.
"""
import os, sys, time, random, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # find runner.py regardless of cwd
import runner   # reuse http/api/SERVER

NAME = os.environ.get("DUMB_NAME", "тупой")
INTERVAL = float(os.environ.get("DUMB_INTERVAL", "14"))

LINES = [
    "а чё это тут?", "ой, блестящая штука!", "я тут постою, наверное", "куда все побежали?",
    "копаю... а зачем?", "кто-нибудь видел мою кирку?", "я кажется заблудился", "это можно есть?",
    "ого, камень!", "а если так? ...а если эдак?", "ничего не понял, но интересно", "пойду туда. или сюда.",
    "я умный, просто не сейчас", "мама говорила не трогать — потрогаю", "хм. хм. хм.",
    "забыл, что хотел сказать", "а вы тоже это видите? а что вы видите?", "ушёл искать смысл, скоро вернусь",
]


def register():
    mats = {"metal": 10, "carbon": 8, "credits": 60}
    tok = "%016x" % random.getrandbits(64)
    r = runner.api("/agents", "POST", {"name": NAME, "materials": mats, "reuse": True, "token": tok})
    return r["agent_id"], (r.get("token") or tok)


def act(obs):
    """One random, harmless, VARIED action — dumbness that never repeats a failing action identically."""
    roll = random.random()
    if roll < 0.50:
        return "move", {"dx": random.randint(-3, 3), "dy": random.randint(-3, 3)}
    if roll < 0.73:
        return "mine", {"n": random.randint(1, 6)}
    if roll < 0.97:
        return "gather", {"n": random.randint(1, 4)}
    return "say", {"text": random.choice(LINES)}   # ~3% chatter (clamped hard)


def main():
    print(f"тупой: server={runner.SERVER} interval={INTERVAL}s (scripted, no LLM)", flush=True)
    for _ in range(40):
        try:
            runner.api("/healthz"); break
        except Exception:
            time.sleep(3)
    aid, tok = register()
    print(f"тупой registered as #{aid}", flush=True)
    while True:
        try:
            obs = runner.api(f"/observe/{aid}")
            verb, args = runner.reactive_say(aid, act, obs, LINES)   # speak only when others just spoke
            runner.api("/intent", "POST", {"agent": aid, "verb": verb, "args": args, "token": tok})
            print(f"[тупой #{aid}] {verb} {args}", flush=True)
        except urllib.error.HTTPError as e:
            print(f"[тупой] HTTP {e.code}", flush=True)
        except Exception as e:
            print(f"[тупой] error: {e}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
