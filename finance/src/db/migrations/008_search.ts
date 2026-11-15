import type { Migration } from "../migrate.ts";

/**
 * v8 — ATTIVA dal 05/11/2026 (in `MIGRATIONS`, blocco A6; prima in `PENDING_MIGRATIONS`): ricerca a testo libero sui movimenti con SQLite FTS5.
 *
 *   - `transactions_search_source` (vista): per ogni movimento il `rowid` di `transactions`, descrizione, controparte e
 *     la nota del movimento manuale (motivo della PRIMA riga di audit `add_manual_transaction`, se c'e');
 *   - `transactions_search` (FTS5 a contenuto esterno su quella vista): solo l'indice, nessuna copia del testo;
 *     tokenizer `unicode61 remove_diacritics 2` (maiuscole e accenti ignorati), ricerca per prefisso;
 *   - trigger di sincronizzazione (le tabelle restano append-only: nessun trigger di cancellazione, che i trigger
 *     `BEFORE DELETE` di v1 renderebbero comunque irraggiungibile):
 *       · nuovo movimento -> indicizzato (nota vuota: l'audit arriva dopo nello stesso batch);
 *       · descrizione o controparte modificate (correzione di un manuale, aggiornamento dall'importazione) ->
 *         vecchi valori tolti dall'indice e nuovi inseriti; i cambi di sola data/stato non toccano l'indice;
 *       · prima riga di audit `add_manual_transaction` con motivo -> il movimento viene reindicizzato con la nota;
 *   - riempimento iniziale con `rebuild` (legge la vista): un database v7 esistente diventa ricercabile per intero.
 * L'indice e' un dato DERIVATO: si ricostruisce da `transactions` + `audit_log` (`rebuild`), non va copiato nei
 * backup. Vedi docs/ricerca.md.
 */
/** Nota di un movimento manuale (stessa definizione per vista, trigger e servizio di ricerca). */
export const NOTE_OF = (id: string): string =>
  `(SELECT a.reason FROM audit_log a WHERE a.entity = 'transaction' AND a.entity_id = ${id}
      AND a.action = 'add_manual_transaction' ORDER BY a.id LIMIT 1)`;

export const migration008: Migration = {
  version: 8,
  name: "search",
  statements: [
    `CREATE VIEW transactions_search_source AS
      SELECT t.rowid AS search_rowid, t.description AS description, t.counterparty AS counterparty,
        ${NOTE_OF("t.id")} AS note
      FROM transactions t`,
    `CREATE VIRTUAL TABLE transactions_search USING fts5(
      description, counterparty, note,
      content = 'transactions_search_source', content_rowid = 'search_rowid',
      tokenize = 'unicode61 remove_diacritics 2'
    )`,
    `CREATE TRIGGER transactions_search_after_insert AFTER INSERT ON transactions BEGIN
      INSERT INTO transactions_search (rowid, description, counterparty, note)
        VALUES (NEW.rowid, NEW.description, NEW.counterparty, ${NOTE_OF("NEW.id")});
    END`,
    `CREATE TRIGGER transactions_search_after_update AFTER UPDATE OF description, counterparty ON transactions
      WHEN NEW.description IS NOT OLD.description OR NEW.counterparty IS NOT OLD.counterparty BEGIN
      INSERT INTO transactions_search (transactions_search, rowid, description, counterparty, note)
        VALUES ('delete', OLD.rowid, OLD.description, OLD.counterparty, ${NOTE_OF("OLD.id")});
      INSERT INTO transactions_search (rowid, description, counterparty, note)
        VALUES (NEW.rowid, NEW.description, NEW.counterparty, ${NOTE_OF("NEW.id")});
    END`,
    `CREATE TRIGGER transactions_search_after_manual_note AFTER INSERT ON audit_log
      WHEN NEW.entity = 'transaction' AND NEW.action = 'add_manual_transaction' AND NEW.reason IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM audit_log a WHERE a.entity = 'transaction' AND a.entity_id = NEW.entity_id
          AND a.action = 'add_manual_transaction' AND a.id < NEW.id) BEGIN
      INSERT INTO transactions_search (transactions_search, rowid, description, counterparty, note)
        SELECT 'delete', t.rowid, t.description, t.counterparty, NULL FROM transactions t WHERE t.id = NEW.entity_id;
      INSERT INTO transactions_search (rowid, description, counterparty, note)
        SELECT t.rowid, t.description, t.counterparty, NEW.reason FROM transactions t WHERE t.id = NEW.entity_id;
    END`,
    `INSERT INTO transactions_search (transactions_search) VALUES ('rebuild')`,
  ],
};
