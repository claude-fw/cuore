-- GENERATO da src/db/migrations con `npm run migrations:generate`. NON MODIFICARE A MANO.
-- versione: 6 | nome: account_profiles | checksum: ce92184670dc5f9e44acf8f84795cc8b71b09395036a10fdb17601c24e7ec738
-- guardia: si applica solo a un database alla versione 5 con le migrazioni precedenti integre

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 5, COALESCE(MAX(version), 0) FROM schema_migrations;

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 1 AND name = 'core' AND checksum = '2b0016addb27d6465103a9f916bd889b01fbbf75d680a73000aebab39ef29758';

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 2 AND name = 'classification_review' AND checksum = '384b6da6dd2d71635aa9f1f55ee2ff940941390c18d9b1cfc9aa555e26b20dcd';

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 3 AND name = 'planning' AND checksum = 'cf7c8d98362d74358f7a197f660573dbeb78ab7b15dda405461c37a437724540';

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 4 AND name = 'bank_formats' AND checksum = '452cb695698eecf4367e8d3f03cc0e749afa69525c46f0d8056f033607a00c9b';

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 5 AND name = 'obligations' AND checksum = '4387459b89dc7a928c4783e66d1ec84b8045235632143dd37b42c90f9d9cebe8';

DELETE FROM write_guard;

CREATE TABLE account_profiles (
      id TEXT PRIMARY KEY,
      account_id TEXT NOT NULL REFERENCES accounts(id),
      formal_holder TEXT NOT NULL CHECK (formal_holder IN ('self', 'parent', 'self_with_parent', 'other', 'unknown')),
      primary_user TEXT NOT NULL CHECK (primary_user IN ('self', 'parent', 'shared', 'unknown')),
      supervision TEXT NOT NULL CHECK (supervision IN ('parent', 'none', 'unknown')),
      note TEXT,
      created_at TEXT NOT NULL
    ) STRICT;

CREATE INDEX account_profiles_by_account ON account_profiles (account_id, created_at);

CREATE TRIGGER account_profiles_no_update BEFORE UPDATE ON account_profiles BEGIN SELECT RAISE(ABORT, 'profili dei conti immutabili (dichiararne uno nuovo): account_profiles non si modifica'); END;

CREATE TRIGGER account_profiles_no_delete BEFORE DELETE ON account_profiles BEGIN SELECT RAISE(ABORT, 'profili dei conti immutabili (dichiararne uno nuovo): account_profiles non si cancella'); END;

CREATE TABLE account_status_events (
      id TEXT PRIMARY KEY,
      account_id TEXT NOT NULL REFERENCES accounts(id),
      status TEXT NOT NULL CHECK (status IN ('closed', 'reopened')),
      effective_date TEXT,
      effective_date_status TEXT NOT NULL CHECK (effective_date_status IN ('verified', 'declared', 'unknown')),
      last_movement_on TEXT,
      reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
      created_at TEXT NOT NULL,
      CHECK ((effective_date IS NULL) = (effective_date_status = 'unknown')),
      CHECK (effective_date IS NULL OR last_movement_on IS NULL OR effective_date >= last_movement_on)
    ) STRICT;

CREATE INDEX account_status_events_by_account ON account_status_events (account_id, created_at);

CREATE TRIGGER account_status_events_no_update BEFORE UPDATE ON account_status_events BEGIN SELECT RAISE(ABORT, 'stato dei conti immutabile (registrare un nuovo evento): account_status_events non si modifica'); END;

CREATE TRIGGER account_status_events_no_delete BEFORE DELETE ON account_status_events BEGIN SELECT RAISE(ABORT, 'stato dei conti immutabile (registrare un nuovo evento): account_status_events non si cancella'); END;

INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (6, 'account_profiles', 'ce92184670dc5f9e44acf8f84795cc8b71b09395036a10fdb17601c24e7ec738', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
