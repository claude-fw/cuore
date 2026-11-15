import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readdirSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, it } from "node:test";
import { BACKEND } from "./db-backend.ts";

const ROOT = new URL("..", import.meta.url).pathname;
const CLI = join(ROOT, "src/cli/main.ts");
const EX = join(ROOT, "examples/sintetici");
const dir = mkdtempSync(join(tmpdir(), "cloud-finance-test-"));
const DB = join(dir, "finanza", "test.sqlite3");

after(() => rmSync(dir, { recursive: true, force: true }));

function cli(...args: string[]): { code: number; out: unknown; err: string } {
  const r = spawnSync(process.execPath, [CLI, "--db", DB, ...args], { encoding: "utf8" });
  let out: unknown = null;
  try {
    out = JSON.parse(r.stdout);
  } catch {
    out = r.stdout;
  }
  return { code: r.status ?? -1, out, err: r.stderr };
}

// La CLI usa sempre SQLite via Node (e' lo strumento locale): con il motore D1 questi test non dicono nulla su D1.
describe("CLI end-to-end (dati sintetici, database temporaneo)", {
  skip: BACKEND === "d1" ? "la CLI usa SQLite via Node, non D1" : false,
}, () => {
  it("senza --db non fa nulla", () => {
    const r = spawnSync(process.execPath, [CLI, "status"], {
      encoding: "utf8",
      env: { PATH: process.env.PATH ?? "" },
    });
    assert.equal(r.status, 2);
    assert.match(r.stderr, /--db/);
  });

  it("database non migrato: i comandi si rifiutano", () => {
    const r = cli("status");
    assert.equal(r.code, 2);
    assert.match(r.err, /migrate/);
  });

  it("flusso completo: migrate, conti, import x2, regole, classify, detect, reconcile, report", () => {
    assert.equal(cli("migrate").code, 0);
    assert.equal(statSync(DB).mode & 0o077, 0, "il file del database non e' leggibile da altri utenti");
    assert.equal(
      cli("account:add", "--id", "conto", "--name", "Conto DEMO", "--type", "current", "--currency", "EUR")
        .code,
      0,
    );
    assert.equal(
      cli("account:add", "--id", "carta", "--name", "Carta DEMO", "--type", "card", "--currency", "EUR").code,
      0,
    );

    const first = cli(
      "import",
      "--account",
      "conto",
      "--format",
      "cloud-finance-csv",
      "--file",
      join(EX, "conto-demo.csv"),
    );
    assert.equal(first.code, 0, first.err);
    assert.equal((first.out as { counts: { created: number } }).counts.created, 8);
    const second = cli(
      "import",
      "--account",
      "conto",
      "--format",
      "cloud-finance-csv",
      "--file",
      join(EX, "conto-demo.csv"),
    );
    assert.equal((second.out as { status: string }).status, "duplicate_file");

    cli(
      "import",
      "--account",
      "carta",
      "--format",
      "cloud-finance-csv",
      "--file",
      join(EX, "carta-demo.csv"),
    );
    cli(
      "import",
      "--account",
      "conto",
      "--format",
      "cloud-finance-balances-csv",
      "--file",
      join(EX, "saldi-conto-demo.csv"),
    );
    assert.equal(cli("rules:publish", "--file", join(EX, "regole-demo.json")).code, 0);
    assert.equal(cli("classify").code, 0);
    assert.deepEqual(cli("detect").out, { transfersProposed: 1, refundsProposed: 1, ambiguous: 0 });
    const rec = cli("reconcile", "--account", "conto", "--from", "2026-01-01", "--to", "2026-01-31");
    assert.equal((rec.out as { status: string }).status, "ok");
    const report = cli("report", "--from", "2026-01-01", "--to", "2026-01-31");
    assert.equal(report.code, 0);
    const st = cli("status").out as {
      schemaVersion: number;
      transactions: { booked: number; pending: number };
    };
    assert.equal(st.schemaVersion, 8);
    assert.deepEqual(st.transactions, { booked: 10, pending: 2 });
    const verify = cli("migrate:verify").out as {
      ok: boolean;
      upToDate: boolean;
      schema: { compared: boolean };
    };
    assert.deepEqual([verify.ok, verify.upToDate, verify.schema.compared], [true, true, true]);
  });

  it("crediti e debiti: controparte, apertura, pagamento parziale, doppione, elenco con residuo", () => {
    assert.equal(cli("counterparty:add", "--id", "amico", "--name", "Amico DEMO").code, 0);
    const open = [
      "obligation:add",
      "--counterparty",
      "amico",
      "--direction",
      "receivable",
      "--amount",
      "10.00",
    ];
    const rest = [
      "--currency",
      "EUR",
      "--date",
      "2026-01-20",
      "--due",
      "2026-02-28",
      "--description",
      "Cena DEMO",
    ];
    const created = cli(...open, ...rest);
    assert.equal(created.code, 0, created.err);
    const id = (created.out as { id: string }).id;
    assert.notEqual(cli(...open, ...rest).code, 0, "doppione rifiutato senza --confirm-duplicate");
    const paid = cli(
      "obligation:event",
      "--id",
      id,
      "--kind",
      "payment",
      "--amount",
      "4.00",
      "--date",
      "2026-01-25",
    );
    assert.equal(paid.code, 0, paid.err);
    const bad = cli("obligation:link", "--txn", "inesistente", "--allocation", `${id}:payment:1.00`);
    assert.notEqual(bad.code, 0);
    const list = cli("obligations", "--date", "2026-03-01").out as {
      obligations: { id: string; state: { residual: number; overdue: boolean } }[];
      position: { currency: string; receivable: number; overdueCount: number }[];
    };
    const o = list.obligations.find((x) => x.id === id);
    assert.deepEqual([o?.state.residual, o?.state.overdue], [600, true]);
    assert.deepEqual(list.position, [
      { currency: "EUR", receivable: 600, payable: 0, net: 600, openCount: 1, overdueCount: 1 },
    ]);
    const report = cli("report", "--from", "2026-01-01", "--to", "2026-01-31").out as {
      currencies: { currency: string; obligationsIn: number; obligationsOut: number }[];
    };
    const eur = report.currencies.find((c) => c.currency === "EUR");
    assert.deepEqual(
      [eur?.obligationsIn, eur?.obligationsOut],
      [0, 0],
      "senza movimenti collegati la cassa non cambia",
    );
  });

  it("query: consultazione in sola lettura con busta { ok, data | error } e codici di uscita", () => {
    const before = statSync(DB).mtimeMs;
    const s = cli("query", "summary", "--input", '{"from":"2026-01-01","to":"2026-01-31"}');
    assert.equal(s.code, 0, s.err);
    const out = s.out as {
      ok: boolean;
      contractVersion: number;
      data: { currencies: { currency: string }[] };
    };
    assert.deepEqual([out.ok, out.contractVersion, out.data.currencies[0]?.currency], [true, 1, "EUR"]);
    const page = cli("query", "transactions", "--input", '{"from":"2026-01-01","to":"2026-01-31","limit":2}');
    assert.equal((page.out as { data: { items: unknown[] } }).data.items.length, 2);
    assert.equal(cli("query", "data_status").code, 0, "asOf predefinito: oggi (Europe/Rome)");
    const bad = cli("query", "summary", "--input", '{"from":"2026-01-01"}');
    assert.deepEqual([bad.code, (bad.out as { error: { code: string } }).error.code], [2, "invalid_input"]);
    assert.equal(cli("query", "summary", "--input", "{non json").code, 2);
    assert.equal(cli("query").code, 2);
    assert.equal(statSync(DB).mtimeMs, before, "il file del database non viene modificato");
    const missing = spawnSync(
      process.execPath,
      [CLI, "--db", join(dir, "assente.sqlite3"), "query", "accounts"],
      {
        encoding: "utf8",
      },
    );
    assert.equal(missing.status, 2);
    assert.equal(readdirSync(dir).includes("assente.sqlite3"), false, "nessun database creato");
  });

  it("file non valido: codice di uscita 3, nulla scritto", () => {
    const bad = join(dir, "bad.csv");
    const header = readFileSync(join(EX, "conto-demo.csv"), "utf8").split("\n")[0];
    spawnSync("sh", [
      "-c",
      `printf '%s\\n%s\\n' '${header}' ',2026-13-01,2026-13-01,booked,1.00,EUR,x,,,' > '${bad}'`,
    ]);
    const r = cli("import", "--account", "conto", "--format", "cloud-finance-csv", "--file", bad);
    assert.equal(r.code, 3);
    assert.equal((r.out as { status: string }).status, "rejected");
  });

  it("formati: implementati e predisposti; un formato predisposto non importa e spiega cosa manca", () => {
    const formats = cli("formats").out as { id: string; status: string }[];
    assert.ok(formats.some((f) => f.id === "cloud-finance-csv" && f.status === "implementato"));
    assert.ok(formats.some((f) => f.id === "revolut-personal-statement" && f.status === "predisposto"));
    const info = cli("format:info", "--id", "mps-digital-banking-movimenti").out as {
      verified: unknown[];
      needed: string[];
    };
    assert.ok(info.verified.length > 0 && info.needed.length > 0);
    const r = cli(
      "import",
      "--account",
      "conto",
      "--format",
      "revolut-personal-statement",
      "--file",
      join(EX, "conto-demo.csv"),
    );
    assert.equal(r.code, 2);
    assert.match(r.err, /PREDISPOSTO/);
  });

  it("errori di input: comando sconosciuto, opzione mancante", () => {
    assert.equal(cli("boh").code, 2);
    assert.equal(cli("account:add", "--id", "x").code, 2);
  });
});

describe("portabilita' verso Cloudflare Workers", () => {
  const files = (sub: string): string[] =>
    readdirSync(join(ROOT, "src", sub), { recursive: true, withFileTypes: true })
      .filter((e) => e.isFile() && e.name.endsWith(".ts"))
      .map((e) => join(e.parentPath, e.name));

  it("il nucleo (domain, import, services, db esclusi gli adattatori) non usa API di Node", () => {
    const core = [
      ...files("domain"),
      ...files("import"),
      ...files("services"),
      ...files("query"),
      ...files("db"),
    ].filter((f) => !f.endsWith("node-sqlite.ts"));
    assert.ok(core.length > 15);
    for (const f of core) {
      const src = readFileSync(f, "utf8");
      assert.ok(!/from\s+["']node:/.test(src), `${f} importa un modulo node:`);
      assert.ok(!/\bprocess\./.test(src), `${f} usa process`);
      assert.ok(!/\bBuffer\b/.test(src), `${f} usa Buffer`);
    }
  });
});
