/**
 * Sola lettura REALE su database SINTETICI: `readOnly` crea -wal/-shm in modalita' WAL; `readImmutable` (usato da CLI
 * `query`, server MCP e CUORE Money) copia un'istantanea coerente in memoria, non tocca nulla sul disco, rifiuta
 * scritture e copie incoerenti, anche con uno scrittore concorrente in un altro processo.
 */
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import {
  existsSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, describe, it } from "node:test";
import { migrate } from "../src/db/migrate.ts";
import { MIGRATIONS } from "../src/db/migrations/index.ts";
import { NodeSqliteDb, readImmutable, SnapshotUnavailableError } from "../src/db/node-sqlite.ts";
import { type Db, stmt } from "../src/db/types.ts";
import { OPERATIONS, runQuery } from "../src/query/operations.ts";
import { createAccount } from "../src/services/accounts.ts";
import { addManualBalance } from "../src/services/balances.ts";
import { BACKEND, importTx, row, testCtx } from "./helpers.ts";

const MCP = new URL("../scripts/mcp-server.ts", import.meta.url).pathname;
const CLI = new URL("../src/cli/main.ts", import.meta.url).pathname;
const WRITER = new URL("./fixtures/concurrent-writer.ts", import.meta.url).pathname;

/** Contenuto della cartella con le impronte dei file: uguale prima e dopo = nulla creato, cambiato o rimosso. */
const snapshot = (dir: string) =>
  readdirSync(dir)
    .sort()
    .map((f) => [f, readFileSync(join(dir, f)).toString("base64")]);

async function demoDb(dir: string): Promise<string> {
  const path = join(dir, "prova.sqlite3");
  const db = new NodeSqliteDb(path);
  const ctx = testCtx(db);
  await migrate(db, MIGRATIONS, ctx.now);
  await createAccount(ctx, {
    id: "conto",
    name: "Conto DEMO",
    type: "current",
    currency: "EUR",
    holderRole: "other",
  });
  await importTx({ db, ctx }, "conto", [
    row("2026-01-02", "100.00", "Entrata DEMO"),
    row("2026-01-03", "-30.00", "Spesa DEMO"),
  ]);
  await addManualBalance(ctx, "conto", "2026-01-03", 7000);
  db.close();
  return path;
}

const VALID: Record<string, Record<string, unknown>> = {
  accounts: {},
  summary: { from: "2026-01-01", to: "2026-01-31" },
  daily: { from: "2026-01-01", to: "2026-01-31" },
  monthly: { from: "2026-01-01", to: "2026-01-31" },
  balances: { asOf: "2026-01-31" },
  transactions: { from: "2026-01-01", to: "2026-01-31", limit: 10 },
  obligations: { asOf: "2026-01-31", status: "all" },
  data_status: { asOf: "2026-01-31" },
  overview: { from: "2026-01-01", to: "2026-01-31" },
  manual: { from: "2026-01-01", to: "2026-01-31" },
  budgets: { month: "2026-01" },
  search: { q: "demo" },
};

function mcp(path: string, calls: { name: string; arguments: unknown }[]) {
  const msgs = calls.map((params, i) => ({ jsonrpc: "2.0", id: i + 1, method: "tools/call", params }));
  const r = spawnSync(process.execPath, [MCP, "--db", path], {
    input: `${msgs.map((m) => JSON.stringify(m)).join("\n")}\n`,
    encoding: "utf8",
  });
  assert.equal(r.status, 0, r.stderr);
  return r.stdout
    .trim()
    .split("\n")
    .map(
      (l) =>
        JSON.parse(l) as {
          id: number;
          result?: { isError: boolean; content: { text: string }[] };
          error?: { code: number };
        },
    );
}

describe("SQLite in sola lettura reale", {
  skip: BACKEND === "d1" ? "riguarda SQLite via Node" : false,
}, () => {
  let dir = "";
  let path = "";
  before(async () => {
    dir = mkdtempSync(join(tmpdir(), "cloud-finance-ro-test-"));
    path = await demoDb(dir);
  });
  after(() => rmSync(dir, { recursive: true, force: true }));

  it("il database sintetico e' in WAL e chiuso pulito: nessun file accanto", () => {
    assert.deepEqual(readdirSync(dir), ["prova.sqlite3"]);
  });

  it("PROBLEMA documentato: `readOnly` su database WAL crea -wal e -shm (il file principale resta identico)", async () => {
    const copy = mkdtempSync(join(tmpdir(), "cloud-finance-ro-copy-"));
    try {
      const p = join(copy, "c.sqlite3");
      writeFileSync(p, readFileSync(path));
      const main = readFileSync(p);
      const db = new NodeSqliteDb(p, { readOnly: true });
      await db.all("SELECT count(*) FROM accounts");
      assert.ok(existsSync(`${p}-wal`) && existsSync(`${p}-shm`), "readOnly crea i file accanto");
      await assert.rejects(db.batch([stmt("DELETE FROM accounts")]), /readonly/i);
      db.close();
      assert.equal(Buffer.compare(main, readFileSync(p)), 0);
    } finally {
      rmSync(copy, { recursive: true, force: true });
    }
  });

  it("readImmutable: nessun file creato o modificato; scritture SQL e batch rifiutati", async () => {
    const snap = snapshot(dir);
    const n = await readImmutable(path, async (db) => {
      await assert.rejects(db.batch([stmt("DELETE FROM accounts")]), /sola lettura/);
      await assert.rejects(db.all("DELETE FROM accounts"), /readonly/i);
      await assert.rejects(db.all("CREATE TABLE intrusa (x)"), /readonly/i);
      await db.all("PRAGMA journal_mode = DELETE").catch(() => undefined); // tentativo: la cartella deve restare identica
      return (await db.first<{ n: number }>("SELECT count(*) AS n FROM accounts"))?.n;
    });
    assert.equal(n, 1);
    assert.deepEqual(snapshot(dir), snap, "cartella identica byte per byte");
  });

  it("readImmutable rifiuta: database assente (senza crearlo), -wal non vuoto, -journal presente, cambio durante la copia, copia rotta", async () => {
    const reason = async (p: string, readBytes?: (p: string) => Buffer) => {
      try {
        await readImmutable(p, async () => 1, { readBytes });
        return "ok";
      } catch (e) {
        assert.ok(e instanceof SnapshotUnavailableError, String(e));
        return e.reason;
      }
    };
    assert.equal(await reason(join(dir, "assente.sqlite3")), "missing");
    assert.equal(existsSync(join(dir, "assente.sqlite3")), false);

    const copy = mkdtempSync(join(tmpdir(), "cloud-finance-ro-busy-"));
    try {
      const p = join(copy, "c.sqlite3");
      writeFileSync(p, readFileSync(path));
      const writer = new NodeSqliteDb(p); // scrittore aperto: il -wal resta pieno
      await writer.batch([stmt("UPDATE accounts SET name = 'Conto DEMO 2'")]);
      assert.equal(await reason(p), "wal_not_empty");
      writer.close();
      assert.equal(await reason(p), "ok", "dopo la chiusura dello scrittore il -wal e' consolidato");

      writeFileSync(`${p}-journal`, "");
      assert.equal(await reason(p), "journal_present");
      rmSync(`${p}-journal`);

      // scrittura durante la copia (simulata: il file cambia mentre i byte vengono letti), a ogni tentativo
      let touches = 0;
      const touching = (q: string) => {
        touches++;
        utimesSync(q, new Date(), new Date(2001, 0, touches));
        return readFileSync(q);
      };
      assert.equal(await reason(p, touching), "changed_during_read");
      assert.equal(touches, 3, "tre tentativi, poi errore");
      // cambio solo al primo tentativo: il secondo riesce
      let first = true;
      const once = (q: string) => {
        if (first) utimesSync(q, new Date(), new Date(2002, 0, 1));
        first = false;
        return readFileSync(q);
      };
      assert.equal(await reason(p, once), "ok");
      // copia rotta: intestazione o pagine non valide, lunghezza diversa
      const zeroed = (q: string) => {
        const b = readFileSync(q);
        b.fill(0, 4096, 4096 * 3);
        return b;
      };
      assert.equal(await reason(p, zeroed), "invalid_snapshot");
      assert.equal(await reason(p, (q) => readFileSync(q).subarray(0, 4096)), "invalid_snapshot");
    } finally {
      rmSync(copy, { recursive: true, force: true });
    }
  });

  it("file di 0 byte = database vuoto: la consultazione risponde schema_mismatch, nessun file creato", async () => {
    const work = mkdtempSync(join(tmpdir(), "cloud-finance-ro-empty-"));
    try {
      const p = join(work, "vuoto.sqlite3");
      writeFileSync(p, "");
      const r = await readImmutable(p, (db) => runQuery(db, "accounts"));
      assert.deepEqual([r.ok, r.ok ? "" : r.error.code], [false, "schema_mismatch"]);
      assert.deepEqual(readdirSync(work), ["vuoto.sqlite3"]);
    } finally {
      rmSync(work, { recursive: true, force: true });
    }
  });

  for (const mode of ["WAL", "DELETE"] as const) {
    it(`scrittore concorrente reale (${mode}): ogni lettura riuscita e' coerente, lo scrittore non viene mai fermato`, async () => {
      const work = mkdtempSync(join(tmpdir(), "cloud-finance-ro-race-"));
      try {
        const p = join(work, "race.sqlite3");
        const init = new NodeSqliteDb(p);
        await init.batch([
          stmt("CREATE TABLE t(id INTEGER PRIMARY KEY, v INTEGER NOT NULL, pad TEXT)"),
          stmt("CREATE TABLE c(n INTEGER NOT NULL, s INTEGER NOT NULL)"),
          stmt("INSERT INTO c VALUES (0, 0)"),
        ]);
        init.close();
        const child = spawn(process.execPath, [WRITER, p, mode, "1500"]);
        let out = "";
        child.stdout.on("data", (d: Buffer) => {
          out += d;
        });
        const exited = new Promise<void>((r) => child.on("exit", () => r()));
        let running = true;
        void exited.then(() => {
          running = false;
        });
        const invariant = async (db: Db) => {
          const a = await db.first<{ n: number; s: number }>(
            "SELECT count(*) AS n, coalesce(sum(v), 0) AS s FROM t",
          );
          const b = await db.first<{ n: number; s: number }>("SELECT n, s FROM c");
          return a?.n === b?.n && a?.s === b?.s;
        };
        const seen = { coherent: 0, incoherent: 0, refused: 0 };
        while (running) {
          try {
            (await readImmutable(p, invariant)) ? seen.coherent++ : seen.incoherent++;
          } catch (e) {
            assert.ok(e instanceof SnapshotUnavailableError, String(e)); // mai altri errori
            seen.refused++;
          }
          await new Promise((r) => setImmediate(r));
        }
        await exited;
        const writer = JSON.parse(out) as { tx: number; busy: number };
        assert.equal(seen.incoherent, 0, JSON.stringify(seen));
        assert.ok(seen.coherent > 0, `nessuna lettura riuscita: ${JSON.stringify(seen)}`);
        assert.ok(writer.tx > 100, "lo scrittore ha lavorato davvero");
        assert.equal(writer.busy, 0, "lo scrittore non e' mai stato bloccato dal lettore");
        assert.deepEqual(readdirSync(work).sort(), ["race.sqlite3"], "nessun file lasciato dal lettore");
      } finally {
        rmSync(work, { recursive: true, force: true });
      }
    });
  }

  it("server MCP: tutti gli strumenti in lettura, input ostili rifiutati, cartella identica byte per byte", () => {
    const snap = snapshot(dir);
    const calls = [
      ...OPERATIONS.map((o) => ({ name: `cloud_finance_${o.name}`, arguments: VALID[o.name] ?? {} })),
      { name: "cloud_finance_accounts", arguments: { sql: "DELETE FROM accounts" } },
      {
        name: "cloud_finance_summary",
        arguments: { from: "2026-01-01'; DROP TABLE accounts; --", to: "2026-01-31" },
      },
      { name: "cloud_finance_transactions", arguments: { from: "2026-01-01", to: "2026-01-31", limit: 1e9 } },
      { name: "cloud_finance_../../migrate", arguments: {} },
      { name: "cloud_finance_import", arguments: {} },
    ];
    const out = mcp(path, calls);
    assert.equal(out.length, calls.length);
    for (let i = 0; i < OPERATIONS.length; i++) assert.equal(out[i]?.result?.isError, false, calls[i]?.name);
    const hostile = out.slice(OPERATIONS.length);
    for (const r of hostile.slice(0, 3)) {
      assert.equal(r.result?.isError, true);
      assert.match(r.result?.content[0]?.text ?? "", /invalid_input/);
    }
    for (const r of hostile.slice(3)) assert.ok(r.error !== undefined || r.result?.isError === true);
    assert.deepEqual(snapshot(dir), snap, "nessun -wal/-shm, database identico");
  });

  it("server MCP con scrittura in corso: errore dello strumento, nessun dato, nessun file in piu'", async () => {
    const copy = mkdtempSync(join(tmpdir(), "cloud-finance-ro-mcp-busy-"));
    try {
      const p = join(copy, "c.sqlite3");
      writeFileSync(p, readFileSync(path));
      const writer = new NodeSqliteDb(p);
      await writer.batch([stmt("UPDATE accounts SET name = 'Conto DEMO 3'")]);
      const files = readdirSync(copy).sort();
      const [r] = mcp(p, [{ name: "cloud_finance_accounts", arguments: {} }]);
      assert.equal(r?.result?.isError, true);
      assert.deepEqual(JSON.parse(r?.result?.content[0]?.text ?? "{}").reason, "wal_not_empty");
      assert.deepEqual(readdirSync(copy).sort(), files);
      writer.close();
    } finally {
      rmSync(copy, { recursive: true, force: true });
    }
  });

  it("CLI `query`: stessa apertura immutabile, cartella identica byte per byte", () => {
    const snap = snapshot(dir);
    const r = spawnSync(
      process.execPath,
      [CLI, "--db", path, "query", "summary", "--input", JSON.stringify(VALID.summary)],
      {
        encoding: "utf8",
      },
    );
    assert.equal(r.status, 0, r.stderr);
    assert.equal((JSON.parse(r.stdout) as { ok: boolean }).ok, true);
    assert.deepEqual(snapshot(dir), snap);
  });
});
