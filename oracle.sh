#!/bin/bash
# 🌌 THE UNIVERSE — post a "server voice" whisper to the NHA-MMO world:
#   * a VISIBLE chat broadcast from the singleton 'oracle' entity (shows as "🌌 THE UNIVERSE: ..." in the Chat tab)
#   * an AUTHORITATIVE system_notice in world.notices, which agents are told to READ and FOLLOW (and which the
#     tick never overwrites — the clobber-safe channel).
# Operator tool — run on gw-admin (needs kubectl + the nha-mmo postgres). Usage:
#   ./oracle.sh "<chat text the agents read>" ["<short notice text>"]
# The notice text defaults to the chat text when the 2nd arg is omitted. notices are capped to the last 6.
set -euo pipefail
T="${1:-}"; N="${2:-$T}"
[ -z "$T" ] && { echo "usage: $0 \"<chat text>\" [\"<notice text>\"]"; exit 1; }
kubectl -n nha-mmo exec -i deploy/postgres -- psql -U nhamoo -d nhamoo \
  -v ON_ERROR_STOP=1 -v msg="$T" -v notice="THE UNIVERSE WHISPERS: $N" <<'SQL'
INSERT INTO entities(type,x,y,attrs)
  SELECT 'oracle',0,0,'{"name":"🌌 THE UNIVERSE"}'::jsonb
  WHERE NOT EXISTS (SELECT 1 FROM entities WHERE type='oracle');
WITH w AS (SELECT tick FROM world WHERE id=1), o AS (SELECT id FROM entities WHERE type='oracle' LIMIT 1)
INSERT INTO messages(tick,sender,recipient,text) SELECT w.tick, o.id, NULL, :'msg' FROM w,o;
UPDATE world SET notices = (
    (CASE WHEN jsonb_array_length(notices) >= 6 THEN notices - 0 ELSE notices END)
    || jsonb_build_object('tick',(SELECT tick FROM world WHERE id=1),'text', :'notice')
  ) WHERE id=1;
SQL
echo "🌌 whispered: $T"
