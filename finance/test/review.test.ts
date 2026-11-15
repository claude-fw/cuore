/**
 * Revisione assistita (scripts/review.ts) con dati SINTETICI su un database temporaneo: esportazione dei movimenti da
 * classificare, delle proposte e dei casi ambigui; validazione e simulazione delle decisioni senza scritture.
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import { migrate } from "../src/db/migrate.ts";
import { MIGRATIONS } from "../src/db/migrations/index.ts";
import { NodeSqliteDb } from "../src/db/node-sqlite.ts";
import { createAccount, createInstitution } from "../src/services/accounts.ts";
import { classifyTransactions, publishRuleSet } from "../src/services/classification.ts";
import { detectLinks } from "../src/services/links.ts";
import { BACKEND, importTx, row, testCtx } from "./helpers.ts";

const SCRIPT = new URL("../scripts/review.ts", import.meta.url).pathname;
const RULES = {
  schema: "cloud-finance-rules/1",
  note: "SINTETICHE",
  categories: [{ id: "regali", name: "Regali", parent: null, budget_group: null }],
  rules: [
    {
      id: "entrata-persona",
      priority: 10,
      description: "entrata da persona: origine da confermare",
      when: { all: [{ field: "description", op: "starts_with", value: "pagamento da parte di" }] },
      assign: { kind: "income", category: "regali", confidence: "low" },
    },
  ],
};

async function makeDb(dir: string): Promise<string> {
  const path = join(dir, "prova.sqlite3");
  const db = new NodeSqliteDb(path);
  const ctx = testCtx(db);
  const env = { db, ctx };
  try {
    await migrate(db, MIGRATIONS, ctx.now);
    await createInstitution(ctx, "base", "Banca DEMO A");
    await createInstitution(ctx, "spesa", "Banca DEMO B");
    await createAccount(ctx, {
      id: "base",
      name: "base",
      type: "current",
      currency: "EUR",
      holderRole: "other",
      institutionId: "base",
    });
    await createAccount(ctx, {
      id: "spesa",
      name: "spesa",
      type: "current",
      currency: "EUR",
      holderRole: "other",
      institutionId: "spesa",
    });
    await importTx(env, "base", [
      row("2026-02-11", "-200.00", "BONIFICO SEPA DEMO CAUS: 048"),
      row("2026-03-02", "-150.00", "BONIFICO SEPA DEMO CAUS: 048"),
      row("2026-03-10", "-64.30", "GIROCONTO DEMO CAUS: 051 rimborso quote"),
    ]);
    await importTx(env, "spesa", [
      row("2026-02-10", "200.00", "Pagamento da parte di Persona DEMO"),
      row("2026-03-03", "150.00", "Pagamento da parte di Persona DEMO"),
      row("2026-03-04", "150.00", "Pagamento da parte di Persona DEMO"),
      row("2026-03-20", "-12.00", "Pagamento a favore di Amico DEMO"),
    ]);
    await publishRuleSet(ctx, RULES);
    await classifyTransactions(ctx);
    await detectLinks(ctx);
  } finally {
    db.close();
  }
  return path;
}

const run = (args: string[]) => {
  const r = spawnSync(process.execPath, [SCRIPT, ...args], { encoding: "utf8" });
  return { code: r.status, out: r.stdout, err: r.stderr };
};

describe("revisione assistita (scripts/review.ts)", {
  skip: BACKEND === "d1" ? "lo script usa SQLite via Node" : false,
}, () => {
  it("export: movimenti uno per uno, proposte e casi ambigui separati, decisioni vuote, file a 600 in cartella a 700", async () => {
    const dir = mkdtempSync(join(tmpdir(), "cloud-finance-review-test-"));
    try {
      const db = await makeDb(dir);
      const out = join(dir, "revisione");
      const r = run(["export", "--db", db, "--out", out]);
      assert.equal(r.code, 0, r.err);
      const res = JSON.parse(r.out) as {
        decisioni: string;
        scheda: string;
        movimenti: number;
        abbinamenti: number;
        ambigui: number;
      };
      assert.deepEqual([res.movimenti, res.abbinamenti, res.ambigui], [7, 1, 1]);
      assert.equal(statSync(out).mode & 0o777, 0o700);
      for (const f of readdirSync(out)) assert.equal(statSync(join(out, f)).mode & 0o777, 0o600, f);
      const file = JSON.parse(readFileSync(res.decisioni, "utf8")) as {
        movimenti: {
          descrizioneOriginale: string;
          proposta: { tipo: string } | null;
          decisione: unknown;
          evidenze: string[];
          vediAnche: string | null;
        }[];
        abbinamentiProposti: { decisione: unknown; evidenze: string[] }[];
        casiAmbigui: { candidati: unknown[]; decisione: unknown }[];
      };
      assert.ok(file.movimenti.every((m) => m.decisione === null));
      assert.ok(file.abbinamentiProposti.every((l) => l.decisione === null));
      const debt = file.movimenti.find((m) => /rimborso quote/.test(m.descrizioneOriginale));
      assert.equal(debt?.proposta, null, "nessuna proposta inventata");
      assert.ok(debt?.evidenze.some((e) => /saldo di debiti/.test(e)));
      const person = file.movimenti.filter((m) => m.proposta?.tipo === "income");
      assert.equal(person.length, 3, "proposta solo dalla regola a bassa confidenza");
      assert.ok(file.abbinamentiProposti[0]?.evidenze.some((e) => /non dimostrano/.test(e)));
      assert.equal(file.casiAmbigui[0]?.candidati.length, 2);
      assert.ok(file.movimenti.some((m) => m.vediAnche === "caso ambiguo 1"));
      assert.match(readFileSync(res.scheda, "utf8"), /## Casi ambigui \(1\)/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("check: decisioni non valide rifiutate; valide simulate su copia con effetto sul riepilogo, database invariato", async () => {
    const dir = mkdtempSync(join(tmpdir(), "cloud-finance-review-test-"));
    try {
      const db = await makeDb(dir);
      const before = readFileSync(db);
      const out = join(dir, "revisione");
      const res = JSON.parse(run(["export", "--db", db, "--out", out]).out) as { decisioni: string };
      const file = JSON.parse(readFileSync(res.decisioni, "utf8"));
      const debt = file.movimenti.find((m: { descrizioneOriginale: string }) =>
        /rimborso quote/.test(m.descrizioneOriginale),
      );
      const write = (f: object) => {
        const p = join(out, "compilato.json");
        writeFileSync(p, JSON.stringify(f));
        return p;
      };
      debt.decisione = { tipo: "income", categoria: null };
      let r = run(["check", "--db", db, "--decisions", write(file)]);
      assert.equal(r.code, 1);
      assert.match(r.out, /income su un'uscita/);
      debt.decisione = { tipo: "expense", categoria: "inesistente" };
      assert.match(run(["check", "--db", db, "--decisions", write(file)]).out, /categoria sconosciuta/);
      file.casiAmbigui[0].decisione = { scegli: "non-esiste" };
      debt.decisione = { tipo: "expense", categoria: null };
      assert.match(run(["check", "--db", db, "--decisions", write(file)]).out, /candidato scelto/);
      file.casiAmbigui[0].decisione = null;
      file.abbinamentiProposti[0].decisione = "rifiuta";
      r = run(["check", "--db", db, "--decisions", write(file)]);
      assert.equal(r.code, 0, r.out + r.err);
      const sim = JSON.parse(r.out) as {
        esito: string;
        riepilogoPrima: { spending: { purchases: number } }[];
        riepilogoDopo: { spending: { purchases: number } }[];
      };
      assert.match(sim.esito, /simulato su copia/);
      assert.equal(
        sim.riepilogoDopo[0]?.spending.purchases,
        (sim.riepilogoPrima[0]?.spending.purchases ?? 0) + 6430,
      );
      assert.ok(Buffer.compare(before, readFileSync(db)) === 0, "database invariato");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
