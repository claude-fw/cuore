/** Utilita' di test: database in memoria, orologio e id deterministici, CSV sintetici. */
import type { Ctx } from "../src/context.ts";
import { migrate } from "../src/db/migrate.ts";
import { MIGRATIONS } from "../src/db/migrations/index.ts";
import type { Db, SqlValue, Statement } from "../src/db/types.ts";
import { type ImportReport, importFile } from "../src/import/importer.ts";
import { TRANSACTION_COLUMNS } from "../src/import/parsers/cloud-finance-csv.ts";
import { createAccount } from "../src/services/accounts.ts";
import { freshDb } from "./db-backend.ts";

export { BACKEND, freshDb, integrityCheck } from "./db-backend.ts";

export interface TestEnv {
  db: Db;
  ctx: Ctx;
}

export function testCtx(db: Db): Ctx {
  let t = Date.parse("2026-02-01T00:00:00.000Z");
  let n = 0;
  return {
    db,
    now: () => {
      t += 1000;
      return new Date(t).toISOString();
    },
    newId: () => {
      n += 1;
      return `id-${String(n).padStart(5, "0")}`;
    },
  };
}

export async function setup(): Promise<TestEnv> {
  const db = await freshDb();
  const ctx = testCtx(db);
  await migrate(db, MIGRATIONS, ctx.now);
  return { db, ctx };
}

export async function setupWithAccounts(): Promise<TestEnv> {
  const env = await setup();
  await createAccount(env.ctx, {
    id: "conto",
    name: "Conto DEMO",
    type: "current",
    currency: "EUR",
    holderRole: "owner",
  });
  await createAccount(env.ctx, {
    id: "carta",
    name: "Carta DEMO",
    type: "card",
    currency: "EUR",
    holderRole: "owner",
  });
  await createAccount(env.ctx, {
    id: "usd",
    name: "Conto USD DEMO",
    type: "current",
    currency: "USD",
    holderRole: "owner",
  });
  return env;
}

export const HEADER = TRANSACTION_COLUMNS.join(",");

/** Riga del CSV neutro: id,data,contabilizzazione,stato,importo,valuta,descrizione,controparte,orig,valuta_orig */
export function row(
  date: string,
  amount: string,
  description: string,
  opts: {
    id?: string;
    booking?: string | null;
    status?: "pending" | "booked";
    currency?: string;
    counterparty?: string;
    original?: [string, string];
  } = {},
): string {
  const status = opts.status ?? "booked";
  const booking = opts.booking === undefined ? (status === "booked" ? date : "") : (opts.booking ?? "");
  return [
    opts.id ?? "",
    date,
    booking,
    status,
    amount,
    opts.currency ?? "EUR",
    description,
    opts.counterparty ?? "",
    opts.original?.[0] ?? "",
    opts.original?.[1] ?? "",
  ].join(",");
}

export function csvBytes(lines: readonly string[], eol = "\n", header = HEADER): Uint8Array {
  return new TextEncoder().encode([header, ...lines].join(eol) + eol);
}

export function importTx(
  env: TestEnv,
  account: string,
  lines: readonly string[],
  fileName = "test.csv",
): Promise<ImportReport> {
  return importFile(env.ctx, {
    accountId: account,
    parserId: "cloud-finance-csv",
    fileName,
    content: csvBytes(lines),
  });
}

export function importBalances(
  env: TestEnv,
  account: string,
  lines: readonly string[],
): Promise<ImportReport> {
  return importFile(env.ctx, {
    accountId: account,
    parserId: "cloud-finance-balances-csv",
    fileName: "saldi.csv",
    content: new TextEncoder().encode(["date,balance,currency", ...lines].join("\n")),
  });
}

export async function count(
  db: Db,
  table: string,
  where = "1 = 1",
  params: SqlValue[] = [],
): Promise<number> {
  const r = await db.first<{ n: number }>(`SELECT COUNT(*) AS n FROM ${table} WHERE ${where}`, params);
  return r?.n ?? 0;
}

/** Db che fallisce all'n-esima istruzione di un batch, per verificare il rollback. */
export class FailingDb implements Db {
  readonly inner: Db;
  failAt: number | null;
  constructor(inner: Db, failAt: number | null) {
    this.inner = inner;
    this.failAt = failAt;
  }
  all<T>(sql: string, params?: readonly SqlValue[]): Promise<T[]> {
    return this.inner.all<T>(sql, params);
  }
  first<T>(sql: string, params?: readonly SqlValue[]): Promise<T | null> {
    return this.inner.first<T>(sql, params);
  }
  batch(statements: readonly Statement[]): Promise<void> {
    if (this.failAt === null || statements.length <= this.failAt) return this.inner.batch(statements);
    const at = this.failAt;
    this.failAt = null;
    // l'istruzione `at` viene sostituita da una che viola un vincolo: il batch deve annullarsi per intero
    return this.inner.batch(
      statements.map((s, i) =>
        i === at ? { sql: "INSERT INTO accounts (id) VALUES (NULL)", params: [] } : s,
      ),
    );
  }
}
