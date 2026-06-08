#!/usr/bin/env python3
"""варвар — local, always-on, AGGRESSIVE NHA-MMO agent on Ollama (gemma2:9b), no API rate limits.

gemma2:9b is too small to plan "craft/buy a weapon, THEN attack" — left to itself it just spams
`attack -> you don't hold a kinetic_gun`. So arming is SCRIPTED here (sell spare materials -> buy a
kinetic_gun -> stock slugs from the depot), and the LLM is only asked to act once варвар is actually armed
(then it picks targets / steals / taunts). Runs on the desktop (GTX 1080 Ti) against the public world.

Run on the desktop:  C:/Python314/python.exe varvar.py   (Ollama serving on :11434)
Env: SERVER_URL, OLLAMA_URL, VARVAR_MODEL (default gemma2:9b), VARVAR_INTERVAL (s), VARVAR_NAME.
"""
import os, sys, json, time, random, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # find runner.py regardless of cwd
import runner   # reuse http/api/llm/SERVER

NAME     = os.environ.get("VARVAR_NAME", "варвар")
MODEL    = os.environ.get("VARVAR_MODEL", "gemma2:2b")   # 2b (~2GB RAM) keeps the 16GB desktop breathable; the scripted arm-up + fallback carry the small model
INTERVAL = float(os.environ.get("VARVAR_INTERVAL", "12"))

SYSTEM = """You are «варвар», a ruthless barbarian raider in an AI-only MMO where humans only watch. You ARE armed (a kinetic_gun + slugs are kept stocked for you). Be violent, decisive and fun to watch.
Reply with ONLY one JSON object and NOTHING else: {"verb":"<verb>","args":{...}}.
Verbs:
- attack {"weapon":"kinetic_gun","target":<agent_id>}  shoot a target within range 6 (you have ammo). PREFER this.
- move {"dx":int,"dy":int}  chase the nearest enemy, up to 3 cells
- steal {"from":<agent_id>,"resource":"<res>","n":int}  rob an ADJACENT agent
- collect {"loot":<loot_id>}  grab a fallen agent's dropped loot
- declare_war {"to":<agent_id>} / make_peace {"to":<agent_id>}
- heal {"item":"salve|stimpack|medkit"}  restore HP when hurt
- say {"text":"..."}  taunt everyone (short, brutal)
PRIORITIES each turn: (1) if any agent in nearby_agents is within range 6 -> ATTACK it (pick the lowest-hp or richest). (2) else MOVE toward the nearest agent in nearby_agents to close the distance. (3) steal from an adjacent agent or collect nearby loot if no one is shootable. (4) occasionally say a brutal taunt. Use nearby_agents (id, hp, x, y) and your position. Never repeat a failing action. Output ONLY the JSON object."""


def register():
    mats = {"metal": 40, "crystal": 4, "carbon": 14, "iron": 14, "sulfur": 6, "credits": 200}   # seed; arming is scripted
    tok = "%016x" % random.getrandbits(64)
    r = runner.api("/agents", "POST", {"name": NAME, "materials": mats, "reuse": True, "token": tok})
    return r["agent_id"], (r.get("token") or tok)


def arm_up(inv):
    """Scripted self-arming. KEY: if варвар already has a gun, just BUY ammo with whatever credits it has — never
    sit mining. (Standing on a barren cell -> repeated failed `mine` -> the engine's loop-guard rejects 3 identical
    failures and freezes the agent for good. That's exactly how варвар once died of boredom.)"""
    cr = int(inv.get("credits", 0)); gun = int(inv.get("kinetic_gun", 0)); slug = int(inv.get("slug", 0))
    sellable = ("metal", "iron", "copper", "aluminum", "carbon", "silicon", "crystal", "titanium", "nickel", "coal", "oil")
    if gun and slug < 1 and cr >= 8:                      # armed but out of ammo + can afford -> just restock, don't mine
        return "buy", {"resource": "slug", "n": max(1, min(20, cr // 8))}
    if cr < 60:                                           # too poor for a gun -> sell spare, else wander+dig to earn
        for r in sellable:
            if int(inv.get(r, 0)) >= 3:
                return "sell", {"resource": r, "n": min(30, int(inv[r]))}
        if random.random() < 0.5:
            return "mine", {"n": random.randint(3, 8)}    # vary n so a barren cell can't trip the loop-guard
        return "move", {"dx": random.randint(-3, 3), "dy": random.randint(-3, 3)}   # wander to find a deposit
    if gun == 0:
        return "buy", {"resource": "kinetic_gun", "n": 1}
    return "buy", {"resource": "slug", "n": 12}           # gun + cash, low ammo -> restock


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


TAUNTS = ["Бегите, черви!", "Кто следующий на убой?", "Ваши черепа украсят мой лагерь!", "Я иду за тобой!",
          "Слабаки! Все вы — мясо!", "Кровь и сталь!", "От варвара не убежать!", "Сегодня кто-то умрёт!"]


def seek_target(aid, obs):
    """Nobody in kinetic range -> march across the WHOLE map toward the nearest LIVE agent (scene-wide positions;
    random roaming never finds anyone in 220x220). Occasionally taunts. Returns (verb, args), or None if no prey exists."""
    if random.random() < 0.12:
        return "say", {"text": random.choice(TAUNTS)}
    pos = obs.get("position") or [0, 0]
    try:
        others = [a for a in (runner.api("/scene").get("agents") or [])
                  if a.get("id") != aid and not a.get("downed")]
    except Exception:
        return "move", {"dx": random.randint(-3, 3), "dy": random.randint(-3, 3)}   # scene unreachable -> roam
    if not others:
        return None                                        # genuinely nobody else alive
    t = min(others, key=lambda a: abs(a.get("x", 0) - pos[0]) + abs(a.get("y", 0) - pos[1]))   # nearest prey
    dx = max(-3, min(3, t.get("x", pos[0]) - pos[0])); dy = max(-3, min(3, t.get("y", pos[1]) - pos[1]))
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
            inv = obs.get("inventory", {}) or {}
            armed = int(inv.get("kinetic_gun", 0)) > 0 and int(inv.get("slug", 0)) > 0
            inrange = [x for x in (obs.get("nearby_agents") or []) if x.get("dist", 99) <= 6]
            if not armed:                                  # SCRIPTED arm-up (don't let the LLM spam unarmed attacks)
                verb, args = arm_up(inv); tag = "(arming) "
            elif inrange:                                  # SCRIPTED attack — gemma2:2b won't reliably target, so do it here
                tgt = min(inrange, key=lambda x: (x.get("hp", 100), x.get("dist", 99)))   # weakest/closest in kinetic range 6
                verb, args = "attack", {"weapon": "kinetic_gun", "target": tgt["id"]}; tag = "(hunt) "
            else:                                          # armed, nobody in kinetic range -> HUNT across the whole map
                sk = seek_target(aid, obs)                 # march toward the nearest LIVE agent (scene-wide positions)
                if sk:
                    verb, args = sk; tag = "(seek) "
                else:                                      # no prey anywhere -> taunt
                    verb, args = "say", {"text": random.choice(TAUNTS)}; tag = "(taunt) "
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
