import type { Migration } from "../migrate.ts";
import { appendOnly } from "./001_core.ts";

/**
 * v3 — pianificazione: obiettivi di risparmio, accantonamenti (append-only), acquisti pianificati
 * (con riferimento facoltativo a un blocco del To-do Notion, senza alcun accesso a Notion), cambi valuta osservati.
 */
export const migration003: Migration = {
  version: 3,
  name: "planning",
  statements: [
    `CREATE TABLE savings_goals (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      target_minor INTEGER NOT NULL CHECK (target_minor > 0),
      currency TEXT NOT NULL,
      deadline TEXT,
      status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'reached', 'cancelled')),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    ) STRICT`,
    `CREATE TRIGGER savings_goals_no_delete BEFORE DELETE ON savings_goals
      BEGIN SELECT RAISE(ABORT, 'nessuna cancellazione degli obiettivi (usare lo stato cancelled)'); END`,

    `CREATE TABLE goal_allocations (
      id TEXT PRIMARY KEY,
      goal_id TEXT NOT NULL REFERENCES savings_goals(id),
      amount_minor INTEGER NOT NULL CHECK (amount_minor <> 0),
      currency TEXT NOT NULL,
      allocated_on TEXT NOT NULL,
      note TEXT,
      created_at TEXT NOT NULL
    ) STRICT`,
    ...appendOnly("goal_allocations", "accantonamenti immutabili (correggere con un movimento opposto)"),

    `CREATE TABLE planned_purchases (
      id TEXT PRIMARY KEY,
      title TEXT NOT NULL,
      estimated_minor INTEGER CHECK (estimated_minor IS NULL OR estimated_minor > 0),
      currency TEXT NOT NULL,
      priority INTEGER CHECK (priority IS NULL OR priority BETWEEN 1 AND 5),
      status TEXT NOT NULL CHECK (status IN ('idea', 'planned', 'purchased', 'cancelled')),
      goal_id TEXT REFERENCES savings_goals(id),
      notion_block_id TEXT UNIQUE,
      transaction_id TEXT REFERENCES transactions(id),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      CHECK (transaction_id IS NULL OR status = 'purchased')
    ) STRICT`,
    `CREATE TRIGGER planned_purchases_no_delete BEFORE DELETE ON planned_purchases
      BEGIN SELECT RAISE(ABORT, 'nessuna cancellazione degli acquisti pianificati (usare lo stato cancelled)'); END`,

    `CREATE TABLE fx_rates (
      rate_date TEXT NOT NULL,
      base TEXT NOT NULL,
      quote TEXT NOT NULL,
      rate TEXT NOT NULL,
      source TEXT NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY (rate_date, base, quote, source),
      CHECK (base <> quote)
    ) STRICT`,
    ...appendOnly("fx_rates", "cambi osservati immutabili"),
  ],
};
