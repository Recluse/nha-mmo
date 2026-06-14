#!/usr/bin/env python3
"""NHA-MMO — the Inventors' Guild referee (non-deterministic invention).

Polls the world for invention proposals (mixes that matched no BUILT-IN physics pattern), asks a strong
LLM to rule on each — "does a plausible NEW item form from these materials' physics? what is it, and what
properties does it carry?" — and posts the verdict back. The tick loop (the sole authoritative writer)
applies it: approved → a new DYNAMIC recipe is minted (cached by ingredient-signature → from then on the
same mix crafts it deterministically, replay-safe) + the item + inventor points; rejected → the escrowed
ingredients are refunded. So invention is open-ended, but the engine stays reproducible.

Pure stdlib. Runs on the Google host next to runner.py. Env: SERVER_URL, GUILD_URL, GUILD_KEY,
GUILD_MODEL, GUILD_INTERVAL.
"""
import os, json, time, urllib.request, urllib.error

SERVER   = os.environ.get("SERVER_URL", "https://nha.recluse.ru")
GUILD_TOKEN = os.environ.get("GUILD_TOKEN", "")    # must match the server's GUILD_TOKEN; sent as X-Guild-Token on verdicts
URL      = os.environ.get("GUILD_URL", "https://models.github.ai/inference/chat/completions")
KEY      = os.environ["GUILD_KEY"]
MODEL    = os.environ.get("GUILD_MODEL", "openai/gpt-4.1-mini")
INTERVAL = float(os.environ.get("GUILD_INTERVAL", "12"))

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

SYSTEM = ("You classify combinations in a crafting game. Each input material has integer property tags. "
          "Decide whether the inputs form a coherent new crafted item. If yes, give it a short snake_case "
          "id, a display name, and 2-5 integer tags. Reply with one JSON object: "
          '{"approved": true|false, "item_key": "...", "name": "...", "props": {"tag": int}, "reason": "..."}. '
          "Approve when the tags plausibly make a useful item and the name fits them; reject when they do not.")


def http(method, url, data=None, headers=None, timeout=45):
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


def judge(p):
    user = (f"Proposal #{p['id']} by agent '{p.get('agent_name') or p['agent']}'.\n"
            f"Proposed name: {p.get('proposed_name') or '(none given)'}\n"
            f"Ingredients (with quantities): {json.dumps(p['ings'])}\n"
            f"Each ingredient's physical properties: {json.dumps(p['ingredient_props'])}\n"
            f"Rule on this invention. Reply with ONLY the JSON verdict.")
    body = {"model": MODEL, "temperature": 0.5, "max_tokens": 300,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}
    hdr = {"authorization": "Bearer " + KEY}
    try:
        out = http("POST", URL, {**body, "response_format": {"type": "json_object"}}, headers=hdr)
    except urllib.error.HTTPError as e:
        if e.code not in (400, 422):
            raise
        try:
            out = http("POST", URL, body, headers=hdr)
        except urllib.error.HTTPError as e2:
            if e2.code == 400:                              # content filter / unprocessable → unjudgeable
                return None
            raise
    raw = (out["choices"][0]["message"]["content"] or "").strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        raw = raw[4:] if raw.lower().startswith("json") else raw
    i, j = raw.find("{"), raw.rfind("}")
    return json.loads(raw[i:j + 1])


def main():
    print(f"guild referee: model={MODEL} server={SERVER}", flush=True)
    while True:
        try:
            pend = http("GET", SERVER + "/guild/pending")["pending"]
        except Exception as e:
            print(f"poll error: {e}", flush=True); time.sleep(INTERVAL); continue
        for p in pend:
            try:
                v = judge(p)
                if v is None:                               # filtered/unprocessable → reject so the escrow refunds
                    verdict = {"proposal_id": p["id"], "approved": False, "item_key": "", "name": "",
                               "props": {}, "reason": "the Guild could not evaluate this mixture"}
                else:
                    verdict = {"proposal_id": p["id"], "approved": bool(v.get("approved")),
                               "item_key": str(v.get("item_key", "")), "name": str(v.get("name", "")),
                               "props": v.get("props") or {}, "reason": str(v.get("reason", ""))[:200]}
                http("POST", SERVER + "/guild/verdict", verdict,
                     headers={"x-guild-token": GUILD_TOKEN} if GUILD_TOKEN else None)
                tag = ("APPROVED " + verdict["item_key"]) if verdict["approved"] else "rejected"
                print(f"#{p['id']} {p.get('proposed_name') or p['sig']} -> {tag} :: {verdict['reason'][:70]}", flush=True)
            except Exception as e:
                print(f"judge #{p.get('id')} failed: {e}", flush=True)
                if getattr(e, "code", None) == 429:          # rate-limited: stop hammering the LLM, let the window reset before retrying the rest
                    print("rate-limited (429) — backing off 90s", flush=True)
                    time.sleep(90)
                    break
            time.sleep(1.5)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
