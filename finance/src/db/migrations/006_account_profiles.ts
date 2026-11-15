import type { Migration } from "../migrate.ts";
import { appendOnly } from "./001_core.ts";

/**
 * v6 — profilo dei conti dichiarato dall'utente (mai dedotto dal nome in un estratto):
 *   - intestatario formale, utilizzatore principale e gestione/supervisione familiare restano distinti;
 *   - `unknown` finche' l'utente non dichiara; ogni dichiarazione e' una riga nuova (storico append-only, vale l'ultima).
 * `accounts.holder_role` resta per compatibilita' ma non e' una dichiarazione di titolarita'.
 */
export const migration006: Migration = {
  version: 6,
  name: "account_profiles",
  statements: [
    `CREATE TABLE account_profiles (
      id TEXT PRIMARY KEY,
      account_id TEXT NOT NULL REFERENCES accounts(id),
      formal_holder TEXT NOT NULL CHECK (formal_holder IN ('self', 'parent', 'self_with_parent', 'other', 'unknown')),
      primary_user TEXT NOT NULL CHECK (primary_user IN ('self', 'parent', 'shared', 'unknown')),
      supervision TEXT NOT NULL CHECK (supervision IN ('parent', 'none', 'unknown')),
      note TEXT,
      created_at TEXT NOT NULL
    ) STRICT`,
    `CREATE INDEX account_profiles_by_account ON account_profiles (account_id, created_at)`,
    ...appendOnly("account_profiles", "profili dei conti immutabili (dichiararne uno nuovo)"),
    // stato operativo del conto: la data di chiusura e' separata dall'ultimo movimento osservato e puo' restare ignota
    `CREATE TABLE account_status_events (
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
    ) STRICT`,
    `CREATE INDEX account_status_events_by_account ON account_status_events (account_id, created_at)`,
    ...appendOnly("account_status_events", "stato dei conti immutabile (registrare un nuovo evento)"),
  ],
};
