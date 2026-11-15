-- GENERATO da src/db/migrations con `npm run migrations:generate`. NON MODIFICARE A MANO.
-- versione: 4 | nome: bank_formats | checksum: 452cb695698eecf4367e8d3f03cc0e749afa69525c46f0d8056f033607a00c9b
-- guardia: si applica solo a un database alla versione 3 con le migrazioni precedenti integre

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 3, COALESCE(MAX(version), 0) FROM schema_migrations;

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 1 AND name = 'core' AND checksum = '2b0016addb27d6465103a9f916bd889b01fbbf75d680a73000aebab39ef29758';

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 2 AND name = 'classification_review' AND checksum = '384b6da6dd2d71635aa9f1f55ee2ff940941390c18d9b1cfc9aa555e26b20dcd';

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 3 AND name = 'planning' AND checksum = 'cf7c8d98362d74358f7a197f660573dbeb78ab7b15dda405461c37a437724540';

DELETE FROM write_guard;

ALTER TABLE transactions ADD COLUMN source_type TEXT;

CREATE TABLE excluded_rows (
      id TEXT PRIMARY KEY,
      import_run_id TEXT NOT NULL REFERENCES import_runs(id),
      line_number INTEGER NOT NULL,
      reason TEXT NOT NULL,
      source_txn_id TEXT,
      content_sha256 TEXT NOT NULL,
      raw_json TEXT NOT NULL,
      UNIQUE (import_run_id, line_number)
    ) STRICT;

CREATE TRIGGER excluded_rows_no_update BEFORE UPDATE ON excluded_rows BEGIN SELECT RAISE(ABORT, 'righe escluse immutabili: excluded_rows non si modifica'); END;

CREATE TRIGGER excluded_rows_no_delete BEFORE DELETE ON excluded_rows BEGIN SELECT RAISE(ABORT, 'righe escluse immutabili: excluded_rows non si cancella'); END;

INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (4, 'bank_formats', '452cb695698eecf4367e8d3f03cc0e749afa69525c46f0d8056f033607a00c9b', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
