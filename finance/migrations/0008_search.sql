-- GENERATO da src/db/migrations con `npm run migrations:generate`. NON MODIFICARE A MANO.
-- versione: 8 | nome: search | checksum: f0cab2d5cb311f5eba5214b1ae986aefd1739b93f238f27064a866f0ac94ba40
-- guardia: si applica solo a un database alla versione 7 con le migrazioni precedenti integre

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 7, COALESCE(MAX(version), 0) FROM schema_migrations;

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 1 AND name = 'core' AND checksum = '2b0016addb27d6465103a9f916bd889b01fbbf75d680a73000aebab39ef29758';

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 2 AND name = 'classification_review' AND checksum = '384b6da6dd2d71635aa9f1f55ee2ff940941390c18d9b1cfc9aa555e26b20dcd';

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 3 AND name = 'planning' AND checksum = 'cf7c8d98362d74358f7a197f660573dbeb78ab7b15dda405461c37a437724540';

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 4 AND name = 'bank_formats' AND checksum = '452cb695698eecf4367e8d3f03cc0e749afa69525c46f0d8056f033607a00c9b';

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 5 AND name = 'obligations' AND checksum = '4387459b89dc7a928c4783e66d1ec84b8045235632143dd37b42c90f9d9cebe8';

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 6 AND name = 'account_profiles' AND checksum = 'ce92184670dc5f9e44acf8f84795cc8b71b09395036a10fdb17601c24e7ec738';

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 7 AND name = 'manual_and_budgets' AND checksum = '583b0418a38686caf61681ae4144111109fc788bfa3526a82d3b9de7df389e84';

DELETE FROM write_guard;

CREATE VIEW transactions_search_source AS
      SELECT t.rowid AS search_rowid, t.description AS description, t.counterparty AS counterparty,
        (SELECT a.reason FROM audit_log a WHERE a.entity = 'transaction' AND a.entity_id = t.id
      AND a.action = 'add_manual_transaction' ORDER BY a.id LIMIT 1) AS note
      FROM transactions t;

CREATE VIRTUAL TABLE transactions_search USING fts5(
      description, counterparty, note,
      content = 'transactions_search_source', content_rowid = 'search_rowid',
      tokenize = 'unicode61 remove_diacritics 2'
    );

CREATE TRIGGER transactions_search_after_insert AFTER INSERT ON transactions BEGIN
      INSERT INTO transactions_search (rowid, description, counterparty, note)
        VALUES (NEW.rowid, NEW.description, NEW.counterparty, (SELECT a.reason FROM audit_log a WHERE a.entity = 'transaction' AND a.entity_id = NEW.id
      AND a.action = 'add_manual_transaction' ORDER BY a.id LIMIT 1));
    END;

CREATE TRIGGER transactions_search_after_update AFTER UPDATE OF description, counterparty ON transactions
      WHEN NEW.description IS NOT OLD.description OR NEW.counterparty IS NOT OLD.counterparty BEGIN
      INSERT INTO transactions_search (transactions_search, rowid, description, counterparty, note)
        VALUES ('delete', OLD.rowid, OLD.description, OLD.counterparty, (SELECT a.reason FROM audit_log a WHERE a.entity = 'transaction' AND a.entity_id = OLD.id
      AND a.action = 'add_manual_transaction' ORDER BY a.id LIMIT 1));
      INSERT INTO transactions_search (rowid, description, counterparty, note)
        VALUES (NEW.rowid, NEW.description, NEW.counterparty, (SELECT a.reason FROM audit_log a WHERE a.entity = 'transaction' AND a.entity_id = NEW.id
      AND a.action = 'add_manual_transaction' ORDER BY a.id LIMIT 1));
    END;

CREATE TRIGGER transactions_search_after_manual_note AFTER INSERT ON audit_log
      WHEN NEW.entity = 'transaction' AND NEW.action = 'add_manual_transaction' AND NEW.reason IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM audit_log a WHERE a.entity = 'transaction' AND a.entity_id = NEW.entity_id
          AND a.action = 'add_manual_transaction' AND a.id < NEW.id) BEGIN
      INSERT INTO transactions_search (transactions_search, rowid, description, counterparty, note)
        SELECT 'delete', t.rowid, t.description, t.counterparty, NULL FROM transactions t WHERE t.id = NEW.entity_id;
      INSERT INTO transactions_search (rowid, description, counterparty, note)
        SELECT t.rowid, t.description, t.counterparty, NEW.reason FROM transactions t WHERE t.id = NEW.entity_id;
    END;

INSERT INTO transactions_search (transactions_search) VALUES ('rebuild');

INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (8, 'search', 'f0cab2d5cb311f5eba5214b1ae986aefd1739b93f238f27064a866f0ac94ba40', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
