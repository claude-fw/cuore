import type { Migration } from "../migrate.ts";
import { appendOnly } from "./001_core.ts";

/**
 * v5 — crediti e debiti verso persone o enti (non conti bancari):
 *   - `counterparties`: controparti scelte dall'utente (nessun dato bancario);
 *   - `obligations`: un credito (`receivable`, mi devono) o un debito (`payable`, devo); immutabile: tutto cio' che
 *     cambia passa dagli eventi;
 *   - `obligation_events`: apertura (una sola), aumento, pagamento, condono, rettifica. Append-only: il residuo e' la
 *     somma degli eventi, mai un campo modificato a mano. Un evento puo' riferirsi a un movimento bancario SOLO se
 *     di apertura, aumento o pagamento; condono e rettifica non muovono denaro.
 * Nessuna di queste tabelle crea entrate o spese: la cassa resta solo nelle transazioni bancarie.
 */
export const migration005: Migration = {
  version: 5,
  name: "obligations",
  statements: [
    `CREATE TABLE counterparties (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      note TEXT,
      created_at TEXT NOT NULL
    ) STRICT`,
    ...appendOnly("counterparties", "controparti immutabili (crearne una nuova per cambiare)"),

    `CREATE TABLE obligations (
      id TEXT PRIMARY KEY,
      counterparty_id TEXT NOT NULL REFERENCES counterparties(id),
      direction TEXT NOT NULL CHECK (direction IN ('receivable', 'payable')),
      currency TEXT NOT NULL,
      description TEXT NOT NULL,
      opened_on TEXT NOT NULL,
      due_on TEXT,
      created_at TEXT NOT NULL,
      CHECK (due_on IS NULL OR due_on >= opened_on)
    ) STRICT`,
    ...appendOnly("obligations", "crediti e debiti immutabili (si cambiano solo con eventi)"),

    `CREATE TABLE obligation_events (
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
    ) STRICT`,
    `CREATE UNIQUE INDEX obligation_events_one_open ON obligation_events (obligation_id) WHERE kind = 'open'`,
    `CREATE INDEX obligation_events_by_obligation ON obligation_events (obligation_id, occurred_on)`,
    `CREATE INDEX obligation_events_by_transaction ON obligation_events (transaction_id)
      WHERE transaction_id IS NOT NULL`,
    ...appendOnly(
      "obligation_events",
      "eventi di crediti e debiti immutabili (correggere con una rettifica)",
    ),
  ],
};
