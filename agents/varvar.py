#!/usr/bin/env python3
"""варвар — local, always-on, AGGRESSIVE NHA-MMO agent on Ollama (gemma2:9b), no API rate limits.

A small local model can't digest runner.py's full ~2k-token game prompt, so this uses a COMPACT,
gemma-friendly aggressive prompt + a robust parser + a "charge the nearest enemy" fallback, so варвар
always does something violent even when the model fumbles the format. Runs on the desktop (GTX 1080 Ti)
against the public world — local = no limits = always online, so there's live action to watch.

Run on the desktop:  C:/Python314/python.exe varvar.py   (Ollama serving on :11434)
Env: SERVER_URL, OLLAMA_URL, VARVAR_MODEL (default gemma2:9b), VARVAR_INTERVAL (s), VARVAR_NAME.
"""
import os, sys, json, time, random, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # find runner.py regardless of cwd
import runner   # reuse http/api/llm/SERVER

NAME     = os.environ.get("VARVAR_NAME", "варвар")
MODEL    = os.environ.get("VARVAR_MODEL", "gemma2:9b")
INTERVAL = float(os.environ.get("VARVAR_INTERVAL", "12"))

SYSTEM = """You are «варвар», a ruthless barbarian raider in an AI-only MMO where humans only watch. Be violent, decisive and fun to watch.
Reply with ONLY one JSON object and NOTHING else: {"verb":"<verb>","args":{...}}.
Verbs:
- move {"dx":int,"dy":int}  roam/chase, up to 3 cells
- attack {"weapon":"kinetic_gun|energy_weapon","target":<agent_id>}  need that weapon + ammo; kinetic range 6, energy 9
- steal {"from":<agent_id>,"resource":"<res>","n":int}  rob an ADJACENT agent
- arm {}   drop an armed bomb (must hold a bomb); detonate {"bomb":<bomb_id>}  blow it
- mine {"n":int} / chop {"n":int} / gather {"n":int}  collect materials
- combine {"ingredients":{"<res>":qty,...},"name":"..."}  craft. weapons: acid_former+carbon+heat->gunpowder; hard metal body->barrel; steel or iridium->slug; barrel+slug+gunpowder->kinetic_gun; explosive+container+reactive->bomb
- collect {"loot":<loot_id>}  grab a fallen agent's loot
- declare_war {"to":<agent_id>} / make_peace {"to":<agent_id>}
- heal {"item":"salve|stimpack|medkit"}  restore HP
- say {"text":"..."}  taunt everyone (short, brutal)
PRIORITIES each turn: (1) if you hold a weapon+ammo and an enemy is within range -> ATTACK it (pick lowest-hp or richest). (2) no weapon? craft one NOW: mine iron/carbon, then gunpowder, barrel, slug, kinetic_gun. (3) steal from an adjacent agent or collect nearby loot. (4) else move toward the nearest agent in nearby_agents to close in. (5) sometimes say a brutal taunt.
Use the data: nearby_agents (id, hp, x, y), your weapons + ammo, your inventory, your hp. Never repeat a failing action. Output ONLY the JSON object."""


def register():
    mats = {"metal": 40, "crystal": 4, "carbon": 14, "iron": 14, "sulfur": 6, "credits": 200}   # war stock
    tok = "%016x" % random.getrandbits(64)
    r = runner.api("/agents", "POST", {"name": NAME, "materials": mats, "reuse": True, "token": tok})
    return r["agent_id"], (r.get("token") or tok)


def parse(raw):
    raw = (raw or "").strip()
    if "```" in raw:
        seg = max(raw.split("```"), key=len)
        raw = seg[4:] if seg.lower().startswith("json") else seg
    i, j = raw.find("{"), raw.rfind("}")
    obj = json.loads(raw[i:j + 1]) if (i >= 0 and j > i) else {}
    verb = obj.get("verb") or obj.get("action") or obj.get("command")
    args = obj.get("args")
    if not isinstance(args, dict):
        args = {k: v for k, v in obj.items() if k not in ("verb", "action", "command")}
    return verb, args


def chase_fallback(obs):
    """No parseable verb -> still behave like a barbarian: charge the nearest agent, else roam."""
    pos = obs.get("position") or [0, 0]
    na = obs.get("nearby_agents") or []
    if na:
        t = min(na, key=lambda a: a.get("dist", 999))
        dx = max(-3, min(3, (t.get("x", pos[0])) - pos[0]))
        dy = max(-3, min(3, (t.get("y", pos[1])) - pos[1]))
        if dx or dy:
            return "move", {"dx": dx, "dy": dy}
    return "move", {"dx": random.randint(-3, 3), "dy": random.randint(-3, 3)}


def main():
    print(f"варвар: server={runner.SERVER} model=ollama:{MODEL} interval={INTERVAL}s", flush=True)
    for _ in range(40):
        try:
            runner.api("/healthz"); break
        except Exception:
            time.sleep(3)
    aid, tok = register()
    print(f"варвар registered as #{aid}", flush=True)
    last = None
    while True:
        try:
            obs = runner.api(f"/observe/{aid}")
            world = runner.api("/world"); depot = runner.api("/depot")
            others = [{"id": o["id"], "name": o["name"], "hp": o.get("hp")}
                      for o in runner.api("/agents")["agents"] if o["id"] != aid][:14]
            user = (f"You are agent #{aid} («{NAME}»). World tick {world['tick']}.\n"
                    f"Your state: {json.dumps(obs, ensure_ascii=False)}\n"
                    f"Last action result: {last or 'none yet'}\n"
                    f"Depot prices: {json.dumps(depot['prices'])}\n"
                    f"Other agents (prey): {json.dumps(others, ensure_ascii=False)}\n"
                    f"Choose ONE aggressive action as JSON.")
            raw = runner.llm("ollama", MODEL, SYSTEM, user)
            try:
                verb, args = parse(raw)
            except Exception:
                verb, args = None, {}
            tag = ""
            if not verb:
                verb, args = chase_fallback(obs); tag = "(fallback) "
            runner.api("/intent", "POST", {"agent": aid, "verb": verb, "args": args, "token": tok})
            last = f"{verb} {json.dumps(args, ensure_ascii=False)} -> queued"
            print(f"[варвар #{aid}] {tag}{last}", flush=True)
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode()[:160]
            except Exception:
                body = ""
            print(f"[варвар] HTTP {e.code}: {body}", flush=True)
        except Exception as e:
            print(f"[варвар] error: {e}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
