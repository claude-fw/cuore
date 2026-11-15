import type { Migration } from "../migrate.ts";
import { appendOnly } from "./001_core.ts";

/**
 * v4 — formati bancari:
 *   - `transactions.source_type`: etichetta del tipo di operazione COSI' COME la riporta la fonte (es. una colonna
 *     "tipo"); e' un dato osservato, non una classificazione: le regole possono usarlo, nulla lo applica da solo;
 *   - `excluded_rows`: righe lette ma escluse per una ragione DICHIARATA dalla specifica del formato (es. stato
 *     "annullata"). Conservate integralmente: nessuna riga viene scartata in silenzio.
 */
export const migration004: Migration = {
  version: 4,
  name: "bank_formats",
  statements: [
    "ALTER TABLE transactions ADD COLUMN source_type TEXT",
    `CREATE TABLE excluded_rows (
      id TEXT PRIMARY KEY,
      import_run_id TEXT NOT NULL REFERENCES import_runs(id),
      line_number INTEGER NOT NULL,
      reason TEXT NOT NULL,
      source_txn_id TEXT,
      content_sha256 TEXT NOT NULL,
      raw_json TEXT NOT NULL,
      UNIQUE (import_run_id, line_number)
    ) STRICT`,
    ...appendOnly("excluded_rows", "righe escluse immutabili"),
  ],
};
