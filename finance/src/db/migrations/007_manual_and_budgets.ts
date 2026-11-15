import type { Migration } from "../migrate.ts";
import { appendOnly } from "./001_core.ts";

/**
 * v7 — ATTIVA (in `MIGRATIONS`, blocco C-07): movimenti inseriti a mano annullabili e budget.
 *
 *   - un movimento manuale e' una riga di `transactions` con `origin = 'manual'` e `source_txn_id = 'manual:<richiesta>'`
 *     (idempotenza: la stessa richiesta ripetuta dal telefono non crea un secondo movimento); non si cancella mai:
 *     l'annullamento e' una riga di `manual_voids` (append-only, con motivo);
 *   - `budgets`: versioni append-only per ambito (categoria, gruppo di budget, totale) valide da un mese; vale l'ultima
 *     con `valid_from` <= mese; `amount_minor` NULL = budget tolto da quel mese;
 *   - `link_reversals`: revoca append-only (con motivo) di un abbinamento manuale ↔ banca confermato per errore
 *     (i collegamenti si decidono una sola volta: la correzione e' una riga nuova, mai una modifica).
 *
 * Attivata (autorizzazione del titolare, C-07): aggiunta a `MIGRATIONS`, `manual_voids` aggiunta a `EXCLUDED_IDS_SQL`
 * e i doppioni il cui collegamento e' revocato in `link_reversals` rimossi dall'esclusione (services/effective.ts).
 * La migrazione del database reale (con backup verificato) resta separata, a cura del titolare. Vedi
 * docs/movimenti-manuali-e-budget.md.
 */
export const migration007: Migration = {
  version: 7,
  name: "manual_and_budgets",
  statements: [
    `CREATE TABLE manual_voids (
      transaction_id TEXT PRIMARY KEY REFERENCES transactions(id),
      reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
      created_at TEXT NOT NULL
    ) STRICT`,
    ...appendOnly("manual_voids", "annullamenti immutabili"),
    `CREATE TABLE link_reversals (
      link_id TEXT PRIMARY KEY REFERENCES transaction_links(id),
      reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
      created_at TEXT NOT NULL
    ) STRICT`,
    ...appendOnly("link_reversals", "revoche immutabili"),
    `CREATE TABLE budgets (
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
    ) STRICT`,
    `CREATE INDEX budgets_by_scope ON budgets (scope, target, currency, valid_from, created_at)`,
    ...appendOnly("budgets", "budget immutabili (registrarne una nuova versione)"),
  ],
};

/**
 * Migrazioni pronte ma non ancora attive (copia sintetica e test soltanto). Vuoto dal 05/11/2026: la v8 (ricerca
 * FTS5, 008_search.ts) e' passata in `MIGRATIONS`. Resta come punto d'aggancio per la prossima migrazione in attesa.
 */
export const PENDING_MIGRATIONS: readonly Migration[] = [];
