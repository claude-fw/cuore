/** Processo di scrittura del ponte reale (`scripts/scrivi.ts`) e consultazione `manual` / `budgets`, su copie SINTETICHE. */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  copyFileSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  realpathSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import { checkDb, dailyBackup } from "../scripts/scrivi.ts";
import { createCtx } from "../src/context.ts";
import { MIGRATIONS } from "../src/db/migrations/index.ts";
import { NodeSqliteDb } from "../src/db/node-sqlite.ts";
import { parseBackupName } from "../src/domain/backup-retention.ts";
import { runQuery } from "../src/query/operations.ts";
import { setBudget } from "../src/services/budgets.ts";
import { addManualTransaction, voidManualTransaction } from "../src/services/manual.ts";
import { createSyntheticSession } from "../src/synthetic/generate.ts";

const FINANCE = join(dirname(fileURLToPath(import.meta.url)), "..");
const SCRIVI = join(FINANCE, "scripts", "scrivi.ts");

/** Database sintetico v7 in `<base>/finance/db/cloud-finance.sqlite3`, come quello reale. */
async function realLike(): Promise<{ base: string; db: string; done: () => void }> {
  const base = realpathSync(mkdtempSync(join(tmpdir(), "cuore-scrivi-test-")));
  const s = await createSyntheticSession(join(base, "s"), { allowedRoot: base, forbidden: [] }, MIGRATIONS);
  const dir = join(base, "finance", "db");
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  const db = join(dir, "cloud-finance.sqlite3");
  copyFileSync(join(s.realDir, "finance-sintetico.sqlite3"), db);
  return { base, db, done: () => rmSync(base, { recursive: true, force: true }) };
}

const run = (args: string[], input: unknown) =>
  spawnSync(process.execPath, [SCRIVI, ...args], { input: JSON.stringify(input), encoding: "utf8" });

const SPESA = {
  requestId: "r-0001",
  accountId: "conto-demo",
  transactionDate: "2026-02-10",
  amountMinor: -450,
  currency: "EUR",
  description: "Caffe",
};

describe("scrivi.ts: scritture sul database indicato", () => {
  it("senza --db: 64; scrittura con backup verificato una volta al giorno; richiesta ripetuta senza doppione", async () => {
    const t = await realLike();
    try {
      assert.equal(run(["esegui"], {}).status, 64);
      const add = run(["esegui", "--db", t.db], { op: "aggiungi_manuale", input: SPESA });
      assert.equal(add.status, 0, add.stdout);
      const doc = JSON.parse(add.stdout);
      assert.equal(doc.risultato.status, "created");
      assert.equal(doc.backup, "creato");
      const again = JSON.parse(
        run(["esegui", "--db", t.db], { op: "aggiungi_manuale", input: SPESA }).stdout,
      );
      assert.deepEqual([again.risultato.status, again.backup], ["already_recorded", "presente"]);
      const files = readdirSync(join(t.base, "finance", "backup"));
      assert.equal(files.length, 1);
      assert.equal(parseBackupName(files[0] as string)?.kind, "scrittura");
      assert.equal(statSync(join(t.base, "finance", "backup", files[0] as string)).mode & 0o777, 0o600);
      const read = JSON.parse(
        run(["esegui", "--db", t.db], { op: "stato_budget", input: { mese: "2026-02", valuta: "EUR" } })
          .stdout,
      );
      assert.equal(read.ok, true);
      assert.equal(read.backup, undefined);
      assert.deepEqual(readdirSync(join(t.base, "finance", "db")), ["cloud-finance.sqlite3"]);
    } finally {
      t.done();
    }
  });

  it("backup non verificato o impossibile: scrittura rifiutata (2), database invariato", async () => {
    const t = await realLike();
    try {
      await assert.rejects(
        dailyBackup(t.db, {
          copy: async (_from, to) => {
            writeFileSync(to, "non un database");
          },
        }),
        /backup non/,
      );
      assert.ok(readdirSync(join(t.base, "finance", "backup")).every((n) => parseBackupName(n) === null));
      rmSync(join(t.base, "finance", "backup"), { recursive: true });
      writeFileSync(join(t.base, "finance", "backup"), "file al posto della cartella");
      const r = run(["esegui", "--db", t.db], { op: "aggiungi_manuale", input: SPESA });
      assert.equal(r.status, 2, r.stdout);
      assert.match(JSON.parse(r.stdout).motivo, /backup/);
      const ro = new NodeSqliteDb(t.db, { readOnly: true });
      try {
        assert.equal(
          (await ro.first<{ n: number }>("SELECT COUNT(*) AS n FROM transactions WHERE origin = 'manual'"))
            ?.n,
          0,
        );
      } finally {
        ro.close();
      }
    } finally {
      t.done();
    }
  });

  it("percorsi rifiutati: relativo, collegamento simbolico, repository", async () => {
    const t = await realLike();
    try {
      assert.throws(() => checkDb("relativo.sqlite3"), /assoluto/);
      const link = join(t.base, "link.sqlite3");
      symlinkSync(t.db, link);
      assert.throws(() => checkDb(link), /file normale/);
      const inRepo = join(FINANCE, "examples", "x.sqlite3");
      assert.equal(run(["esegui", "--db", inRepo], { op: "consulta", input: {} }).status, 2);
      assert.equal(checkDb(t.db), t.db);
    } finally {
      t.done();
    }
  });
});

describe("consultazione: manual e budgets (A5)", () => {
  it("manuali con stato annullato e budget del mese, in sola lettura", async () => {
    const t = await realLike();
    const db = new NodeSqliteDb(t.db);
    try {
      const ctx = createCtx(db);
      const a = await addManualTransaction(ctx, SPESA);
      const b = await addManualTransaction(ctx, { ...SPESA, requestId: "r-0002", amountMinor: -900 });
      assert.ok(a.status === "created" && b.status === "created");
      await voidManualTransaction(ctx, b.id, "doppione");
      await setBudget(ctx, {
        scope: "total",
        target: null,
        validFrom: "2026-02",
        currency: "EUR",
        amountMinor: 10000,
      });
      const m = await runQuery(db, "manual", { from: "2026-02-01", to: "2026-02-28" });
      assert.ok(m.ok);
      const items = (m.data as { items: { id: string; voided: boolean; voidedAt: string | null }[] }).items;
      assert.deepEqual(
        items.map((x) => [x.id, x.voided, x.voidedAt !== null]).sort(),
        [
          [a.id, false, false],
          [b.id, true, true],
        ].sort(),
      );
      const bg = await runQuery(db, "budgets", {}, { today: "2026-02-15" });
      assert.ok(bg.ok);
      const cur = (bg.data as { month: string; currencies: { currency: string; lines: unknown[] }[] })
        .currencies;
      assert.deepEqual([cur.length, cur[0]?.currency, cur[0]?.lines.length], [1, "EUR", 1]);
      assert.equal((await runQuery(db, "budgets", { month: "2026-13" })).ok, false);
      assert.equal((await runQuery(db, "budgets", {})).ok, false);
    } finally {
      db.close();
      t.done();
    }
  });
});
