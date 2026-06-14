-- GENERATED FILE — do not hand-edit.
-- The authoritative schema is the SCHEMA constant in engine/engine.py, applied idempotently
-- at every startup. This file is a readable mirror for reference only (nothing loads it).

CREATE TABLE IF NOT EXISTS world (id int PRIMARY KEY DEFAULT 1, tick int NOT NULL DEFAULT 0);
ALTER TABLE world ADD COLUMN IF NOT EXISTS notices jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE world ADD COLUMN IF NOT EXISTS era text NOT NULL DEFAULT 'architect';
INSERT INTO world (id, tick) VALUES (1,0) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS entities (id bigserial PRIMARY KEY, type text NOT NULL,
  x int NOT NULL DEFAULT 0, y int NOT NULL DEFAULT 0, owner bigint,
  buffers jsonb NOT NULL DEFAULT '{}', attrs jsonb NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS entities_type_idx ON entities(type);
CREATE INDEX IF NOT EXISTS entities_owner_idx ON entities(owner) WHERE owner IS NOT NULL;
CREATE TABLE IF NOT EXISTS intents (id bigserial PRIMARY KEY, agent bigint NOT NULL,
  verb text NOT NULL, args jsonb NOT NULL DEFAULT '{}', status text NOT NULL DEFAULT 'pending',
  result text, created int);
CREATE INDEX IF NOT EXISTS intents_agent_idx ON intents(agent, id);
CREATE TABLE IF NOT EXISTS events (id bigserial PRIMARY KEY, tick int NOT NULL,
  entity bigint, kind text NOT NULL, data jsonb NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS events_kind_tick_idx ON events(kind, tick);
CREATE INDEX IF NOT EXISTS events_entity_kind_tick_idx ON events(entity, kind, tick);   -- per-agent EXISTS/max(tick) subqueries in /agents,/scene,/roster (filter by entity+kind, order by tick)
CREATE TABLE IF NOT EXISTS tick_hashes (tick int PRIMARY KEY, hash text NOT NULL);
CREATE TABLE IF NOT EXISTS market_orders (id bigserial PRIMARY KEY, agent bigint NOT NULL,
  side text NOT NULL, resource text NOT NULL, qty int NOT NULL, price int NOT NULL,
  status text NOT NULL DEFAULT 'open', created int);
CREATE INDEX IF NOT EXISTS market_open_idx ON market_orders(resource, side, status);
CREATE TABLE IF NOT EXISTS trades (id bigserial PRIMARY KEY, proposer bigint NOT NULL,
  target bigint NOT NULL, give jsonb NOT NULL, want jsonb NOT NULL,
  status text NOT NULL DEFAULT 'open', created int);
CREATE TABLE IF NOT EXISTS messages (id bigserial PRIMARY KEY, tick int NOT NULL,
  sender bigint NOT NULL, recipient bigint, text text NOT NULL);
CREATE TABLE IF NOT EXISTS discoveries (rule_key text PRIMARY KEY, name text NOT NULL,
  discoverer bigint NOT NULL, discoverer_name text, tick int NOT NULL, points int NOT NULL DEFAULT 0);
ALTER TABLE discoveries ADD COLUMN IF NOT EXISTS discoverer_name text;
CREATE TABLE IF NOT EXISTS proposals (id bigserial PRIMARY KEY, agent bigint NOT NULL,
  ings jsonb NOT NULL, sig text NOT NULL, proposed_name text,
  status text NOT NULL DEFAULT 'pending', item_key text, item_name text, props jsonb,
  points int, reason text, tick int NOT NULL);
CREATE INDEX IF NOT EXISTS proposals_status_idx ON proposals(status);
CREATE TABLE IF NOT EXISTS dynamic_rules (sig text PRIMARY KEY, item_key text NOT NULL, name text NOT NULL,
  props jsonb NOT NULL DEFAULT '{}', discoverer bigint NOT NULL, discoverer_name text, points int NOT NULL DEFAULT 0, tick int NOT NULL);
ALTER TABLE dynamic_rules ADD COLUMN IF NOT EXISTS discoverer_name text;
