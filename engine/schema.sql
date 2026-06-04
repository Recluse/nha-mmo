-- NHA-MMO — world state (PostgreSQL). Authoritative, serializable, auditable.
CREATE TABLE IF NOT EXISTS world (
  id   int PRIMARY KEY DEFAULT 1,
  tick int NOT NULL DEFAULT 0
);
INSERT INTO world (id, tick) VALUES (1, 0) ON CONFLICT DO NOTHING;

-- every object/component/agent/source is an entity
CREATE TABLE IF NOT EXISTS entities (
  id      bigserial PRIMARY KEY,
  type    text   NOT NULL,                 -- agent | battery | solar | generator | drill | furnace | container | ore_deposit ...
  x       int    NOT NULL DEFAULT 0,
  y       int    NOT NULL DEFAULT 0,
  owner   bigint,                          -- agent id; NULL = world fixture
  buffers jsonb  NOT NULL DEFAULT '{}',    -- {resource: int}  (conserved integer stocks)
  attrs   jsonb  NOT NULL DEFAULT '{}'     -- type-specific (capacity, deposit remaining, resource kind, ...)
);

-- typed ports (the visible "API handles")
CREATE TABLE IF NOT EXISTS ports (
  id     bigserial PRIMARY KEY,
  entity bigint NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  name   text   NOT NULL,
  kind   text   NOT NULL,    -- power | fluid | item | signal | mount
  dir    text   NOT NULL     -- in | out | bi
);
CREATE INDEX IF NOT EXISTS ports_entity_idx ON ports(entity);

-- connections between compatible ports
CREATE TABLE IF NOT EXISTS links (
  id bigserial PRIMARY KEY,
  a  bigint NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
  b  bigint NOT NULL REFERENCES ports(id) ON DELETE CASCADE
);

-- queued agent actions (the only way agents touch the world)
CREATE TABLE IF NOT EXISTS intents (
  id      bigserial PRIMARY KEY,
  agent   bigint NOT NULL,
  verb    text   NOT NULL,                 -- grab | deposit | transfer | move | attach | ...
  args    jsonb  NOT NULL DEFAULT '{}',
  status  text   NOT NULL DEFAULT 'pending', -- pending | applied | rejected
  result  text,
  created int
);
CREATE INDEX IF NOT EXISTS intents_pending_idx ON intents(status) WHERE status = 'pending';

-- append-only event log (observations / audit / replay)
CREATE TABLE IF NOT EXISTS events (
  id     bigserial PRIMARY KEY,
  tick   int  NOT NULL,
  entity bigint,
  kind   text NOT NULL,
  data   jsonb NOT NULL DEFAULT '{}'
);
