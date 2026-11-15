// GENERATO da `npm run migrations:generate` (scripts/migrations.ts). NON MODIFICARE A MANO.
// Schema atteso dopo tutte le migrazioni, letto da sqlite_master su SQLite con spazi normalizzati.
import type { SchemaSnapshot } from "../schema-check.ts";

export const SCHEMA_SNAPSHOT: SchemaSnapshot = {
  "version": 8,
  "objects": [
    {
      "type": "index",
      "name": "account_profiles_by_account",
      "tbl_name": "account_profiles",
      "sql": "CREATE INDEX account_profiles_by_account ON account_profiles (account_id, created_at)"
    },
    {
      "type": "index",
      "name": "account_status_events_by_account",
      "tbl_name": "account_status_events",
      "sql": "CREATE INDEX account_status_events_by_account ON account_status_events (account_id, created_at)"
    },
    {
      "type": "index",
      "name": "audit_by_entity",
      "tbl_name": "audit_log",
      "sql": "CREATE INDEX audit_by_entity ON audit_log (entity, entity_id)"
    },
    {
      "type": "index",
      "name": "budgets_by_scope",
      "tbl_name": "budgets",
      "sql": "CREATE INDEX budgets_by_scope ON budgets (scope, target, currency, valid_from, created_at)"
    },
    {
      "type": "index",
      "name": "classifications_by_txn",
      "tbl_name": "classifications",
      "sql": "CREATE INDEX classifications_by_txn ON classifications (transaction_id, id)"
    },
    {
      "type": "index",
      "name": "import_runs_by_file",
      "tbl_name": "import_runs",
      "sql": "CREATE INDEX import_runs_by_file ON import_runs (account_id, parser_id, file_sha256, status)"
    },
    {
      "type": "index",
      "name": "links_by_to",
      "tbl_name": "transaction_links",
      "sql": "CREATE INDEX links_by_to ON transaction_links (to_txn_id)"
    },
    {
      "type": "index",
      "name": "obligation_events_by_obligation",
      "tbl_name": "obligation_events",
      "sql": "CREATE INDEX obligation_events_by_obligation ON obligation_events (obligation_id, occurred_on)"
    },
    {
      "type": "index",
      "name": "obligation_events_by_transaction",
      "tbl_name": "obligation_events",
      "sql": "CREATE INDEX obligation_events_by_transaction ON obligation_events (transaction_id)\n      WHERE transaction_id IS NOT NULL"
    },
    {
      "type": "index",
      "name": "obligation_events_one_open",
      "tbl_name": "obligation_events",
      "sql": "CREATE UNIQUE INDEX obligation_events_one_open ON obligation_events (obligation_id) WHERE kind = 'open'"
    },
    {
      "type": "index",
      "name": "review_items_one_open",
      "tbl_name": "review_items",
      "sql": "CREATE UNIQUE INDEX review_items_one_open ON review_items (kind, entity, entity_id) WHERE status = 'open'"
    },
    {
      "type": "index",
      "name": "transactions_by_date",
      "tbl_name": "transactions",
      "sql": "CREATE INDEX transactions_by_date ON transactions (account_id, transaction_date)"
    },
    {
      "type": "index",
      "name": "transactions_by_fingerprint",
      "tbl_name": "transactions",
      "sql": "CREATE UNIQUE INDEX transactions_by_fingerprint ON transactions (account_id, fingerprint, occurrence)\n      WHERE source_txn_id IS NULL"
    },
    {
      "type": "index",
      "name": "transactions_by_source_id",
      "tbl_name": "transactions",
      "sql": "CREATE UNIQUE INDEX transactions_by_source_id ON transactions (account_id, source_txn_id)\n      WHERE source_txn_id IS NOT NULL"
    },
    {
      "type": "table",
      "name": "account_profiles",
      "tbl_name": "account_profiles",
      "sql": "CREATE TABLE account_profiles (\n      id TEXT PRIMARY KEY,\n      account_id TEXT NOT NULL REFERENCES accounts(id),\n      formal_holder TEXT NOT NULL CHECK (formal_holder IN ('self', 'parent', 'self_with_parent', 'other', 'unknown')),\n      primary_user TEXT NOT NULL CHECK (primary_user IN ('self', 'parent', 'shared', 'unknown')),\n      supervision TEXT NOT NULL CHECK (supervision IN ('parent', 'none', 'unknown')),\n      note TEXT,\n      created_at TEXT NOT NULL\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "account_status_events",
      "tbl_name": "account_status_events",
      "sql": "CREATE TABLE account_status_events (\n      id TEXT PRIMARY KEY,\n      account_id TEXT NOT NULL REFERENCES accounts(id),\n      status TEXT NOT NULL CHECK (status IN ('closed', 'reopened')),\n      effective_date TEXT,\n      effective_date_status TEXT NOT NULL CHECK (effective_date_status IN ('verified', 'declared', 'unknown')),\n      last_movement_on TEXT,\n      reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),\n      created_at TEXT NOT NULL,\n      CHECK ((effective_date IS NULL) = (effective_date_status = 'unknown')),\n      CHECK (effective_date IS NULL OR last_movement_on IS NULL OR effective_date >= last_movement_on)\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "accounts",
      "tbl_name": "accounts",
      "sql": "CREATE TABLE accounts (\n      id TEXT PRIMARY KEY,\n      institution_id TEXT REFERENCES institutions(id),\n      name TEXT NOT NULL,\n      type TEXT NOT NULL CHECK (type IN ('current', 'savings', 'card', 'cash', 'investment', 'other')),\n      currency TEXT NOT NULL CHECK (length(currency) = 3 AND currency = upper(currency)),\n      holder_role TEXT NOT NULL CHECK (holder_role IN ('owner', 'joint', 'delegate', 'other')),\n      created_at TEXT NOT NULL,\n      closed_at TEXT\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "audit_log",
      "tbl_name": "audit_log",
      "sql": "CREATE TABLE audit_log (\n      id INTEGER PRIMARY KEY AUTOINCREMENT,\n      at TEXT NOT NULL,\n      actor TEXT NOT NULL CHECK (actor IN ('system', 'import', 'rule', 'user')),\n      action TEXT NOT NULL,\n      entity TEXT NOT NULL,\n      entity_id TEXT NOT NULL,\n      before_json TEXT,\n      after_json TEXT,\n      reason TEXT,\n      import_run_id TEXT\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "balance_observations",
      "tbl_name": "balance_observations",
      "sql": "CREATE TABLE balance_observations (\n      id TEXT PRIMARY KEY,\n      account_id TEXT NOT NULL REFERENCES accounts(id),\n      as_of_date TEXT NOT NULL,\n      balance_minor INTEGER NOT NULL,\n      currency TEXT NOT NULL,\n      kind TEXT NOT NULL CHECK (kind IN ('end_of_day')),\n      source_id TEXT NOT NULL REFERENCES sources(id),\n      import_run_id TEXT REFERENCES import_runs(id),\n      raw_record_id TEXT REFERENCES raw_records(id),\n      note TEXT,\n      created_at TEXT NOT NULL,\n      UNIQUE (account_id, as_of_date, kind, source_id, balance_minor)\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "budgets",
      "tbl_name": "budgets",
      "sql": "CREATE TABLE budgets (\n      id TEXT PRIMARY KEY,\n      scope TEXT NOT NULL CHECK (scope IN ('category', 'budget_group', 'total')),\n      target TEXT,\n      valid_from TEXT NOT NULL CHECK (valid_from GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]'),\n      amount_minor INTEGER CHECK (amount_minor IS NULL OR amount_minor > 0),\n      currency TEXT NOT NULL,\n      note TEXT,\n      created_at TEXT NOT NULL,\n      CHECK ((scope = 'total') = (target IS NULL)),\n      CHECK (scope <> 'budget_group' OR target IN ('needs', 'wants', 'savings'))\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "categories",
      "tbl_name": "categories",
      "sql": "CREATE TABLE categories (\n      id TEXT PRIMARY KEY,\n      name TEXT NOT NULL,\n      parent_id TEXT REFERENCES categories(id),\n      budget_group TEXT CHECK (budget_group IS NULL OR budget_group IN ('needs', 'wants', 'savings')),\n      created_at TEXT NOT NULL\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "classifications",
      "tbl_name": "classifications",
      "sql": "CREATE TABLE classifications (\n      id INTEGER PRIMARY KEY AUTOINCREMENT,\n      transaction_id TEXT NOT NULL REFERENCES transactions(id),\n      kind TEXT NOT NULL CHECK (kind IN ('income', 'expense', 'transfer', 'refund', 'fee', 'unknown')),\n      category_id TEXT REFERENCES categories(id),\n      status TEXT NOT NULL CHECK (status IN ('auto', 'confirmed', 'needs_review')),\n      source TEXT NOT NULL CHECK (source IN ('rule', 'user')),\n      rule_set_version INTEGER REFERENCES rule_sets(version),\n      rule_id TEXT,\n      reason TEXT NOT NULL,\n      created_at TEXT NOT NULL,\n      CHECK (source <> 'rule' OR rule_set_version IS NOT NULL),\n      CHECK (source <> 'user' OR status = 'confirmed')\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "counterparties",
      "tbl_name": "counterparties",
      "sql": "CREATE TABLE counterparties (\n      id TEXT PRIMARY KEY,\n      name TEXT NOT NULL,\n      note TEXT,\n      created_at TEXT NOT NULL\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "excluded_rows",
      "tbl_name": "excluded_rows",
      "sql": "CREATE TABLE excluded_rows (\n      id TEXT PRIMARY KEY,\n      import_run_id TEXT NOT NULL REFERENCES import_runs(id),\n      line_number INTEGER NOT NULL,\n      reason TEXT NOT NULL,\n      source_txn_id TEXT,\n      content_sha256 TEXT NOT NULL,\n      raw_json TEXT NOT NULL,\n      UNIQUE (import_run_id, line_number)\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "fx_rates",
      "tbl_name": "fx_rates",
      "sql": "CREATE TABLE fx_rates (\n      rate_date TEXT NOT NULL,\n      base TEXT NOT NULL,\n      quote TEXT NOT NULL,\n      rate TEXT NOT NULL,\n      source TEXT NOT NULL,\n      created_at TEXT NOT NULL,\n      PRIMARY KEY (rate_date, base, quote, source),\n      CHECK (base <> quote)\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "goal_allocations",
      "tbl_name": "goal_allocations",
      "sql": "CREATE TABLE goal_allocations (\n      id TEXT PRIMARY KEY,\n      goal_id TEXT NOT NULL REFERENCES savings_goals(id),\n      amount_minor INTEGER NOT NULL CHECK (amount_minor <> 0),\n      currency TEXT NOT NULL,\n      allocated_on TEXT NOT NULL,\n      note TEXT,\n      created_at TEXT NOT NULL\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "import_runs",
      "tbl_name": "import_runs",
      "sql": "CREATE TABLE import_runs (\n      id TEXT PRIMARY KEY,\n      source_id TEXT NOT NULL REFERENCES sources(id),\n      account_id TEXT NOT NULL REFERENCES accounts(id),\n      parser_id TEXT NOT NULL,\n      parser_version INTEGER NOT NULL,\n      file_name TEXT,\n      file_sha256 TEXT NOT NULL,\n      status TEXT NOT NULL CHECK (status IN ('committed', 'rejected', 'duplicate_file')),\n      started_at TEXT NOT NULL,\n      finished_at TEXT NOT NULL,\n      counts_json TEXT NOT NULL,\n      errors_json TEXT NOT NULL\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "institutions",
      "tbl_name": "institutions",
      "sql": "CREATE TABLE institutions (\n      id TEXT PRIMARY KEY,\n      name TEXT NOT NULL,\n      created_at TEXT NOT NULL\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "link_reversals",
      "tbl_name": "link_reversals",
      "sql": "CREATE TABLE link_reversals (\n      link_id TEXT PRIMARY KEY REFERENCES transaction_links(id),\n      reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),\n      created_at TEXT NOT NULL\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "manual_voids",
      "tbl_name": "manual_voids",
      "sql": "CREATE TABLE manual_voids (\n      transaction_id TEXT PRIMARY KEY REFERENCES transactions(id),\n      reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),\n      created_at TEXT NOT NULL\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "meta",
      "tbl_name": "meta",
      "sql": "CREATE TABLE meta (\n      key TEXT PRIMARY KEY,\n      value TEXT NOT NULL\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "obligation_events",
      "tbl_name": "obligation_events",
      "sql": "CREATE TABLE obligation_events (\n      id TEXT PRIMARY KEY,\n      obligation_id TEXT NOT NULL REFERENCES obligations(id),\n      kind TEXT NOT NULL CHECK (kind IN ('open', 'increase', 'payment', 'forgive', 'adjust')),\n      amount_minor INTEGER NOT NULL,\n      occurred_on TEXT NOT NULL,\n      transaction_id TEXT REFERENCES transactions(id),\n      note TEXT,\n      created_at TEXT NOT NULL,\n      CHECK (kind = 'adjust' OR amount_minor > 0),\n      CHECK (kind <> 'adjust' OR amount_minor <> 0),\n      CHECK (kind <> 'adjust' OR length(trim(coalesce(note, ''))) > 0),\n      CHECK (transaction_id IS NULL OR kind IN ('open', 'increase', 'payment'))\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "obligations",
      "tbl_name": "obligations",
      "sql": "CREATE TABLE obligations (\n      id TEXT PRIMARY KEY,\n      counterparty_id TEXT NOT NULL REFERENCES counterparties(id),\n      direction TEXT NOT NULL CHECK (direction IN ('receivable', 'payable')),\n      currency TEXT NOT NULL,\n      description TEXT NOT NULL,\n      opened_on TEXT NOT NULL,\n      due_on TEXT,\n      created_at TEXT NOT NULL,\n      CHECK (due_on IS NULL OR due_on >= opened_on)\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "planned_purchases",
      "tbl_name": "planned_purchases",
      "sql": "CREATE TABLE planned_purchases (\n      id TEXT PRIMARY KEY,\n      title TEXT NOT NULL,\n      estimated_minor INTEGER CHECK (estimated_minor IS NULL OR estimated_minor > 0),\n      currency TEXT NOT NULL,\n      priority INTEGER CHECK (priority IS NULL OR priority BETWEEN 1 AND 5),\n      status TEXT NOT NULL CHECK (status IN ('idea', 'planned', 'purchased', 'cancelled')),\n      goal_id TEXT REFERENCES savings_goals(id),\n      notion_block_id TEXT UNIQUE,\n      transaction_id TEXT REFERENCES transactions(id),\n      created_at TEXT NOT NULL,\n      updated_at TEXT NOT NULL,\n      CHECK (transaction_id IS NULL OR status = 'purchased')\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "raw_records",
      "tbl_name": "raw_records",
      "sql": "CREATE TABLE raw_records (\n      id TEXT PRIMARY KEY,\n      import_run_id TEXT NOT NULL REFERENCES import_runs(id),\n      line_number INTEGER NOT NULL,\n      record_type TEXT NOT NULL CHECK (record_type IN ('transaction', 'balance')),\n      content_sha256 TEXT NOT NULL,\n      raw_json TEXT NOT NULL,\n      UNIQUE (import_run_id, line_number)\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "reconciliations",
      "tbl_name": "reconciliations",
      "sql": "CREATE TABLE reconciliations (\n      id TEXT PRIMARY KEY,\n      account_id TEXT NOT NULL REFERENCES accounts(id),\n      from_date TEXT NOT NULL,\n      to_date TEXT NOT NULL,\n      status TEXT NOT NULL CHECK (status IN ('ok', 'difference', 'insufficient_data', 'conflict')),\n      opening_minor INTEGER,\n      closing_minor INTEGER,\n      transactions_sum_minor INTEGER NOT NULL,\n      difference_minor INTEGER,\n      detail_json TEXT NOT NULL,\n      computed_at TEXT NOT NULL\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "review_items",
      "tbl_name": "review_items",
      "sql": "CREATE TABLE review_items (\n      id TEXT PRIMARY KEY,\n      kind TEXT NOT NULL,\n      entity TEXT NOT NULL,\n      entity_id TEXT NOT NULL,\n      status TEXT NOT NULL CHECK (status IN ('open', 'resolved', 'dismissed')),\n      detail_json TEXT NOT NULL,\n      created_at TEXT NOT NULL,\n      closed_at TEXT,\n      resolution TEXT,\n      CHECK ((status = 'open') = (closed_at IS NULL))\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "rule_sets",
      "tbl_name": "rule_sets",
      "sql": "CREATE TABLE rule_sets (\n      version INTEGER PRIMARY KEY,\n      checksum TEXT NOT NULL UNIQUE,\n      note TEXT,\n      created_at TEXT NOT NULL\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "rules",
      "tbl_name": "rules",
      "sql": "CREATE TABLE rules (\n      rule_set_version INTEGER NOT NULL REFERENCES rule_sets(version),\n      rule_id TEXT NOT NULL,\n      priority INTEGER NOT NULL,\n      description TEXT NOT NULL,\n      conditions_json TEXT NOT NULL,\n      kind TEXT NOT NULL CHECK (kind IN ('income', 'expense', 'transfer', 'refund', 'fee')),\n      category_id TEXT REFERENCES categories(id),\n      confidence TEXT NOT NULL CHECK (confidence IN ('high', 'low')),\n      PRIMARY KEY (rule_set_version, rule_id)\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "savings_goals",
      "tbl_name": "savings_goals",
      "sql": "CREATE TABLE savings_goals (\n      id TEXT PRIMARY KEY,\n      name TEXT NOT NULL,\n      target_minor INTEGER NOT NULL CHECK (target_minor > 0),\n      currency TEXT NOT NULL,\n      deadline TEXT,\n      status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'reached', 'cancelled')),\n      created_at TEXT NOT NULL,\n      updated_at TEXT NOT NULL\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "schema_migrations",
      "tbl_name": "schema_migrations",
      "sql": "CREATE TABLE schema_migrations (\n      version INTEGER PRIMARY KEY,\n      name TEXT NOT NULL,\n      checksum TEXT NOT NULL,\n      applied_at TEXT NOT NULL\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "sources",
      "tbl_name": "sources",
      "sql": "CREATE TABLE sources (\n      id TEXT PRIMARY KEY,\n      kind TEXT NOT NULL CHECK (kind IN ('file', 'api', 'manual')),\n      name TEXT NOT NULL,\n      created_at TEXT NOT NULL\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "transaction_links",
      "tbl_name": "transaction_links",
      "sql": "CREATE TABLE transaction_links (\n      id TEXT PRIMARY KEY,\n      type TEXT NOT NULL CHECK (type IN ('transfer', 'refund', 'pending_booked', 'possible_duplicate')),\n      from_txn_id TEXT NOT NULL REFERENCES transactions(id),\n      to_txn_id TEXT NOT NULL REFERENCES transactions(id),\n      status TEXT NOT NULL CHECK (status IN ('proposed', 'confirmed', 'rejected')),\n      origin TEXT NOT NULL CHECK (origin IN ('detector', 'user')),\n      note TEXT,\n      created_at TEXT NOT NULL,\n      decided_at TEXT,\n      CHECK (from_txn_id <> to_txn_id),\n      UNIQUE (type, from_txn_id, to_txn_id)\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "transaction_observations",
      "tbl_name": "transaction_observations",
      "sql": "CREATE TABLE transaction_observations (\n      transaction_id TEXT NOT NULL REFERENCES transactions(id),\n      import_run_id TEXT NOT NULL REFERENCES import_runs(id),\n      raw_record_id TEXT NOT NULL REFERENCES raw_records(id),\n      outcome TEXT NOT NULL CHECK (outcome IN ('created', 'unchanged', 'updated', 'conflict')),\n      PRIMARY KEY (transaction_id, raw_record_id)\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "transactions",
      "tbl_name": "transactions",
      "sql": "CREATE TABLE transactions (\n      id TEXT PRIMARY KEY,\n      account_id TEXT NOT NULL REFERENCES accounts(id),\n      origin TEXT NOT NULL CHECK (origin IN ('import', 'manual')),\n      source_txn_id TEXT,\n      fingerprint TEXT NOT NULL,\n      occurrence INTEGER,\n      status TEXT NOT NULL CHECK (status IN ('pending', 'booked')),\n      transaction_date TEXT NOT NULL,\n      booking_date TEXT,\n      amount_minor INTEGER NOT NULL,\n      currency TEXT NOT NULL,\n      original_amount_minor INTEGER,\n      original_currency TEXT,\n      description TEXT NOT NULL,\n      counterparty TEXT,\n      description_norm TEXT NOT NULL,\n      counterparty_norm TEXT,\n      first_import_run_id TEXT REFERENCES import_runs(id),\n      last_import_run_id TEXT REFERENCES import_runs(id),\n      created_at TEXT NOT NULL,\n      updated_at TEXT NOT NULL, source_type TEXT,\n      CHECK ((source_txn_id IS NULL) = (occurrence IS NOT NULL)),\n      CHECK (occurrence IS NULL OR occurrence >= 1),\n      CHECK ((original_amount_minor IS NULL) = (original_currency IS NULL)),\n      CHECK (status = 'pending' OR booking_date IS NOT NULL)\n    ) STRICT"
    },
    {
      "type": "table",
      "name": "transactions_search",
      "tbl_name": "transactions_search",
      "sql": "CREATE VIRTUAL TABLE transactions_search USING fts5(\n      description, counterparty, note,\n      content = 'transactions_search_source', content_rowid = 'search_rowid',\n      tokenize = 'unicode61 remove_diacritics 2'\n    )"
    },
    {
      "type": "table",
      "name": "transactions_search_config",
      "tbl_name": "transactions_search_config",
      "sql": "CREATE TABLE 'transactions_search_config'(k PRIMARY KEY, v) WITHOUT ROWID"
    },
    {
      "type": "table",
      "name": "transactions_search_data",
      "tbl_name": "transactions_search_data",
      "sql": "CREATE TABLE 'transactions_search_data'(id INTEGER PRIMARY KEY, block BLOB)"
    },
    {
      "type": "table",
      "name": "transactions_search_docsize",
      "tbl_name": "transactions_search_docsize",
      "sql": "CREATE TABLE 'transactions_search_docsize'(id INTEGER PRIMARY KEY, sz BLOB)"
    },
    {
      "type": "table",
      "name": "transactions_search_idx",
      "tbl_name": "transactions_search_idx",
      "sql": "CREATE TABLE 'transactions_search_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID"
    },
    {
      "type": "table",
      "name": "write_guard",
      "tbl_name": "write_guard",
      "sql": "CREATE TABLE write_guard (\n      expected_seq INTEGER NOT NULL,\n      actual_seq INTEGER NOT NULL,\n      CONSTRAINT write_guard_seq CHECK (expected_seq = actual_seq)\n    ) STRICT"
    },
    {
      "type": "trigger",
      "name": "account_profiles_no_delete",
      "tbl_name": "account_profiles",
      "sql": "CREATE TRIGGER account_profiles_no_delete BEFORE DELETE ON account_profiles BEGIN SELECT RAISE(ABORT, 'profili dei conti immutabili (dichiararne uno nuovo): account_profiles non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "account_profiles_no_update",
      "tbl_name": "account_profiles",
      "sql": "CREATE TRIGGER account_profiles_no_update BEFORE UPDATE ON account_profiles BEGIN SELECT RAISE(ABORT, 'profili dei conti immutabili (dichiararne uno nuovo): account_profiles non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "account_status_events_no_delete",
      "tbl_name": "account_status_events",
      "sql": "CREATE TRIGGER account_status_events_no_delete BEFORE DELETE ON account_status_events BEGIN SELECT RAISE(ABORT, 'stato dei conti immutabile (registrare un nuovo evento): account_status_events non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "account_status_events_no_update",
      "tbl_name": "account_status_events",
      "sql": "CREATE TRIGGER account_status_events_no_update BEFORE UPDATE ON account_status_events BEGIN SELECT RAISE(ABORT, 'stato dei conti immutabile (registrare un nuovo evento): account_status_events non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "audit_log_no_delete",
      "tbl_name": "audit_log",
      "sql": "CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit immutabile: audit_log non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "audit_log_no_update",
      "tbl_name": "audit_log",
      "sql": "CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit immutabile: audit_log non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "balance_observations_no_delete",
      "tbl_name": "balance_observations",
      "sql": "CREATE TRIGGER balance_observations_no_delete BEFORE DELETE ON balance_observations BEGIN SELECT RAISE(ABORT, 'osservazioni dei saldi immutabili: balance_observations non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "balance_observations_no_update",
      "tbl_name": "balance_observations",
      "sql": "CREATE TRIGGER balance_observations_no_update BEFORE UPDATE ON balance_observations BEGIN SELECT RAISE(ABORT, 'osservazioni dei saldi immutabili: balance_observations non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "budgets_no_delete",
      "tbl_name": "budgets",
      "sql": "CREATE TRIGGER budgets_no_delete BEFORE DELETE ON budgets BEGIN SELECT RAISE(ABORT, 'budget immutabili (registrarne una nuova versione): budgets non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "budgets_no_update",
      "tbl_name": "budgets",
      "sql": "CREATE TRIGGER budgets_no_update BEFORE UPDATE ON budgets BEGIN SELECT RAISE(ABORT, 'budget immutabili (registrarne una nuova versione): budgets non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "categories_no_delete",
      "tbl_name": "categories",
      "sql": "CREATE TRIGGER categories_no_delete BEFORE DELETE ON categories BEGIN SELECT RAISE(ABORT, 'categorie immutabili (crearne una nuova per cambiare): categories non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "categories_no_update",
      "tbl_name": "categories",
      "sql": "CREATE TRIGGER categories_no_update BEFORE UPDATE ON categories BEGIN SELECT RAISE(ABORT, 'categorie immutabili (crearne una nuova per cambiare): categories non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "classifications_no_delete",
      "tbl_name": "classifications",
      "sql": "CREATE TRIGGER classifications_no_delete BEFORE DELETE ON classifications BEGIN SELECT RAISE(ABORT, 'storico delle classificazioni immutabile: classifications non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "classifications_no_update",
      "tbl_name": "classifications",
      "sql": "CREATE TRIGGER classifications_no_update BEFORE UPDATE ON classifications BEGIN SELECT RAISE(ABORT, 'storico delle classificazioni immutabile: classifications non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "counterparties_no_delete",
      "tbl_name": "counterparties",
      "sql": "CREATE TRIGGER counterparties_no_delete BEFORE DELETE ON counterparties BEGIN SELECT RAISE(ABORT, 'controparti immutabili (crearne una nuova per cambiare): counterparties non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "counterparties_no_update",
      "tbl_name": "counterparties",
      "sql": "CREATE TRIGGER counterparties_no_update BEFORE UPDATE ON counterparties BEGIN SELECT RAISE(ABORT, 'controparti immutabili (crearne una nuova per cambiare): counterparties non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "excluded_rows_no_delete",
      "tbl_name": "excluded_rows",
      "sql": "CREATE TRIGGER excluded_rows_no_delete BEFORE DELETE ON excluded_rows BEGIN SELECT RAISE(ABORT, 'righe escluse immutabili: excluded_rows non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "excluded_rows_no_update",
      "tbl_name": "excluded_rows",
      "sql": "CREATE TRIGGER excluded_rows_no_update BEFORE UPDATE ON excluded_rows BEGIN SELECT RAISE(ABORT, 'righe escluse immutabili: excluded_rows non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "fx_rates_no_delete",
      "tbl_name": "fx_rates",
      "sql": "CREATE TRIGGER fx_rates_no_delete BEFORE DELETE ON fx_rates BEGIN SELECT RAISE(ABORT, 'cambi osservati immutabili: fx_rates non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "fx_rates_no_update",
      "tbl_name": "fx_rates",
      "sql": "CREATE TRIGGER fx_rates_no_update BEFORE UPDATE ON fx_rates BEGIN SELECT RAISE(ABORT, 'cambi osservati immutabili: fx_rates non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "goal_allocations_no_delete",
      "tbl_name": "goal_allocations",
      "sql": "CREATE TRIGGER goal_allocations_no_delete BEFORE DELETE ON goal_allocations BEGIN SELECT RAISE(ABORT, 'accantonamenti immutabili (correggere con un movimento opposto): goal_allocations non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "goal_allocations_no_update",
      "tbl_name": "goal_allocations",
      "sql": "CREATE TRIGGER goal_allocations_no_update BEFORE UPDATE ON goal_allocations BEGIN SELECT RAISE(ABORT, 'accantonamenti immutabili (correggere con un movimento opposto): goal_allocations non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "import_runs_no_delete",
      "tbl_name": "import_runs",
      "sql": "CREATE TRIGGER import_runs_no_delete BEFORE DELETE ON import_runs BEGIN SELECT RAISE(ABORT, 'storico delle importazioni immutabile: import_runs non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "import_runs_no_update",
      "tbl_name": "import_runs",
      "sql": "CREATE TRIGGER import_runs_no_update BEFORE UPDATE ON import_runs BEGIN SELECT RAISE(ABORT, 'storico delle importazioni immutabile: import_runs non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "link_reversals_no_delete",
      "tbl_name": "link_reversals",
      "sql": "CREATE TRIGGER link_reversals_no_delete BEFORE DELETE ON link_reversals BEGIN SELECT RAISE(ABORT, 'revoche immutabili: link_reversals non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "link_reversals_no_update",
      "tbl_name": "link_reversals",
      "sql": "CREATE TRIGGER link_reversals_no_update BEFORE UPDATE ON link_reversals BEGIN SELECT RAISE(ABORT, 'revoche immutabili: link_reversals non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "manual_voids_no_delete",
      "tbl_name": "manual_voids",
      "sql": "CREATE TRIGGER manual_voids_no_delete BEFORE DELETE ON manual_voids BEGIN SELECT RAISE(ABORT, 'annullamenti immutabili: manual_voids non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "manual_voids_no_update",
      "tbl_name": "manual_voids",
      "sql": "CREATE TRIGGER manual_voids_no_update BEFORE UPDATE ON manual_voids BEGIN SELECT RAISE(ABORT, 'annullamenti immutabili: manual_voids non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "obligation_events_no_delete",
      "tbl_name": "obligation_events",
      "sql": "CREATE TRIGGER obligation_events_no_delete BEFORE DELETE ON obligation_events BEGIN SELECT RAISE(ABORT, 'eventi di crediti e debiti immutabili (correggere con una rettifica): obligation_events non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "obligation_events_no_update",
      "tbl_name": "obligation_events",
      "sql": "CREATE TRIGGER obligation_events_no_update BEFORE UPDATE ON obligation_events BEGIN SELECT RAISE(ABORT, 'eventi di crediti e debiti immutabili (correggere con una rettifica): obligation_events non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "obligations_no_delete",
      "tbl_name": "obligations",
      "sql": "CREATE TRIGGER obligations_no_delete BEFORE DELETE ON obligations BEGIN SELECT RAISE(ABORT, 'crediti e debiti immutabili (si cambiano solo con eventi): obligations non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "obligations_no_update",
      "tbl_name": "obligations",
      "sql": "CREATE TRIGGER obligations_no_update BEFORE UPDATE ON obligations BEGIN SELECT RAISE(ABORT, 'crediti e debiti immutabili (si cambiano solo con eventi): obligations non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "planned_purchases_no_delete",
      "tbl_name": "planned_purchases",
      "sql": "CREATE TRIGGER planned_purchases_no_delete BEFORE DELETE ON planned_purchases\n      BEGIN SELECT RAISE(ABORT, 'nessuna cancellazione degli acquisti pianificati (usare lo stato cancelled)'); END"
    },
    {
      "type": "trigger",
      "name": "raw_records_no_delete",
      "tbl_name": "raw_records",
      "sql": "CREATE TRIGGER raw_records_no_delete BEFORE DELETE ON raw_records BEGIN SELECT RAISE(ABORT, 'storico grezzo immutabile: raw_records non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "raw_records_no_update",
      "tbl_name": "raw_records",
      "sql": "CREATE TRIGGER raw_records_no_update BEFORE UPDATE ON raw_records BEGIN SELECT RAISE(ABORT, 'storico grezzo immutabile: raw_records non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "reconciliations_no_delete",
      "tbl_name": "reconciliations",
      "sql": "CREATE TRIGGER reconciliations_no_delete BEFORE DELETE ON reconciliations BEGIN SELECT RAISE(ABORT, 'storico delle riconciliazioni immutabile: reconciliations non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "reconciliations_no_update",
      "tbl_name": "reconciliations",
      "sql": "CREATE TRIGGER reconciliations_no_update BEFORE UPDATE ON reconciliations BEGIN SELECT RAISE(ABORT, 'storico delle riconciliazioni immutabile: reconciliations non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "review_items_close_once",
      "tbl_name": "review_items",
      "sql": "CREATE TRIGGER review_items_close_once BEFORE UPDATE ON review_items\n      WHEN OLD.status <> 'open' OR NEW.id IS NOT OLD.id OR NEW.kind IS NOT OLD.kind OR NEW.entity IS NOT OLD.entity\n        OR NEW.entity_id IS NOT OLD.entity_id OR NEW.detail_json IS NOT OLD.detail_json\n        OR NEW.created_at IS NOT OLD.created_at\n      BEGIN SELECT RAISE(ABORT, 'un elemento da verificare si chiude una sola volta'); END"
    },
    {
      "type": "trigger",
      "name": "review_items_no_delete",
      "tbl_name": "review_items",
      "sql": "CREATE TRIGGER review_items_no_delete BEFORE DELETE ON review_items\n      BEGIN SELECT RAISE(ABORT, 'nessuna cancellazione della coda di verifica'); END"
    },
    {
      "type": "trigger",
      "name": "rule_sets_no_delete",
      "tbl_name": "rule_sets",
      "sql": "CREATE TRIGGER rule_sets_no_delete BEFORE DELETE ON rule_sets BEGIN SELECT RAISE(ABORT, 'versioni delle regole immutabili: rule_sets non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "rule_sets_no_update",
      "tbl_name": "rule_sets",
      "sql": "CREATE TRIGGER rule_sets_no_update BEFORE UPDATE ON rule_sets BEGIN SELECT RAISE(ABORT, 'versioni delle regole immutabili: rule_sets non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "rules_no_delete",
      "tbl_name": "rules",
      "sql": "CREATE TRIGGER rules_no_delete BEFORE DELETE ON rules BEGIN SELECT RAISE(ABORT, 'regole immutabili (pubblicare una nuova versione): rules non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "rules_no_update",
      "tbl_name": "rules",
      "sql": "CREATE TRIGGER rules_no_update BEFORE UPDATE ON rules BEGIN SELECT RAISE(ABORT, 'regole immutabili (pubblicare una nuova versione): rules non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "savings_goals_no_delete",
      "tbl_name": "savings_goals",
      "sql": "CREATE TRIGGER savings_goals_no_delete BEFORE DELETE ON savings_goals\n      BEGIN SELECT RAISE(ABORT, 'nessuna cancellazione degli obiettivi (usare lo stato cancelled)'); END"
    },
    {
      "type": "trigger",
      "name": "schema_migrations_no_delete",
      "tbl_name": "schema_migrations",
      "sql": "CREATE TRIGGER schema_migrations_no_delete BEFORE DELETE ON schema_migrations BEGIN SELECT RAISE(ABORT, 'storico delle migrazioni immutabile: schema_migrations non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "schema_migrations_no_update",
      "tbl_name": "schema_migrations",
      "sql": "CREATE TRIGGER schema_migrations_no_update BEFORE UPDATE ON schema_migrations BEGIN SELECT RAISE(ABORT, 'storico delle migrazioni immutabile: schema_migrations non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "transaction_links_decide_once",
      "tbl_name": "transaction_links",
      "sql": "CREATE TRIGGER transaction_links_decide_once BEFORE UPDATE ON transaction_links\n      WHEN OLD.status <> 'proposed' OR NEW.status = 'proposed' OR NEW.id IS NOT OLD.id OR NEW.type IS NOT OLD.type\n        OR NEW.from_txn_id IS NOT OLD.from_txn_id OR NEW.to_txn_id IS NOT OLD.to_txn_id\n        OR NEW.origin IS NOT OLD.origin OR NEW.created_at IS NOT OLD.created_at\n      BEGIN SELECT RAISE(ABORT, 'un collegamento si decide una sola volta'); END"
    },
    {
      "type": "trigger",
      "name": "transaction_links_no_delete",
      "tbl_name": "transaction_links",
      "sql": "CREATE TRIGGER transaction_links_no_delete BEFORE DELETE ON transaction_links\n      BEGIN SELECT RAISE(ABORT, 'nessuna cancellazione dei collegamenti'); END"
    },
    {
      "type": "trigger",
      "name": "transaction_observations_no_delete",
      "tbl_name": "transaction_observations",
      "sql": "CREATE TRIGGER transaction_observations_no_delete BEFORE DELETE ON transaction_observations BEGIN SELECT RAISE(ABORT, 'provenienza immutabile: transaction_observations non si cancella'); END"
    },
    {
      "type": "trigger",
      "name": "transaction_observations_no_update",
      "tbl_name": "transaction_observations",
      "sql": "CREATE TRIGGER transaction_observations_no_update BEFORE UPDATE ON transaction_observations BEGIN SELECT RAISE(ABORT, 'provenienza immutabile: transaction_observations non si modifica'); END"
    },
    {
      "type": "trigger",
      "name": "transactions_identity_fixed",
      "tbl_name": "transactions",
      "sql": "CREATE TRIGGER transactions_identity_fixed BEFORE UPDATE ON transactions\n      WHEN NEW.id IS NOT OLD.id OR NEW.account_id IS NOT OLD.account_id OR NEW.origin IS NOT OLD.origin\n        OR NEW.source_txn_id IS NOT OLD.source_txn_id OR NEW.created_at IS NOT OLD.created_at\n        OR NEW.first_import_run_id IS NOT OLD.first_import_run_id\n      BEGIN SELECT RAISE(ABORT, 'identita della transazione immutabile'); END"
    },
    {
      "type": "trigger",
      "name": "transactions_no_booked_to_pending",
      "tbl_name": "transactions",
      "sql": "CREATE TRIGGER transactions_no_booked_to_pending BEFORE UPDATE ON transactions\n      WHEN OLD.status = 'booked' AND NEW.status = 'pending'\n      BEGIN SELECT RAISE(ABORT, 'una transazione contabilizzata non torna in attesa'); END"
    },
    {
      "type": "trigger",
      "name": "transactions_no_delete",
      "tbl_name": "transactions",
      "sql": "CREATE TRIGGER transactions_no_delete BEFORE DELETE ON transactions\n      BEGIN SELECT RAISE(ABORT, 'nessuna cancellazione di transazioni'); END"
    },
    {
      "type": "trigger",
      "name": "transactions_search_after_insert",
      "tbl_name": "transactions",
      "sql": "CREATE TRIGGER transactions_search_after_insert AFTER INSERT ON transactions BEGIN\n      INSERT INTO transactions_search (rowid, description, counterparty, note)\n        VALUES (NEW.rowid, NEW.description, NEW.counterparty, (SELECT a.reason FROM audit_log a WHERE a.entity = 'transaction' AND a.entity_id = NEW.id\n      AND a.action = 'add_manual_transaction' ORDER BY a.id LIMIT 1));\n    END"
    },
    {
      "type": "trigger",
      "name": "transactions_search_after_manual_note",
      "tbl_name": "audit_log",
      "sql": "CREATE TRIGGER transactions_search_after_manual_note AFTER INSERT ON audit_log\n      WHEN NEW.entity = 'transaction' AND NEW.action = 'add_manual_transaction' AND NEW.reason IS NOT NULL\n        AND NOT EXISTS (SELECT 1 FROM audit_log a WHERE a.entity = 'transaction' AND a.entity_id = NEW.entity_id\n          AND a.action = 'add_manual_transaction' AND a.id < NEW.id) BEGIN\n      INSERT INTO transactions_search (transactions_search, rowid, description, counterparty, note)\n        SELECT 'delete', t.rowid, t.description, t.counterparty, NULL FROM transactions t WHERE t.id = NEW.entity_id;\n      INSERT INTO transactions_search (rowid, description, counterparty, note)\n        SELECT t.rowid, t.description, t.counterparty, NEW.reason FROM transactions t WHERE t.id = NEW.entity_id;\n    END"
    },
    {
      "type": "trigger",
      "name": "transactions_search_after_update",
      "tbl_name": "transactions",
      "sql": "CREATE TRIGGER transactions_search_after_update AFTER UPDATE OF description, counterparty ON transactions\n      WHEN NEW.description IS NOT OLD.description OR NEW.counterparty IS NOT OLD.counterparty BEGIN\n      INSERT INTO transactions_search (transactions_search, rowid, description, counterparty, note)\n        VALUES ('delete', OLD.rowid, OLD.description, OLD.counterparty, (SELECT a.reason FROM audit_log a WHERE a.entity = 'transaction' AND a.entity_id = OLD.id\n      AND a.action = 'add_manual_transaction' ORDER BY a.id LIMIT 1));\n      INSERT INTO transactions_search (rowid, description, counterparty, note)\n        VALUES (NEW.rowid, NEW.description, NEW.counterparty, (SELECT a.reason FROM audit_log a WHERE a.entity = 'transaction' AND a.entity_id = NEW.id\n      AND a.action = 'add_manual_transaction' ORDER BY a.id LIMIT 1));\n    END"
    },
    {
      "type": "view",
      "name": "current_classifications",
      "tbl_name": "current_classifications",
      "sql": "CREATE VIEW current_classifications AS\n      SELECT c.* FROM classifications c\n      JOIN (SELECT transaction_id, MAX(id) AS id FROM classifications GROUP BY transaction_id) last\n        ON last.id = c.id"
    },
    {
      "type": "view",
      "name": "transactions_search_source",
      "tbl_name": "transactions_search_source",
      "sql": "CREATE VIEW transactions_search_source AS\n      SELECT t.rowid AS search_rowid, t.description AS description, t.counterparty AS counterparty,\n        (SELECT a.reason FROM audit_log a WHERE a.entity = 'transaction' AND a.entity_id = t.id\n      AND a.action = 'add_manual_transaction' ORDER BY a.id LIMIT 1) AS note\n      FROM transactions t"
    }
  ]
};
