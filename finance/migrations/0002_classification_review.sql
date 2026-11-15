-- GENERATO da src/db/migrations con `npm run migrations:generate`. NON MODIFICARE A MANO.
-- versione: 2 | nome: classification_review | checksum: 384b6da6dd2d71635aa9f1f55ee2ff940941390c18d9b1cfc9aa555e26b20dcd
-- guardia: si applica solo a un database alla versione 1 con le migrazioni precedenti integre

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COALESCE(MAX(version), 0) FROM schema_migrations;

INSERT INTO write_guard (expected_seq, actual_seq) SELECT 1, COUNT(*) FROM schema_migrations WHERE version = 1 AND name = 'core' AND checksum = '2b0016addb27d6465103a9f916bd889b01fbbf75d680a73000aebab39ef29758';

DELETE FROM write_guard;

CREATE TABLE categories (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      parent_id TEXT REFERENCES categories(id),
      budget_group TEXT CHECK (budget_group IS NULL OR budget_group IN ('needs', 'wants', 'savings')),
      created_at TEXT NOT NULL
    ) STRICT;

CREATE TRIGGER categories_no_update BEFORE UPDATE ON categories BEGIN SELECT RAISE(ABORT, 'categorie immutabili (crearne una nuova per cambiare): categories non si modifica'); END;

CREATE TRIGGER categories_no_delete BEFORE DELETE ON categories BEGIN SELECT RAISE(ABORT, 'categorie immutabili (crearne una nuova per cambiare): categories non si cancella'); END;

CREATE TABLE rule_sets (
      version INTEGER PRIMARY KEY,
      checksum TEXT NOT NULL UNIQUE,
      note TEXT,
      created_at TEXT NOT NULL
    ) STRICT;

CREATE TRIGGER rule_sets_no_update BEFORE UPDATE ON rule_sets BEGIN SELECT RAISE(ABORT, 'versioni delle regole immutabili: rule_sets non si modifica'); END;

CREATE TRIGGER rule_sets_no_delete BEFORE DELETE ON rule_sets BEGIN SELECT RAISE(ABORT, 'versioni delle regole immutabili: rule_sets non si cancella'); END;

CREATE TABLE rules (
      rule_set_version INTEGER NOT NULL REFERENCES rule_sets(version),
      rule_id TEXT NOT NULL,
      priority INTEGER NOT NULL,
      description TEXT NOT NULL,
      conditions_json TEXT NOT NULL,
      kind TEXT NOT NULL CHECK (kind IN ('income', 'expense', 'transfer', 'refund', 'fee')),
      category_id TEXT REFERENCES categories(id),
      confidence TEXT NOT NULL CHECK (confidence IN ('high', 'low')),
      PRIMARY KEY (rule_set_version, rule_id)
    ) STRICT;

CREATE TRIGGER rules_no_update BEFORE UPDATE ON rules BEGIN SELECT RAISE(ABORT, 'regole immutabili (pubblicare una nuova versione): rules non si modifica'); END;

CREATE TRIGGER rules_no_delete BEFORE DELETE ON rules BEGIN SELECT RAISE(ABORT, 'regole immutabili (pubblicare una nuova versione): rules non si cancella'); END;

CREATE TABLE classifications (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      transaction_id TEXT NOT NULL REFERENCES transactions(id),
      kind TEXT NOT NULL CHECK (kind IN ('income', 'expense', 'transfer', 'refund', 'fee', 'unknown')),
      category_id TEXT REFERENCES categories(id),
      status TEXT NOT NULL CHECK (status IN ('auto', 'confirmed', 'needs_review')),
      source TEXT NOT NULL CHECK (source IN ('rule', 'user')),
      rule_set_version INTEGER REFERENCES rule_sets(version),
      rule_id TEXT,
      reason TEXT NOT NULL,
      created_at TEXT NOT NULL,
      CHECK (source <> 'rule' OR rule_set_version IS NOT NULL),
      CHECK (source <> 'user' OR status = 'confirmed')
    ) STRICT;

CREATE INDEX classifications_by_txn ON classifications (transaction_id, id);

CREATE TRIGGER classifications_no_update BEFORE UPDATE ON classifications BEGIN SELECT RAISE(ABORT, 'storico delle classificazioni immutabile: classifications non si modifica'); END;

CREATE TRIGGER classifications_no_delete BEFORE DELETE ON classifications BEGIN SELECT RAISE(ABORT, 'storico delle classificazioni immutabile: classifications non si cancella'); END;

CREATE VIEW current_classifications AS
      SELECT c.* FROM classifications c
      JOIN (SELECT transaction_id, MAX(id) AS id FROM classifications GROUP BY transaction_id) last
        ON last.id = c.id;

CREATE TABLE transaction_links (
      id TEXT PRIMARY KEY,
      type TEXT NOT NULL CHECK (type IN ('transfer', 'refund', 'pending_booked', 'possible_duplicate')),
      from_txn_id TEXT NOT NULL REFERENCES transactions(id),
      to_txn_id TEXT NOT NULL REFERENCES transactions(id),
      status TEXT NOT NULL CHECK (status IN ('proposed', 'confirmed', 'rejected')),
      origin TEXT NOT NULL CHECK (origin IN ('detector', 'user')),
      note TEXT,
      created_at TEXT NOT NULL,
      decided_at TEXT,
      CHECK (from_txn_id <> to_txn_id),
      UNIQUE (type, from_txn_id, to_txn_id)
    ) STRICT;

CREATE INDEX links_by_to ON transaction_links (to_txn_id);

CREATE TRIGGER transaction_links_decide_once BEFORE UPDATE ON transaction_links
      WHEN OLD.status <> 'proposed' OR NEW.status = 'proposed' OR NEW.id IS NOT OLD.id OR NEW.type IS NOT OLD.type
        OR NEW.from_txn_id IS NOT OLD.from_txn_id OR NEW.to_txn_id IS NOT OLD.to_txn_id
        OR NEW.origin IS NOT OLD.origin OR NEW.created_at IS NOT OLD.created_at
      BEGIN SELECT RAISE(ABORT, 'un collegamento si decide una sola volta'); END;

CREATE TRIGGER transaction_links_no_delete BEFORE DELETE ON transaction_links
      BEGIN SELECT RAISE(ABORT, 'nessuna cancellazione dei collegamenti'); END;

CREATE TABLE review_items (
      id TEXT PRIMARY KEY,
      kind TEXT NOT NULL,
      entity TEXT NOT NULL,
      entity_id TEXT NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('open', 'resolved', 'dismissed')),
      detail_json TEXT NOT NULL,
      created_at TEXT NOT NULL,
      closed_at TEXT,
      resolution TEXT,
      CHECK ((status = 'open') = (closed_at IS NULL))
    ) STRICT;

CREATE UNIQUE INDEX review_items_one_open ON review_items (kind, entity, entity_id) WHERE status = 'open';

CREATE TRIGGER review_items_close_once BEFORE UPDATE ON review_items
      WHEN OLD.status <> 'open' OR NEW.id IS NOT OLD.id OR NEW.kind IS NOT OLD.kind OR NEW.entity IS NOT OLD.entity
        OR NEW.entity_id IS NOT OLD.entity_id OR NEW.detail_json IS NOT OLD.detail_json
        OR NEW.created_at IS NOT OLD.created_at
      BEGIN SELECT RAISE(ABORT, 'un elemento da verificare si chiude una sola volta'); END;

CREATE TRIGGER review_items_no_delete BEFORE DELETE ON review_items
      BEGIN SELECT RAISE(ABORT, 'nessuna cancellazione della coda di verifica'); END;

CREATE TABLE reconciliations (
      id TEXT PRIMARY KEY,
      account_id TEXT NOT NULL REFERENCES accounts(id),
      from_date TEXT NOT NULL,
      to_date TEXT NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('ok', 'difference', 'insufficient_data', 'conflict')),
      opening_minor INTEGER,
      closing_minor INTEGER,
      transactions_sum_minor INTEGER NOT NULL,
      difference_minor INTEGER,
      detail_json TEXT NOT NULL,
      computed_at TEXT NOT NULL
    ) STRICT;

CREATE TRIGGER reconciliations_no_update BEFORE UPDATE ON reconciliations BEGIN SELECT RAISE(ABORT, 'storico delle riconciliazioni immutabile: reconciliations non si modifica'); END;

CREATE TRIGGER reconciliations_no_delete BEFORE DELETE ON reconciliations BEGIN SELECT RAISE(ABORT, 'storico delle riconciliazioni immutabile: reconciliations non si cancella'); END;

INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (2, 'classification_review', '384b6da6dd2d71635aa9f1f55ee2ff940941390c18d9b1cfc9aa555e26b20dcd', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
