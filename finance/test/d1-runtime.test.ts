/**
 * Nucleo eseguito DENTRO workerd (runtime Cloudflare locale) con D1 locale, senza nodejs_compat.
 * Il Worker di prova (test/d1-runtime/worker.ts) e' impacchettato con esbuild per piattaforma "browser":
 * qualsiasi import `node:` nel nucleo farebbe fallire l'impacchettamento. Eseguito con CLOUD_FINANCE_TEST_DB=d1.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import { build } from "esbuild";
import { BACKEND, startMiniflare } from "./db-backend.ts";
import { aFile, bFile } from "./fixtures/synthetic-formats.ts";
import { buildXlsx, type XCell } from "./xlsx-builder.ts";

/** Rapporto JSON restituito dal Worker di prova (vedi test/d1-runtime/worker.ts). */
interface RuntimeReport {
  runtime: { navigator: string | null; hasProcess: boolean };
  migrate1: unknown;
  migrate2: unknown;
  neutral: unknown;
  specA: { status: string };
  specAdates: {
    source_txn_id: string;
    transaction_date: string;
    amount_minor: number;
    source_type: string;
  }[];
  excluded: unknown;
  specB: { status: string };
  specBtext: { description: string } | null;
  specC: { status: string; counts: { created: number; unchanged: number } };
  classify: { examined: number };
  detect: unknown;
  reconcile: { status: string; difference: number | null };
  report: { currency: string; transfersIn: number; refunds: number }[];
  consultation: {
    summarySameAsReport: boolean;
    pages: number;
    distinctIds: boolean;
    listed: number;
    unknownOperation: boolean;
  };
  immutability: Record<string, string>;
  rollback: unknown;
  conflict: string;
  integrity: unknown;
}

const SKIP = BACKEND === "d1" ? false : "solo con CLOUD_FINANCE_TEST_DB=d1 (richiede workerd)";
const ex = (name: string) => readFileSync(new URL(`../examples/sintetici/${name}`, import.meta.url));
const b64 = (bytes: Uint8Array) => Buffer.from(bytes).toString("base64");

function payload() {
  const xlsxRows: XCell[][] = [
    ["Lista movimenti DEMO"],
    [],
    ["Data contabile", "Data valuta", "Descrizione", "Dare", "Avere"],
    [46058, 46058, "Caffè Bar Esempio", 3.5, null],
    ["06/02/2026", "06/02/2026", "Bonifico in entrata DEMO", null, 150],
    [46060, 46060, "Spesa supermercato", { raw: "1045.2000000000001", type: "n" }, null], // 07/02/2026
    [],
    ["Saldo contabile", null, null, null, 1234.56],
  ];
  return {
    files: {
      "conto-demo.csv": b64(ex("conto-demo.csv")),
      "carta-demo.csv": b64(ex("carta-demo.csv")),
      "saldi-conto-demo.csv": b64(ex("saldi-conto-demo.csv")),
      "a.csv": b64(
        aFile([
          { id: "W1", op: "2025-12-31 23:30:00", amount: "-12.00", desc: "Cena" },
          { id: "W2", op: "2026-01-03 10:00:00", amount: "-33.30", fee: "0.25" },
          { id: "W3", op: "2026-01-05 09:00:00", stato: "ANNULLATA", amount: "-4.00" },
          { id: "W4", op: "2026-01-20 09:00:00", stato: "IN_ATTESA", amount: "-7.70" },
        ]),
      ),
      "b.csv": b64(
        bFile([
          "05/02/2026;05/02/2026;Caffè Bar Esempio;3,50;;",
          "06/02/2026;06/02/2026;Bonifico in entrata DEMO;;150,00;",
          "07/02/2026;07/02/2026;Spesa supermercato;1.045,20;;",
        ]),
      ),
      "c.xlsx": b64(buildXlsx([{ name: "Movimenti", rows: xlsxRows }])),
    },
    rules: JSON.parse(ex("regole-demo.json").toString("utf8")) as unknown,
  };
}

describe("nucleo dentro workerd con D1 locale", { skip: SKIP }, () => {
  it("scenario completo: migrazioni, import (neutro, CSV UTC, CSV windows-1252, XLSX), regole, collegamenti, riconciliazione, immutabilita', rollback, concorrenza", async () => {
    const bundle = await build({
      entryPoints: [new URL("./d1-runtime/worker.ts", import.meta.url).pathname],
      bundle: true,
      format: "esm",
      platform: "browser",
      target: "es2024",
      write: false,
      logLevel: "silent",
    });
    const script = bundle.outputFiles[0]?.text ?? "";
    assert.ok(script.length > 10_000);
    assert.ok(!/from\s*["']node:/.test(script), "nessun modulo node: nel pacchetto del Worker");

    const mf = await startMiniflare({
      modules: true,
      script,
      compatibilityDate: "2026-09-01",
      d1Databases: { DB: "runtime" },
    });
    const res = await mf.dispatchFetch("http://worker.local/", {
      method: "POST",
      body: JSON.stringify(payload()),
    });
    const r = (await res.json()) as RuntimeReport;
    assert.equal(res.status, 200, JSON.stringify(r));

    // prova che il codice gira in workerd (non in Node); nessun flag di compatibilita' passato a Miniflare.
    // Nota verificata: anche senza nodejs_compat workerd espone un oggetto globale `process` (non usato dal nucleo).
    assert.equal(r.runtime.navigator, "Cloudflare-Workers");
    assert.deepEqual(r.migrate1, { from: 0, to: 8, applied: [1, 2, 3, 4, 5, 6, 7, 8] });
    assert.deepEqual(r.migrate2, { from: 8, to: 8, applied: [] });
    assert.deepEqual(r.neutral, { first: "committed", created: 8, second: "duplicate_file" });

    assert.equal(r.specA.status, "committed", JSON.stringify(r.specA));
    assert.deepEqual(
      r.specAdates.map((t) => [t.source_txn_id, t.transaction_date, t.amount_minor, t.source_type]),
      [
        ["W1", "2026-01-01", -1200, "PAGAMENTO"],
        ["W2", "2026-01-03", -3330, "PAGAMENTO"],
        ["W2#fee", "2026-01-03", -25, "derived:fee"],
        ["W4", "2026-01-20", -770, "PAGAMENTO"],
      ],
      "fuso UTC -> Europe/Rome calcolato con Intl dentro workerd",
    );
    assert.deepEqual(r.excluded, [{ source_txn_id: "W3", reason: "operazione annullata dalla fonte" }]);

    assert.equal(r.specB.status, "committed", JSON.stringify(r.specB));
    assert.equal(r.specBtext?.description, "Caffè Bar Esempio", "TextDecoder windows-1252 in workerd");
    assert.equal(r.specC.status, "committed", JSON.stringify(r.specC));
    assert.deepEqual(
      [r.specC.counts.created, r.specC.counts.unchanged],
      [0, 3],
      "XLSX (DecompressionStream) = stessi movimenti del CSV",
    );

    assert.ok(r.classify.examined > 0);
    assert.deepEqual(r.detect, { transfersProposed: 1, refundsProposed: 1, ambiguous: 0 });
    assert.equal(r.reconcile.status, "ok");
    assert.equal(r.reconcile.difference, 0);
    const eur = r.report.find((c) => c.currency === "EUR");
    assert.equal(eur?.transfersIn, 20000);
    assert.equal(eur?.refunds, 2765);
    assert.equal(r.consultation.summarySameAsReport, true, "consultazione = riepilogo anche in workerd");
    assert.ok(r.consultation.pages >= 2, "paginazione con cursore dentro workerd");
    assert.equal(r.consultation.distinctIds, true);
    assert.ok(r.consultation.listed >= 6);
    assert.equal(r.consultation.unknownOperation, false);

    for (const [sql, outcome] of Object.entries(r.immutability)) {
      assert.notEqual(outcome, "ACCETTATA", sql);
      assert.match(outcome, /D1_ERROR/, sql);
    }
    assert.deepEqual(r.rollback, {
      status: "rejected",
      issue: "write_failed",
      transactionsUnchanged: true,
      rawUnchanged: true,
    });
    assert.equal(r.conflict, "ConflictError");
    assert.deepEqual(r.integrity, { quick: "ok", foreignKeyViolations: 0 });
  });
});
