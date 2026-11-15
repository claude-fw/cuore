-- GENERATO da src/db/migrations con `npm run migrations:generate`. NON MODIFICARE A MANO.
-- versione: 3 | nome: planning | checksum: cf7c8d98362d74358f7a197f660573dbeb78ab7b15dda405461c37a437724540
-- guardia: si applica solo a un database alla versione 2 con le migrazioni precedenti integre

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 2, COALESCE(MAX(version), 0) FROM schema_migrations;

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 1 AND name = 'core' AND checksum = '2b0016addb27d6465103a9f916bd889b01fbbf75d680a73000aebab39ef29758';

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 2 AND name = 'classification_review' AND checksum = '384b6da6dd2d71635aa9f1f55ee2ff940941390c18d9b1cfc9aa555e26b20dcd';

DELETE FROM write_guard;

CREATE TABLE savings_goals (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      target_minor INTEGER NOT NULL CHECK (target_minor > 0),
      currency TEXT NOT NULL,
      deadline TEXT,
      status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'reached', 'cancelled')),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    ) STRICT;

CREATE TRIGGER savings_goals_no_delete BEFORE DELETE ON savings_goals
      BEGIN SELECT RAISE(ABORT, 'nessuna cancellazione degli obiettivi (usare lo stato cancelled)'); END;

CREATE TABLE goal_allocations (
      id TEXT PRIMARY KEY,
      goal_id TEXT NOT NULL REFERENCES savings_goals(id),
      amount_minor INTEGER NOT NULL CHECK (amount_minor <> 0),
      currency TEXT NOT NULL,
      allocated_on TEXT NOT NULL,
      note TEXT,
      created_at TEXT NOT NULL
    ) STRICT;

CREATE TRIGGER goal_allocations_no_update BEFORE UPDATE ON goal_allocations BEGIN SELECT RAISE(ABORT, 'accantonamenti immutabili (correggere con un movimento opposto): goal_allocations non si modifica'); END;

CREATE TRIGGER goal_allocations_no_delete BEFORE DELETE ON goal_allocations BEGIN SELECT RAISE(ABORT, 'accantonamenti immutabili (correggere con un movimento opposto): goal_allocations non si cancella'); END;

CREATE TABLE planned_purchases (
      id TEXT PRIMARY KEY,
      title TEXT NOT NULL,
      estimated_minor INTEGER CHECK (estimated_minor IS NULL OR estimated_minor > 0),
      currency TEXT NOT NULL,
      priority INTEGER CHECK (priority IS NULL OR priority BETWEEN 1 AND 5),
      status TEXT NOT NULL CHECK (status IN ('idea', 'planned', 'purchased', 'cancelled')),
      goal_id TEXT REFERENCES savings_goals(id),
      notion_block_id TEXT UNIQUE,
      transaction_id TEXT REFERENCES transactions(id),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      CHECK (transaction_id IS NULL OR status = 'purchased')
    ) STRICT;

CREATE TRIGGER planned_purchases_no_delete BEFORE DELETE ON planned_purchases
      BEGIN SELECT RAISE(ABORT, 'nessuna cancellazione degli acquisti pianificati (usare lo stato cancelled)'); END;

CREATE TABLE fx_rates (
      rate_date TEXT NOT NULL,
      base TEXT NOT NULL,
      quote TEXT NOT NULL,
      rate TEXT NOT NULL,
      source TEXT NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY (rate_date, base, quote, source),
      CHECK (base <> quote)
    ) STRICT;

CREATE TRIGGER fx_rates_no_update BEFORE UPDATE ON fx_rates BEGIN SELECT RAISE(ABORT, 'cambi osservati immutabili: fx_rates non si modifica'); END;

CREATE TRIGGER fx_rates_no_delete BEFORE DELETE ON fx_rates BEGIN SELECT RAISE(ABORT, 'cambi osservati immutabili: fx_rates non si cancella'); END;

INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (3, 'planning', 'cf7c8d98362d74358f7a197f660573dbeb78ab7b15dda405461c37a437724540', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
