#!/usr/bin/env python3
"""Drive two existing NHA agents — codex (#6322) and alchemist (#6345) — until ~20:00 MSK today.

Reuses runner.py's plumbing (api / llm / compact_obs / parse_action / SYSTEM).

Capacity lesson learned the hard way: Groq's free tier limit that bites here is TOKENS-PER-MINUTE,
and the full game prompt (obs + world + market + depot + every agent) is huge, so even a couple of
agents blow the per-model TPM. So this driver (a) uses a SMALL prompt — compact obs + persona + a
one-line world tick, dropping the market/depot/agents dumps — and (b) gives each agent its OWN
exclusive Groq model (gpt-oss-20b / gpt-oss-120b, which the trimmed runner no longer uses) so they
never share a TPM bucket with the runner or each other. A 429 just backs that agent off a turn.

Run on the monitoring-VM with agents.env + tokens CODEX_TOKEN, ALCH_TOKEN.
"""
import os, sys, json, time, datetime, urllib.error
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner  # noqa: E402

PROV = "groq"
MSK = ZoneInfo("Europe/Moscow")
_now = datetime.datetime.now(MSK)
_deadline = _now.replace(hour=20, minute=0, second=0, microsecond=0)
if _deadline <= _now:
    _deadline = _now + datetime.timedelta(minutes=30)
DEADLINE = _deadline.timestamp()

STEP = float(os.environ.get("PLAY_STEP", "22"))           # seconds between turns; each agent acts ~every 44s

# (aid, token, name, EXCLUSIVE groq model, persona) — models the trimmed runner does NOT use
AGENTS = [
    (6322, os.environ["CODEX_TOKEN"], "codex", "openai/gpt-oss-20b",
     "You are a relentless monument-builder; you hold the Grand Sculptor + Aqueduct Architect Wonder titles. "
     "Chase MORE first-of-kind Wonders (castle/dam/theater/statue/colossus): gather metal+composite, then "
     "construct on CLEAR ground away from the map edge and away from water (geese block building there). Also "
     "craft a kinetic_gun (barrel+slug+gunpowder) vs the killer 'Barbarian' (#3108)."),
    (6345, os.environ["ALCH_TOKEN"], "alchemist", "openai/gpt-oss-120b",
     "You are a mad chemist. Mix to discover NEW recipes for first-discovery points. Buy a diverse cheap "
     "inventory + mine where you stand. Avoid letting water/salt/crystal dominate a mix (they collapse to "
     "salve/antidote/lens); the path to the Guild is DRY multi-organic / cross-domain mixes and chaining your "
     "own inventions. Try big 10+ ingredient mixes."),
]


def turn_persona(aid, tok, name, model, persona, last):
    """One think->act step with a SMALL prompt (compact obs + persona + a one-line tick)."""
    obs = runner.api(f"/observe/{aid}")
    tick = runner.api("/world").get("tick")
    user = (f"You are '{name}' (agent #{aid}). {persona}\n"
            f"World tick {tick}. Your state: {json.dumps(runner.compact_obs(obs), ensure_ascii=False)}\n"
            f"Last action: {last or 'none yet'}\nChoose ONE action as JSON.")
    raw = runner.llm(PROV, model, runner.SYSTEM.format(name=name), user)
    verb, args = runner.parse_action(raw)
    runner.api("/intent", "POST", {"agent": aid, "verb": verb, "args": args, "token": tok})
    res = f"{verb} {json.dumps(args, ensure_ascii=False)} -> queued"
    print(f"[{name} #{aid}] {res}", flush=True)
    return res


def main():
    print(f"play-until-evening: codex#6322 (gpt-oss-20b) + alchemist#6345 (gpt-oss-120b); small prompt; "
          f"stop {datetime.datetime.fromtimestamp(DEADLINE, MSK):%Y-%m-%d %H:%M %Z}; step={STEP}s", flush=True)
    last = {aid: None for aid, *_ in AGENTS}
    i = 0
    while time.time() < DEADLINE:
        aid, tok, name, model, persona = AGENTS[i % len(AGENTS)]
        try:
            last[aid] = turn_persona(aid, tok, name, model, persona, last[aid])
        except urllib.error.HTTPError as e:
            print(f"[#{aid}] HTTP {e.code}", flush=True)
        except Exception as e:
            print(f"[#{aid}] {type(e).__name__}: {str(e)[:80]}", flush=True)
        i += 1
        time.sleep(STEP)
    print("20:00 MSK — codex and alchemist clock out for the evening", flush=True)


if __name__ == "__main__":
    main()
