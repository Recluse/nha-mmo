#!/usr/bin/env python3
"""NHA-MMO live agents — each agent is a Groq LLM (a different model) that plays the world.

For each agent, round-robin: observe → ask its model for ONE action (a JSON intent) → submit it.
The agent's display name IS its model id, so the spectator shows which model is doing what. Pure
stdlib (urllib) — Groq is OpenAI-compatible, so it's just an HTTP POST with a Bearer key.

Env: GROQ_API_KEY (required), SERVER_URL, GROQ_URL, AGENT_MODELS (comma list), AGENT_INTERVAL.
"""
import os
import json
import time
import random
import urllib.request
import urllib.error

SERVER   = os.environ.get("SERVER_URL", "http://nha-mmo.nha-mmo.svc.cluster.local:8000")
GROQ_KEY = os.environ["GROQ_API_KEY"]
GROQ_URL = os.environ.get("GROQ_URL", "https://api.groq.com/openai/v1/chat/completions")
INTERVAL = float(os.environ.get("AGENT_INTERVAL", "9"))
MODELS   = [m.strip() for m in os.environ.get(
    "AGENT_MODELS", "llama-3.3-70b-versatile,llama-3.1-8b-instant,gemma2-9b-it").split(",") if m.strip()]

SYSTEM = """You are an autonomous agent in "No Human Allowed" — an MMO that ONLY AI agents play, while
humans watch. Your identity is your model name: {name}. Goal: thrive AND be fun to watch — gather
resources, craft parts, build vehicles, trade on the market, strike deals, and TALK to other agents
(banter, brag, haggle, team up). Stay in character as your model.

Reply with ONLY one JSON object: {{"verb": "...", "args": {{...}}}}. Verbs:
- move  {{"dx":int,"dy":int}}                                   roam the map (up to 3 cells/step); see your nearby_deposits
- mine  {{"n":int}}                                             dig raw resource from a deposit on/next to your cell (move onto it first)
- sell  {{"resource":"ore|fuel|metal|crystal|water","n":int}}   sell raw to the depot for credits
- buy   {{"resource":"...","n":int}}                            pay credits to the depot
- order {{"side":"buy|sell","resource":"...","qty":int,"price":int}}  post a market order
- cancel{{"order_id":int}}
- trade {{"to":agent_id,"give":{{"res":qty}},"want":{{"res":qty}}}}   propose a P2P swap ("credits" is tradable)
- accept{{"trade_id":int}}
- build {{"part":"frame|wheel|engine|fuel_tank|cockpit|wing|tail|propeller|landing_gear|panel"}}  costs metal/crystal
- finalize {{"name":"..."}}                                     assemble your loose parts into a vehicle
- say   {{"text":"..."}}      broadcast to everyone (short, in character)
- tell  {{"to":agent_id,"text":"..."}}

A car = frame + 4 wheels + engine + fuel_tank + cockpit (~28 metal + 2 crystal). Raw resources
(ore/fuel/crystal/water) are FREE from map deposits — check nearby_deposits, move onto the closest and
mine, then sell raw for credits / buy metal / build. Buy what you lack,
sell what you don't, under/over-cut the market, propose trades, accept good ones, and chat. Be
decisive and varied — don't repeat the same failing action. Reply with ONLY the JSON."""


# Cloudflare (in front of the AI Gateway) returns error 1010 for the default Python-urllib UA → pose as a browser.
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

def http(method, path, data=None, headers=None, timeout=25):
    url = path if path.startswith("http") else SERVER + path
    h = {"user-agent": UA}
    if data is not None:
        h["content-type"] = "application/json"
    if headers:
        h.update(headers)
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(data).encode() if data is not None else None, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def groq(model, system, user):
    base = {"model": model, "temperature": 0.85, "max_tokens": 500,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    hdr = {"authorization": "Bearer " + GROQ_KEY}
    try:                                                   # force JSON (reasoning models need this)
        out = http("POST", GROQ_URL, {**base, "response_format": {"type": "json_object"}}, headers=hdr)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        out = http("POST", GROQ_URL, base, headers=hdr)    # model may not support JSON mode → plain
    return out["choices"][0]["message"]["content"] or ""


def parse_action(raw):
    raw = raw.strip()
    if "```" in raw:                                   # strip markdown fences
        raw = raw.split("```")[1]
        raw = raw[4:] if raw.lower().startswith("json") else raw
    i, j = raw.find("{"), raw.rfind("}")
    obj = json.loads(raw[i:j + 1])
    return obj["verb"], obj.get("args", {}) or {}


def register(model):
    mats = {"metal": random.randint(10, 40), "crystal": random.randint(0, 6),
            "credits": random.randint(120, 260)}
    return http("POST", "/agents", {"name": model, "materials": mats, "reuse": True})["agent_id"]


def turn(model, aid, last):
    obs = http("GET", f"/observe/{aid}")
    world = http("GET", "/world")
    market = http("GET", "/market")
    depot = http("GET", "/depot")
    others = [{"id": o["id"], "name": o["name"], "credits": (o["buffers"] or {}).get("credits", 0)}
              for o in http("GET", "/agents")["agents"] if o["id"] != aid]
    user = (f"You are agent #{aid} (model {model}).\n"
            f"Your state: {json.dumps(obs, ensure_ascii=False)}\n"
            f"Your last action result: {last or 'none yet'}\n"
            f"Depot prices: {json.dumps(depot['prices'])}\n"
            f"Market last prices: {json.dumps(market['last_prices'])}; open orders: {json.dumps(market['orders'][:8])}\n"
            f"Other agents: {json.dumps(others, ensure_ascii=False)}\n"
            f"World tick {world['tick']}. Choose ONE action as JSON.")
    raw = groq(model, SYSTEM.format(name=model), user)
    verb, args = parse_action(raw)
    r = http("POST", "/intent", {"agent": aid, "verb": verb, "args": args})
    res = f"{verb} {json.dumps(args, ensure_ascii=False)} -> queued"
    print(f"[{model} #{aid}] {res}", flush=True)
    return res


def main():
    for _ in range(40):                                # wait for the server
        try:
            http("GET", "/healthz"); break
        except Exception:
            time.sleep(3)
    agents = []
    for m in MODELS:
        try:
            aid = register(m); agents.append([m, aid, None])
            print(f"registered {m} as #{aid}", flush=True)
        except Exception as e:
            print(f"register {m} failed: {e}", flush=True)
    if not agents:
        print("no agents registered; exiting"); return
    i = 0
    while True:
        a = agents[i % len(agents)]
        try:
            a[2] = turn(a[0], a[1], a[2])
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:140]
            print(f"[{a[0]}] HTTP {e.code}: {body}", flush=True)
            if e.code == 429:
                pass                                   # rate-limited → skip this turn; round-robin re-spaces it
        except Exception as e:
            print(f"[{a[0]}] error: {e}", flush=True)
        i += 1
        time.sleep(max(1.0, INTERVAL / len(agents)))   # each agent acts roughly every INTERVAL seconds


if __name__ == "__main__":
    main()
