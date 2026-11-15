-- GENERATO da src/db/migrations con `npm run migrations:generate`. NON MODIFICARE A MANO.
-- versione: 5 | nome: obligations | checksum: 4387459b89dc7a928c4783e66d1ec84b8045235632143dd37b42c90f9d9cebe8
-- guardia: si applica solo a un database alla versione 4 con le migrazioni precedenti integre

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 4, COALESCE(MAX(version), 0) FROM schema_migrations;

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 1 AND name = 'core' AND checksum = '2b0016addb27d6465103a9f916bd889b01fbbf75d680a73000aebab39ef29758';

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 2 AND name = 'classification_review' AND checksum = '384b6da6dd2d71635aa9f1f55ee2ff940941390c18d9b1cfc9aa555e26b20dcd';

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 3 AND name = 'planning' AND checksum = 'cf7c8d98362d74358f7a197f660573dbeb78ab7b15dda405461c37a437724540';

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 4 AND name = 'bank_formats' AND checksum = '452cb695698eecf4367e8d3f03cc0e749afa69525c46f0d8056f033607a00c9b';

DELETE FROM write_guard;

CREATE TABLE counterparties (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      note TEXT,
      created_at TEXT NOT NULL
    ) STRICT;

CREATE TRIGGER counterparties_no_update BEFORE UPDATE ON counterparties BEGIN SELECT RAISE(ABORT, 'controparti immutabili (crearne una nuova per cambiare): counterparties non si modifica'); END;

CREATE TRIGGER counterparties_no_delete BEFORE DELETE ON counterparties BEGIN SELECT RAISE(ABORT, 'controparti immutabili (crearne una nuova per cambiare): counterparties non si cancella'); END;

CREATE TABLE obligations (
      id TEXT PRIMARY KEY,
      counterparty_id TEXT NOT NULL REFERENCES counterparties(id),
      direction TEXT NOT NULL CHECK (direction IN ('receivable', 'payable')),
      currency TEXT NOT NULL,
      description TEXT NOT NULL,
      opened_on TEXT NOT NULL,
      due_on TEXT,
      created_at TEXT NOT NULL,
      CHECK (due_on IS NULL OR due_on >= opened_on)
    ) STRICT;

CREATE TRIGGER obligations_no_update BEFORE UPDATE ON obligations BEGIN SELECT RAISE(ABORT, 'crediti e debiti immutabili (si cambiano solo con eventi): obligations non si modifica'); END;

CREATE TRIGGER obligations_no_delete BEFORE DELETE ON obligations BEGIN SELECT RAISE(ABORT, 'crediti e debiti immutabili (si cambiano solo con eventi): obligations non si cancella'); END;

CREATE TABLE obligation_events (
      id TEXT PRIMARY KEY,
      obligation_id TEXT NOT NULL REFERENCES obligations(id),
      kind TEXT NOT NULL CHECK (kind IN ('open', 'increase', 'payment', 'forgive', 'adjust')),
      amount_minor INTEGER NOT NULL,
      occurred_on TEXT NOT NULL,
      transaction_id TEXT REFERENCES transactions(id),
      note TEXT,
      created_at TEXT NOT NULL,
      CHECK (kind = 'adjust' OR amount_minor > 0),
      CHECK (kind <> 'adjust' OR amount_minor <> 0),
      CHECK (kind <> 'adjust' OR length(trim(coalesce(note, ''))) > 0),
      CHECK (transaction_id IS NULL OR kind IN ('open', 'increase', 'payment'))
    ) STRICT;

CREATE UNIQUE INDEX obligation_events_one_open ON obligation_events (obligation_id) WHERE kind = 'open';

CREATE INDEX obligation_events_by_obligation ON obligation_events (obligation_id, occurred_on);

CREATE INDEX obligation_events_by_transaction ON obligation_events (transaction_id)
      WHERE transaction_id IS NOT NULL;

CREATE TRIGGER obligation_events_no_update BEFORE UPDATE ON obligation_events BEGIN SELECT RAISE(ABORT, 'eventi di crediti e debiti immutabili (correggere con una rettifica): obligation_events non si modifica'); END;

CREATE TRIGGER obligation_events_no_delete BEFORE DELETE ON obligation_events BEGIN SELECT RAISE(ABORT, 'eventi di crediti e debiti immutabili (correggere con una rettifica): obligation_events non si cancella'); END;

INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (5, 'obligations', '4387459b89dc7a928c4783e66d1ec84b8045235632143dd37b42c90f9d9cebe8', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
