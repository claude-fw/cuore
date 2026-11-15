/**
 * Worker di PROVA (solo test, mai pubblicato): esegue il nucleo Cloud Finance DENTRO workerd, con il binding D1
 * locale, senza `nodejs_compat`. Riceve i file sintetici dal test (base64) e restituisce un rapporto JSON.
 * Verifica nel runtime Cloudflare cio' che i test in Node non possono provare: Web Crypto, TextDecoder
 * windows-1252, DecompressionStream (XLSX), Intl con fusi orari, e l'adattatore D1 usato "dall'interno".
 */
import { type D1DatabaseLike, D1Db } from "../../src/db/d1.ts";
import { migrate } from "../../src/db/migrate.ts";
import { MIGRATIONS } from "../../src/db/migrations/index.ts";
import type { Db, SqlValue, Statement } from "../../src/db/types.ts";
import { stmt } from "../../src/db/types.ts";
import { ConflictError, commitGuarded, readWriteSeq } from "../../src/db/write.ts";
import { importFile } from "../../src/import/importer.ts";
import { createTabularParser } from "../../src/import/tabular/engine.ts";
import { runQuery } from "../../src/query/operations.ts";
import { createAccount } from "../../src/services/accounts.ts";
import { classifyTransactions, publishRuleSet } from "../../src/services/classification.ts";
import { decideLink, detectLinks, listLinks } from "../../src/services/links.ts";
import { reconcileAccount } from "../../src/services/reconciliation.ts";
import { periodReport } from "../../src/services/reports.ts";
import { SPEC_A, SPEC_B, SPEC_C } from "../fixtures/synthetic-formats.ts";

interface Env {
  DB: D1DatabaseLike;
}

interface Payload {
  files: Record<string, string>;
  rules: unknown;
}

const b64 = (s: string) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

/** Fa fallire l'istruzione `at` del primo batch abbastanza lungo (per provare il rollback dentro il runtime). */
class FailOnce implements Db {
  readonly inner: Db;
  at: number | null;
  constructor(inner: Db, at: number) {
    this.inner = inner;
    this.at = at;
  }
  all<T>(sql: string, params?: readonly SqlValue[]) {
    return this.inner.all<T>(sql, params);
  }
  first<T>(sql: string, params?: readonly SqlValue[]) {
    return this.inner.first<T>(sql, params);
  }
  batch(statements: readonly Statement[]) {
    const at = this.at;
    if (at === null || statements.length <= at) return this.inner.batch(statements);
    this.at = null;
    return this.inner.batch(
      statements.map((s, i) => (i === at ? stmt("INSERT INTO accounts (id) VALUES (NULL)") : s)),
    );
  }
}

async function scenario(env: Env, payload: Payload) {
  const db = new D1Db(env.DB);
  const now = () => new Date().toISOString();
  const ctx = { db, now, newId: () => crypto.randomUUID() };
  const out: Record<string, unknown> = {};
  const count = async (sql: string) => (await db.first<{ n: number }>(sql))?.n ?? -1;

  out.runtime = {
    navigator: typeof navigator === "undefined" ? null : navigator.userAgent,
    hasProcess: typeof (globalThis as { process?: unknown }).process !== "undefined",
  };
  out.migrate1 = await migrate(db, MIGRATIONS, now);
  out.migrate2 = await migrate(db, MIGRATIONS, now);

  await createAccount(ctx, {
    id: "conto",
    name: "Conto DEMO",
    type: "current",
    currency: "EUR",
    holderRole: "owner",
  });
  await createAccount(ctx, {
    id: "carta",
    name: "Carta DEMO",
    type: "card",
    currency: "EUR",
    holderRole: "owner",
  });

  const neutral = async (account: string, parserId: string, name: string) =>
    importFile(ctx, {
      accountId: account,
      parserId,
      fileName: name,
      content: b64(payload.files[name] ?? ""),
    });
  const n1 = await neutral("conto", "cloud-finance-csv", "conto-demo.csv");
  const n2 = await neutral("conto", "cloud-finance-csv", "conto-demo.csv");
  await neutral("carta", "cloud-finance-csv", "carta-demo.csv");
  await neutral("conto", "cloud-finance-balances-csv", "saldi-conto-demo.csv");
  out.neutral = { first: n1.status, created: n1.counts.created, second: n2.status };

  const tab = async (spec: typeof SPEC_A, name: string, account = "carta") => {
    const parser = createTabularParser(spec);
    const r = await importFile(ctx, {
      accountId: account,
      parserId: parser.id,
      parser,
      fileName: name,
      content: b64(payload.files[name] ?? ""),
    });
    return { status: r.status, counts: r.counts, issues: r.issues.map((i) => i.code), notices: r.notices };
  };
  out.specA = await tab(SPEC_A, "a.csv");
  out.specAdates = await db.all(
    "SELECT source_txn_id, transaction_date, amount_minor, source_type FROM transactions WHERE account_id = 'carta' AND source_txn_id LIKE 'W%' ORDER BY source_txn_id",
  );
  out.excluded = await db.all("SELECT source_txn_id, reason FROM excluded_rows");
  out.specB = await tab(SPEC_B, "b.csv", "conto");
  out.specBtext = await db.first<{ description: string }>(
    "SELECT description FROM transactions WHERE description LIKE 'Caff%' AND source_txn_id IS NULL AND account_id = 'conto' AND transaction_date = '2026-02-05'",
  );
  out.specC = await tab(SPEC_C, "c.xlsx", "conto");

  await publishRuleSet(ctx, payload.rules);
  out.classify = await classifyTransactions(ctx);
  out.detect = await detectLinks(ctx);
  for (const l of await listLinks(ctx, "proposed")) await decideLink(ctx, l.id, "confirmed");
  out.reconcile = await reconcileAccount(ctx, "conto", "2026-01-01", "2026-01-31");
  const report = await periodReport(ctx, "2026-01-01", "2026-01-31", ["conto", "carta"]);
  out.report = report.currencies.map((c) => ({
    currency: c.currency,
    income: c.income,
    expenses: c.expenses,
    refunds: c.refunds,
    transfersIn: c.transfersIn,
  }));

  // consultazione (contratto v1) dentro workerd: busta, sola lettura e paginazione (btoa/atob del runtime)
  const period = { from: "2026-01-01", to: "2026-01-31", accountIds: ["conto", "carta"] };
  const summary = await runQuery(db, "summary", period);
  const ids: string[] = [];
  let cursor: string | null = null;
  let pages = 0;
  do {
    const page = await runQuery(db, "transactions", { ...period, limit: 3, ...(cursor ? { cursor } : {}) });
    if (!page.ok) throw new Error(`transactions: ${page.error.message}`);
    const data = page.data as { items: { id: string }[]; nextCursor: string | null };
    ids.push(...data.items.map((i) => i.id));
    cursor = data.nextCursor;
    pages += 1;
  } while (cursor !== null && pages < 20);
  out.consultation = {
    summarySameAsReport:
      summary.ok &&
      JSON.stringify((summary.data as { currencies: unknown }).currencies) ===
        JSON.stringify(report.currencies),
    pages,
    distinctIds: new Set(ids).size === ids.length,
    listed: ids.length,
    unknownOperation: (await runQuery(db, "drop", {})).ok,
  };

  // immutabilita' dentro il runtime
  const immut: Record<string, string> = {};
  for (const sql of [
    "DELETE FROM raw_records",
    "UPDATE excluded_rows SET reason = 'x'",
    "DELETE FROM transactions",
    "UPDATE audit_log SET action = 'x'",
  ]) {
    try {
      await db.batch([stmt(sql)]);
      immut[sql] = "ACCETTATA";
    } catch (err) {
      immut[sql] = err instanceof Error ? err.message.slice(0, 80) : String(err);
    }
  }
  out.immutability = immut;

  // rollback dentro il runtime: l'importazione fallisce a meta' e non lascia nulla
  const before = await count("SELECT COUNT(*) AS n FROM transactions");
  const beforeRaw = await count("SELECT COUNT(*) AS n FROM raw_records");
  const failing = { ...ctx, db: new FailOnce(db, 6) };
  const parserA = createTabularParser(SPEC_A);
  const failed = await importFile(failing, {
    accountId: "conto",
    parserId: parserA.id,
    parser: parserA,
    fileName: "a2.csv",
    content: b64(payload.files["a.csv"] ?? ""),
  });
  out.rollback = {
    status: failed.status,
    issue: failed.issues[0]?.code,
    transactionsUnchanged: before === (await count("SELECT COUNT(*) AS n FROM transactions")),
    rawUnchanged: beforeRaw === (await count("SELECT COUNT(*) AS n FROM raw_records")),
  };

  // controllo di concorrenza
  const seq = await readWriteSeq(db);
  await commitGuarded(db, seq, [
    stmt("INSERT INTO institutions (id, name, created_at) VALUES ('w1', 'W1', 't')"),
  ]);
  try {
    await commitGuarded(db, seq, [
      stmt("INSERT INTO institutions (id, name, created_at) VALUES ('w2', 'W2', 't')"),
    ]);
    out.conflict = "NON RILEVATO";
  } catch (err) {
    out.conflict = err instanceof ConflictError ? "ConflictError" : String(err);
  }

  out.integrity = {
    quick: (await db.first<{ quick_check: string }>("PRAGMA quick_check"))?.quick_check,
    foreignKeyViolations: (await db.all("PRAGMA foreign_key_check")).length,
  };
  out.totals = {
    transactions: await count("SELECT COUNT(*) AS n FROM transactions"),
    excluded: await count("SELECT COUNT(*) AS n FROM excluded_rows"),
    importRuns: await count("SELECT COUNT(*) AS n FROM import_runs"),
  };
  return out;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method !== "POST") return new Response("solo POST", { status: 405 });
    try {
      return Response.json(await scenario(env, (await request.json()) as Payload));
    } catch (err) {
      return Response.json(
        { error: err instanceof Error ? `${err.name}: ${err.message}` : String(err) },
        { status: 500 },
      );
    }
  },
};
