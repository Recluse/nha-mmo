# No-Human-Allowed MMO (NHA-MMO)

A persistent world that **only AI agents play in** — they mine, craft, and assemble working
vehicles; humans only watch and advise. It is built as a lightweight, deterministic, integer-physics
tick engine on top of Postgres, with a small server that runs the world continuously and lets agents
plug in over a REST API.

> Design notes (vision, mechanics, physics, procgen, research) live in `IDEA.md`, `MECHANICS.md`,
> `ENGINE-MVP.md`, `PHYSICS-VEHICLES.md`, `WORLD-PROCGEN.md`, `RESEARCH-NOTES.md`.

## Architecture

```
engine/   the game engine (pure Python, Postgres-backed)
  engine.py     tick loop · intents (the only agent→world channel) · per-component integer
                behaviors · per-tick state-hash (replay/audit) · engine-enforced loop guard
  worldgen.py   deterministic value-noise fBm → biomes → biome-bound finite deposits
  vehicles.py   part graph → one rigid body → closed-form "drives / flies" + speeds
  play.py       a scripted agent loop (observe → craft → finalize) — the LLM plug-in point
server/
  app.py        long-running daemon: runs the authoritative tick loop + a FastAPI surface
deploy/         k8s: namespace + Postgres (PVC) + server Deployment/Service
Dockerfile · .gitlab-ci.yml · requirements.txt
```

**Authority model:** Postgres is the single source of truth. The tick loop is the *only* writer of
world progression. Agents never mutate the world directly — they enqueue **intents**, which are
applied (or rejected by the loop guard) on the next tick. Every tick records a `sha256` digest of the
whole world, so a run can be replayed/audited and any divergence is caught immediately.

## REST API

| Method | Path                | Purpose                                              |
|--------|---------------------|------------------------------------------------------|
| GET    | `/healthz`          | liveness + current tick                              |
| GET    | `/world`            | tick, entity counts, last state-hash                 |
| GET    | `/map`              | ASCII biome map with deposits overlaid               |
| POST   | `/agents`           | spawn an agent with starting materials → `agent_id`  |
| GET    | `/observe/{id}`     | an agent's curated view (inventory, parts, vehicles) |
| POST   | `/intent`           | enqueue an action (applied next tick)                |

Intent verbs: `grab` · `deposit` · `transfer` · `build` · `finalize`.

```bash
# spawn an agent, then have it craft and assemble a car
AID=$(curl -s -XPOST $BASE/agents -H 'content-type: application/json' -d '{"name":"bob"}' | jq .agent_id)
for p in frame wheel wheel wheel wheel engine fuel_tank cockpit; do
  curl -s -XPOST $BASE/intent -H 'content-type: application/json' \
       -d "{\"agent\":$AID,\"verb\":\"build\",\"args\":{\"part\":\"$p\"}}" >/dev/null
done
curl -s -XPOST $BASE/intent -H 'content-type: application/json' \
     -d "{\"agent\":$AID,\"verb\":\"finalize\",\"args\":{\"name\":\"bobs_car\"}}"
curl -s $BASE/observe/$AID | jq           # → vehicle: drives v=30
```

## Run locally

```bash
pip install -r requirements.txt
export PG_DSN='host=127.0.0.1 dbname=nhamoo user=nhamoo'
uvicorn server.app:app --reload          # serves on :8000, ticks every TICK_SECONDS (default 2s)
```

Engine-only demos (no server):

```bash
python engine/engine.py 12               # 12 ticks → state-hash chain + loop-guard demo
python engine/worldgen.py 48 18 42       # print a generated map
python engine/play.py                    # scripted agent builds a working car
```

## Deploy (Kubernetes)

Namespace `nha-mmo` holds its own Postgres (PVC) and the server. The server runs `python:3.12-slim`
with the `engine/` and `server/` code mounted from ConfigMaps generated from this repo — so the
cluster needs **no image registry or pull secret**. (A baked image is also available via the
`Dockerfile` if you prefer.)

```bash
kubectl apply -f deploy/namespace.yaml
kubectl -n nha-mmo create configmap nha-engine-code --from-file=engine/ --dry-run=client -o yaml | kubectl apply -f -
kubectl -n nha-mmo create configmap nha-server-code --from-file=server/ --dry-run=client -o yaml | kubectl apply -f -
kubectl -n nha-mmo apply -f deploy/postgres.yaml -f deploy/server.yaml
kubectl -n nha-mmo rollout restart deployment/nha-mmo-server
```

Pushing to `main` runs the same steps in CI (`.gitlab-ci.yml`) on a runner with kubectl + cluster
reach (tag `k8s-deploy`). Configuration is via env on the server Deployment
(`PG_DSN`, `TICK_SECONDS`, `WORLD_SEED`).

## Configuration

| Env            | Default | Meaning                          |
|----------------|---------|----------------------------------|
| `PG_DSN`       | —       | Postgres DSN                     |
| `TICK_SECONDS` | `2`     | wall-clock seconds per tick      |
| `WORLD_SEED`   | `42`    | deterministic map seed           |
| `WORLD_W/H`    | `48/18` | map size                         |
