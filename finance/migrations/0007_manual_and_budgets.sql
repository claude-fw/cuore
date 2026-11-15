-- GENERATO da src/db/migrations con `npm run migrations:generate`. NON MODIFICARE A MANO.
-- versione: 7 | nome: manual_and_budgets | checksum: 583b0418a38686caf61681ae4144111109fc788bfa3526a82d3b9de7df389e84
-- guardia: si applica solo a un database alla versione 6 con le migrazioni precedenti integre

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 6, COALESCE(MAX(version), 0) FROM schema_migrations;

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 1 AND name = 'core' AND checksum = '2b0016addb27d6465103a9f916bd889b01fbbf75d680a73000aebab39ef29758';

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 2 AND name = 'classification_review' AND checksum = '384b6da6dd2d71635aa9f1f55ee2ff940941390c18d9b1cfc9aa555e26b20dcd';

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 3 AND name = 'planning' AND checksum = 'cf7c8d98362d74358f7a197f660573dbeb78ab7b15dda405461c37a437724540';

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 4 AND name = 'bank_formats' AND checksum = '452cb695698eecf4367e8d3f03cc0e749afa69525c46f0d8056f033607a00c9b';

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 5 AND name = 'obligations' AND checksum = '4387459b89dc7a928c4783e66d1ec84b8045235632143dd37b42c90f9d9cebe8';

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 6 AND name = 'account_profiles' AND checksum = 'ce92184670dc5f9e44acf8f84795cc8b71b09395036a10fdb17601c24e7ec738';

DELETE FROM write_guard;

CREATE TABLE manual_voids (
      transaction_id TEXT PRIMARY KEY REFERENCES transactions(id),
      reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
      created_at TEXT NOT NULL
    ) STRICT;

CREATE TRIGGER manual_voids_no_update BEFORE UPDATE ON manual_voids BEGIN SELECT RAISE(ABORT, 'annullamenti immutabili: manual_voids non si modifica'); END;

CREATE TRIGGER manual_voids_no_delete BEFORE DELETE ON manual_voids BEGIN SELECT RAISE(ABORT, 'annullamenti immutabili: manual_voids non si cancella'); END;

CREATE TABLE link_reversals (
      link_id TEXT PRIMARY KEY REFERENCES transaction_links(id),
      reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
      created_at TEXT NOT NULL
    ) STRICT;

CREATE TRIGGER link_reversals_no_update BEFORE UPDATE ON link_reversals BEGIN SELECT RAISE(ABORT, 'revoche immutabili: link_reversals non si modifica'); END;

CREATE TRIGGER link_reversals_no_delete BEFORE DELETE ON link_reversals BEGIN SELECT RAISE(ABORT, 'revoche immutabili: link_reversals non si cancella'); END;

CREATE TABLE budgets (
      id TEXT PRIMARY KEY,
      scope TEXT NOT NULL CHECK (scope IN ('category', 'budget_group', 'total')),
      target TEXT,
      valid_from TEXT NOT NULL CHECK (valid_from GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]'),
      amount_minor INTEGER CHECK (amount_minor IS NULL OR amount_minor > 0),
      currency TEXT NOT NULL,
      note TEXT,
      created_at TEXT NOT NULL,
      CHECK ((scope = 'total') = (target IS NULL)),
      CHECK (scope <> 'budget_group' OR target IN ('needs', 'wants', 'savings'))
    ) STRICT;

CREATE INDEX budgets_by_scope ON budgets (scope, target, currency, valid_from, created_at);

CREATE TRIGGER budgets_no_update BEFORE UPDATE ON budgets BEGIN SELECT RAISE(ABORT, 'budget immutabili (registrarne una nuova versione): budgets non si modifica'); END;

CREATE TRIGGER budgets_no_delete BEFORE DELETE ON budgets BEGIN SELECT RAISE(ABORT, 'budget immutabili (registrarne una nuova versione): budgets non si cancella'); END;

INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (7, 'manual_and_budgets', '583b0418a38686caf61681ae4144111109fc788bfa3526a82d3b9de7df389e84', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
