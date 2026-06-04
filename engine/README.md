# NHA-MMO engine — MVP skeleton

Lightweight **PostgreSQL-backed** tick engine. World state lives in Postgres (`schema.sql`); each tick
the engine loads state into memory, applies queued **intents**, runs tiny per-component **behaviors**
(integer-conserved), and writes back. Postgres is the authoritative, serializable, auditable store.

## Run
Needs Python 3.x + `psycopg2` + a PostgreSQL database.
```bash
createdb nhamoo
PG_DSN='host=127.0.0.1 dbname=nhamoo user=postgres' python engine.py 12
```
Self-creates the schema and, if the DB is empty, seeds a demo world — then runs N ticks and prints
entity state.

## Demo world
`solar` (+1⚡/tick) and a `generator` (1 fuel → 10⚡) charge a `battery`; a `drill` sitting on the
`ore_deposit` spends 5⚡ + 1 deposit-ore → 1 ore into a `container`. Watch over ticks: battery energy
rises, ore moves deposit → container, fuel burns down, the deposit depletes — all integer-conserved
(nothing created from nothing).

## How agents plug in
Agents never touch state directly — they insert rows into `intents` (`grab`/`deposit`/`transfer`/…)
and read `entities` + `events`. A thin REST/MCP layer over those two operations = the agent API.

## Files
- `schema.sql` — the PG schema (world / entities / ports / links / intents / events).
- `engine.py` — tick loop + behaviors + intents + self-seeding demo (schema embedded for one-file run).

## Extend
- More behaviors in `behave()` (a `furnace`: 2 ore + 1 fuel + 5⚡ → 1 metal is already wired).
- More verbs in `apply_intent()` (`attach`/`detach`/`build`/`move`/`signal`).
- Bigger worlds = shard by grid region; determinism + the event log give cheap replay/audit.

See `../MECHANICS.md` (principles) and `../ENGINE-MVP.md` (full design).
