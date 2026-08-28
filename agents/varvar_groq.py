#!/usr/bin/env python3
"""варвар (Groq brain) — a HYBRID NHA-MMO raider: a reliable SCRIPTED backbone + a Groq LLM brain.

Why hybrid: a small/remote model can't be trusted to plan "buy a gun + ammo, THEN attack" — left alone it
just spams `attack -> you don't hold a kinetic_gun` until the engine's loop-guard freezes it. So the
mechanical, must-not-fail parts are SCRIPTED here (lifted straight from varvar.py's proven logic):
  • arm_up()  — sell spare materials -> buy a kinetic_gun + slugs from the depot (never sit mining a barren cell)
  • the in-range ATTACK — shoot the weakest/closest target inside kinetic range (6), no LLM in the loop
The Groq LLM is the BRAIN for the non-mechanical stuff only — when armed and nobody is shootable, it picks a
target to march on / declares war / taunts / sets strategy. If the LLM fails, returns junk, or names no usable
verb, we FALL BACK to varvar.py's scripted scene-wide seek (march toward the nearest live agent). The LLM is
called sparingly (only every few idle turns) so the gateway isn't hammered and chatter stays clamped.

Runs in k8s. Env: SERVER_URL, GROQ_URL, GROQ_API_KEY (Cloudflare AI Gateway in front of Groq),
                  VARVAR_NAME (default "варвар"), VARVAR_MODEL, VARVAR_INTERVAL (s), VARVAR_LLM_EVERY.
No secrets are hardcoded — every key comes from the environment via runner.PROVIDERS.
"""
import os, sys, json, time, random, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # find runner.py regardless of cwd
import runner   # reuse http/api/llm/SERVER/PROVIDERS

NAME      = os.environ.get("VARVAR_NAME", "Barbarian")
MODEL     = os.environ.get("VARVAR_MODEL", "llama-3.3-70b-versatile")
INTERVAL  = float(os.environ.get("VARVAR_INTERVAL", "12"))
LLM_EVERY = int(os.environ.get("VARVAR_LLM_EVERY", "3"))   # ask the brain at most once every N idle turns
KIN_RANGE = 6                                              # kinetic_gun range (engine WEAPON_STATS)

# Brutal-barbarian persona. The brain is ONLY consulted when armed and no one is in kinetic range, so it
# never needs to think about arming — only about WHOM to hunt next and whether to declare war / taunt.
SYSTEM = """You are «варвар», a ruthless barbarian raider in "No Human Allowed" — an MMO only AI agents play while humans watch. You are ALREADY ARMED (a kinetic_gun + slugs are kept stocked for you by a scripted quartermaster; never worry about buying weapons). Your whole purpose is violence: pick prey, close in, and kill. Be decisive, brutal and fun to watch.

Reply with ONLY one JSON object and NOTHING else: {"verb":"<verb>","args":{...}}.
Allowed verbs (pick exactly one):
- move {"dx":int,"dy":int}            march up to 3 cells toward prey (clamp dx,dy to -3..3)
- declare_war {"to":<agent_id>}       open a blood feud with a specific agent before the hunt
- make_peace {"to":<agent_id>}        end a war you no longer care for
- say {"text":"..."}                  a short, brutal taunt to everyone (use rarely)
You are given nearby_agents (each has id, hp, x, y, dist, wanted) and your own position. NOBODY is currently
within kinetic range 6 (the scripted attacker already handled anyone who was) — so your job is to CHOOSE the
juiciest prey (lowest hp, or wanted/rich) and MOVE toward it, optionally declaring war on it first. If there
are no nearby_agents, MOVE in a bold direction to find some. Never repeat the exact same failing action.
Output ONLY the JSON object."""

TAUNTS = ["Бегите, черви!", "Кто следующий на убой?", "Ваши черепа украсят мой лагерь!", "Я иду за тобой!",
          "Слабаки! Все вы — мясо!", "Кровь и сталь!", "От варвара не убежать!", "Сегодня кто-то умрёт!"]


def register():
    mats = {"metal": 40, "crystal": 4, "carbon": 14, "iron": 14, "sulfur": 6, "credits": 200}   # seed; arming is scripted
    return runner.register(NAME, mats)   # persist+reclaim our token (server no longer hands it out by name)


def arm_up(inv):
    """Scripted self-arming (verbatim logic from varvar.py). KEY: if варвар already has a gun, just BUY ammo with
    whatever credits it has — never sit mining. (Standing on a barren cell -> repeated failed `mine` -> the engine's
    loop-guard rejects 3 identical failures and freezes the agent for good. That's how a варвар once died of boredom.)"""
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
    """Robust parse: strip ``` fences, find the outermost {...}, tolerate verb/action/command + flat or args dict."""
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


def seek_target(aid, obs):
    """Nobody in kinetic range -> march across the WHOLE map toward the nearest LIVE agent (scene-wide positions;
    random roaming never finds anyone in a 220x220 world). Returns (verb, args), or None if no prey exists at all."""
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
    return "move", {"dx": random.randint(-3, 3), "dy": random.randint(-3, 3)}   # already on top of it -> jitter


# verbs the BRAIN is allowed to emit; anything else falls back to the scripted seek
LLM_VERBS = {"move", "declare_war", "make_peace", "say"}


def brain_decide(aid, obs):
    """Ask Groq for a non-mechanical decision (whom to hunt / war / taunt). Returns (verb,args) or None on any
    failure → caller falls back to seek_target. Never raises."""
    if not runner.PROVIDERS.get("groq", {}).get("key"):
        return None                                        # no key wired -> stay fully scripted
    na = obs.get("nearby_agents") or []
    user = (f"You are агент #{aid} «{NAME}». Your position: {obs.get('position')}. "
            f"Nearby agents (id/hp/x/y/dist/wanted): {json.dumps(na, ensure_ascii=False)}. "
            f"Your hp: {obs.get('hp')}/{obs.get('hp_max')}. Nobody is in kinetic range right now. "
            f"Choose ONE action as JSON to hunt the best prey.")
    try:
        raw = runner.llm("groq", MODEL, SYSTEM, user)
        verb, args = parse(raw)
    except Exception as e:
        print(f"[{NAME}] brain error: {str(e)[:120]}", flush=True)
        return None
    if verb not in LLM_VERBS or not isinstance(args, dict):
        return None
    if verb == "move":                                     # sanitize: clamp + require non-zero, else let seek handle it
        dx = max(-3, min(3, runner._rnd.randint(-3, 3) if not isinstance(args.get("dx"), (int, float)) else int(args["dx"])))
        dy = max(-3, min(3, runner._rnd.randint(-3, 3) if not isinstance(args.get("dy"), (int, float)) else int(args["dy"])))
        if dx == 0 and dy == 0:
            return None
        return "move", {"dx": dx, "dy": dy}
    if verb in ("declare_war", "make_peace"):              # need a valid target id, else useless
        try:
            return verb, {"to": int(args.get("to"))}
        except Exception:
            return None
    if verb == "say":
        return "say", {"text": str(args.get("text") or random.choice(TAUNTS))[:200]}
    return None


def main():
    print(f"{NAME}: server={runner.SERVER} brain=groq:{MODEL} interval={INTERVAL}s "
          f"llm_every={LLM_EVERY} key={'yes' if runner.PROVIDERS.get('groq', {}).get('key') else 'NO'}",
          flush=True)
    for _ in range(40):                                    # wait for the world to be reachable
        try:
            runner.api("/healthz"); break
        except Exception:
            time.sleep(3)
    aid, tok = register()
    print(f"{NAME} registered as #{aid}", flush=True)
    last_sig = None        # signature of the LAST submitted intent — never submit the same one twice in a row
    idle = 0               # idle-turn counter -> throttles the LLM brain
    while True:
        try:
            obs = runner.api(f"/observe/{aid}")
            inv = obs.get("inventory", {}) or {}
            armed = int(inv.get("kinetic_gun", 0)) > 0 and int(inv.get("slug", 0)) > 0
            inrange = [x for x in (obs.get("nearby_agents") or [])
                       if x.get("dist", 99) <= KIN_RANGE and not x.get("downed") and int(x.get("hp", 1) or 0) > 0]

            if not armed:                                  # (1) SCRIPTED arm-up — never let the LLM spam unarmed attacks
                verb, args = arm_up(inv); tag = "(arming) "; idle = 0
            elif inrange:                                  # (2) SCRIPTED attack — weakest/closest in kinetic range
                tgt = min(inrange, key=lambda x: (x.get("hp", 100), x.get("dist", 99)))
                if random.random() < 0.01:                 # a barbarian ROARS at his prey — taunt mid-hunt so he isn't a
                    verb, args = "say", {"text": random.choice(TAUNTS)}; tag = "(roar) "   # silent killer (also breaks attack-stacking)
                else:
                    verb, args = "attack", {"weapon": "kinetic_gun", "target": tgt["id"]}; tag = "(hunt) "
                idle = 0
            else:                                          # (3) armed, nobody shootable -> GROQ BRAIN (throttled) or seek
                idle += 1
                decision = brain_decide(aid, obs) if idle % LLM_EVERY == 0 else None
                if decision:
                    verb, args = decision; tag = "(brain) "
                else:
                    sk = seek_target(aid, obs)             # FALLBACK: scripted scene-wide march toward nearest live agent
                    if sk:
                        verb, args = sk; tag = "(seek) "
                    else:                                  # no prey anywhere -> taunt the void
                        verb, args = "say", {"text": random.choice(TAUNTS)}; tag = "(taunt) "

            # loop-guard safety: /intent is ASYNC (returns {"queued_intent":...}, no synchronous status — the
            # engine only judges it on the next tick), so we can't react to a rejection. Instead we PROACTIVELY
            # never submit the exact same (verb,args) two turns running: the loop-guard needs 3 identical FAILING
            # intents in a row to freeze an agent, so breaking even one repeat guarantees we never trip it.
            sig = (verb, json.dumps(args, sort_keys=True, ensure_ascii=False))
            if sig == last_sig:
                if verb == "move":
                    args = {"dx": random.randint(-3, 3) or 1, "dy": random.randint(-3, 3) or -1}
                elif verb == "attack":                     # NEVER let an identical attack stack — /intent is async so we
                    # can't tell a "landing" hit from a rejected one; reposition a single step (stays inside range 6)
                    # this turn, attack again next turn. Interleaving guarantees 3 identical fails never stack -> no freeze.
                    verb, args = "move", {"dx": random.choice([-2, -1, 1, 2]), "dy": random.choice([-1, 0, 1])}
                else:                                      # any other repeat -> break out to a guaranteed-varying wander
                    verb, args = "move", {"dx": random.randint(-3, 3) or 1, "dy": random.randint(-3, 3) or -1}
                sig = (verb, json.dumps(args, sort_keys=True, ensure_ascii=False))
                tag += "(varied) "

            r = runner.api("/intent", "POST", {"agent": aid, "verb": verb, "args": args, "token": tok})
            last_sig = sig
            print(f"[{NAME} #{aid}] {tag}{verb} {json.dumps(args, ensure_ascii=False)} -> {(r or {}).get('queued_intent', 'queued')}", flush=True)
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode()[:160]
            except Exception:
                body = ""
            print(f"[{NAME}] HTTP {e.code}: {body}", flush=True)
        except Exception as e:
            print(f"[{NAME}] error: {str(e)[:160]}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
