-- GENERATO da src/db/migrations con `npm run migrations:generate`. NON MODIFICARE A MANO.
-- versione: 1 | nome: core | checksum: 2b0016addb27d6465103a9f916bd889b01fbbf75d680a73000aebab39ef29758
-- guardia: la creazione delle tabelle fallisce se il database e' gia' inizializzato

CREATE TABLE meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    ) STRICT;

INSERT INTO meta (key, value) VALUES ('write_seq', '0');

CREATE TABLE write_guard (
      expected_seq INTEGER NOT NULL,
      actual_seq INTEGER NOT NULL,
      CONSTRAINT write_guard_seq CHECK (expected_seq = actual_seq)
    ) STRICT;

CREATE TABLE schema_migrations (
      version INTEGER PRIMARY KEY,
      name TEXT NOT NULL,
      checksum TEXT NOT NULL,
      applied_at TEXT NOT NULL
    ) STRICT;

CREATE TRIGGER schema_migrations_no_update BEFORE UPDATE ON schema_migrations BEGIN SELECT RAISE(ABORT, 'storico delle migrazioni immutabile: schema_migrations non si modifica'); END;

CREATE TRIGGER schema_migrations_no_delete BEFORE DELETE ON schema_migrations BEGIN SELECT RAISE(ABORT, 'storico delle migrazioni immutabile: schema_migrations non si cancella'); END;

CREATE TABLE institutions (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      created_at TEXT NOT NULL
    ) STRICT;

CREATE TABLE accounts (
      id TEXT PRIMARY KEY,
      institution_id TEXT REFERENCES institutions(id),
      name TEXT NOT NULL,
      type TEXT NOT NULL CHECK (type IN ('current', 'savings', 'card', 'cash', 'investment', 'other')),
      currency TEXT NOT NULL CHECK (length(currency) = 3 AND currency = upper(currency)),
      holder_role TEXT NOT NULL CHECK (holder_role IN ('owner', 'joint', 'delegate', 'other')),
      created_at TEXT NOT NULL,
      closed_at TEXT
    ) STRICT;

CREATE TABLE sources (
      id TEXT PRIMARY KEY,
      kind TEXT NOT NULL CHECK (kind IN ('file', 'api', 'manual')),
      name TEXT NOT NULL,
      created_at TEXT NOT NULL
    ) STRICT;

INSERT INTO sources (id, kind, name, created_at) VALUES ('manual', 'manual', 'Inserimento manuale', '1970-01-01T00:00:00.000Z');

CREATE TABLE import_runs (
      id TEXT PRIMARY KEY,
      source_id TEXT NOT NULL REFERENCES sources(id),
      account_id TEXT NOT NULL REFERENCES accounts(id),
      parser_id TEXT NOT NULL,
      parser_version INTEGER NOT NULL,
      file_name TEXT,
      file_sha256 TEXT NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('committed', 'rejected', 'duplicate_file')),
      started_at TEXT NOT NULL,
      finished_at TEXT NOT NULL,
      counts_json TEXT NOT NULL,
      errors_json TEXT NOT NULL
    ) STRICT;

CREATE INDEX import_runs_by_file ON import_runs (account_id, parser_id, file_sha256, status);

CREATE TRIGGER import_runs_no_update BEFORE UPDATE ON import_runs BEGIN SELECT RAISE(ABORT, 'storico delle importazioni immutabile: import_runs non si modifica'); END;

CREATE TRIGGER import_runs_no_delete BEFORE DELETE ON import_runs BEGIN SELECT RAISE(ABORT, 'storico delle importazioni immutabile: import_runs non si cancella'); END;

CREATE TABLE raw_records (
      id TEXT PRIMARY KEY,
      import_run_id TEXT NOT NULL REFERENCES import_runs(id),
      line_number INTEGER NOT NULL,
      record_type TEXT NOT NULL CHECK (record_type IN ('transaction', 'balance')),
      content_sha256 TEXT NOT NULL,
      raw_json TEXT NOT NULL,
      UNIQUE (import_run_id, line_number)
    ) STRICT;

CREATE TRIGGER raw_records_no_update BEFORE UPDATE ON raw_records BEGIN SELECT RAISE(ABORT, 'storico grezzo immutabile: raw_records non si modifica'); END;

CREATE TRIGGER raw_records_no_delete BEFORE DELETE ON raw_records BEGIN SELECT RAISE(ABORT, 'storico grezzo immutabile: raw_records non si cancella'); END;

CREATE TABLE transactions (
      id TEXT PRIMARY KEY,
      account_id TEXT NOT NULL REFERENCES accounts(id),
      origin TEXT NOT NULL CHECK (origin IN ('import', 'manual')),
      source_txn_id TEXT,
      fingerprint TEXT NOT NULL,
      occurrence INTEGER,
      status TEXT NOT NULL CHECK (status IN ('pending', 'booked')),
      transaction_date TEXT NOT NULL,
      booking_date TEXT,
      amount_minor INTEGER NOT NULL,
      currency TEXT NOT NULL,
      original_amount_minor INTEGER,
      original_currency TEXT,
      description TEXT NOT NULL,
      counterparty TEXT,
      description_norm TEXT NOT NULL,
      counterparty_norm TEXT,
      first_import_run_id TEXT REFERENCES import_runs(id),
      last_import_run_id TEXT REFERENCES import_runs(id),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      CHECK ((source_txn_id IS NULL) = (occurrence IS NOT NULL)),
      CHECK (occurrence IS NULL OR occurrence >= 1),
      CHECK ((original_amount_minor IS NULL) = (original_currency IS NULL)),
      CHECK (status = 'pending' OR booking_date IS NOT NULL)
    ) STRICT;

CREATE UNIQUE INDEX transactions_by_source_id ON transactions (account_id, source_txn_id)
      WHERE source_txn_id IS NOT NULL;

CREATE UNIQUE INDEX transactions_by_fingerprint ON transactions (account_id, fingerprint, occurrence)
      WHERE source_txn_id IS NULL;

CREATE INDEX transactions_by_date ON transactions (account_id, transaction_date);

CREATE TRIGGER transactions_identity_fixed BEFORE UPDATE ON transactions
      WHEN NEW.id IS NOT OLD.id OR NEW.account_id IS NOT OLD.account_id OR NEW.origin IS NOT OLD.origin
        OR NEW.source_txn_id IS NOT OLD.source_txn_id OR NEW.created_at IS NOT OLD.created_at
        OR NEW.first_import_run_id IS NOT OLD.first_import_run_id
      BEGIN SELECT RAISE(ABORT, 'identita della transazione immutabile'); END;

CREATE TRIGGER transactions_no_booked_to_pending BEFORE UPDATE ON transactions
      WHEN OLD.status = 'booked' AND NEW.status = 'pending'
      BEGIN SELECT RAISE(ABORT, 'una transazione contabilizzata non torna in attesa'); END;

CREATE TRIGGER transactions_no_delete BEFORE DELETE ON transactions
      BEGIN SELECT RAISE(ABORT, 'nessuna cancellazione di transazioni'); END;

CREATE TABLE transaction_observations (
      transaction_id TEXT NOT NULL REFERENCES transactions(id),
      import_run_id TEXT NOT NULL REFERENCES import_runs(id),
      raw_record_id TEXT NOT NULL REFERENCES raw_records(id),
      outcome TEXT NOT NULL CHECK (outcome IN ('created', 'unchanged', 'updated', 'conflict')),
      PRIMARY KEY (transaction_id, raw_record_id)
    ) STRICT;

CREATE TRIGGER transaction_observations_no_update BEFORE UPDATE ON transaction_observations BEGIN SELECT RAISE(ABORT, 'provenienza immutabile: transaction_observations non si modifica'); END;

CREATE TRIGGER transaction_observations_no_delete BEFORE DELETE ON transaction_observations BEGIN SELECT RAISE(ABORT, 'provenienza immutabile: transaction_observations non si cancella'); END;

CREATE TABLE balance_observations (
      id TEXT PRIMARY KEY,
      account_id TEXT NOT NULL REFERENCES accounts(id),
      as_of_date TEXT NOT NULL,
      balance_minor INTEGER NOT NULL,
      currency TEXT NOT NULL,
      kind TEXT NOT NULL CHECK (kind IN ('end_of_day')),
      source_id TEXT NOT NULL REFERENCES sources(id),
      import_run_id TEXT REFERENCES import_runs(id),
      raw_record_id TEXT REFERENCES raw_records(id),
      note TEXT,
      created_at TEXT NOT NULL,
      UNIQUE (account_id, as_of_date, kind, source_id, balance_minor)
    ) STRICT;

CREATE TRIGGER balance_observations_no_update BEFORE UPDATE ON balance_observations BEGIN SELECT RAISE(ABORT, 'osservazioni dei saldi immutabili: balance_observations non si modifica'); END;

CREATE TRIGGER balance_observations_no_delete BEFORE DELETE ON balance_observations BEGIN SELECT RAISE(ABORT, 'osservazioni dei saldi immutabili: balance_observations non si cancella'); END;

CREATE TABLE audit_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      at TEXT NOT NULL,
      actor TEXT NOT NULL CHECK (actor IN ('system', 'import', 'rule', 'user')),
      action TEXT NOT NULL,
      entity TEXT NOT NULL,
      entity_id TEXT NOT NULL,
      before_json TEXT,
      after_json TEXT,
      reason TEXT,
      import_run_id TEXT
    ) STRICT;

CREATE INDEX audit_by_entity ON audit_log (entity, entity_id);

CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit immutabile: audit_log non si modifica'); END;

CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit immutabile: audit_log non si cancella'); END;

INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (1, 'core', '2b0016addb27d6465103a9f916bd889b01fbbf75d680a73000aebab39ef29758', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
