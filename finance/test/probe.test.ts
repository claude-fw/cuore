/**
 * Analisi locale del formato di un export (src/import/probe.ts): solo fixture SINTETICHE. Verifica soprattutto
 * che il testo dei dati non compaia mai, salvo l'intestazione scelta e le colonne richieste entro i limiti.
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, it } from "node:test";
import { maskShape, probeFile } from "../src/import/probe.ts";
import { aFile, bFile } from "./fixtures/synthetic-formats.ts";
import { buildXlsx } from "./xlsx-builder.ts";

const SECRET = ["PERSONA FITTIZIA", "Caffè Bar Esempio", "Edicola Esempio", "1.234,56", "3,50"];
const B = bFile([
  "05/01/2026;05/01/2026;Caffè Bar Esempio;3,50;;",
  "06/01/2026;06/01/2026;Edicola Esempio;2,00;;nota",
  "07/01/2026;07/01/2026;Bonifico in entrata DEMO;;1.200,00;",
]);
const noSecrets = (text: string) => {
  for (const s of SECRET) assert.ok(!text.includes(s), `trapelato: ${s}`);
};

describe("analisi locale del formato (probe)", () => {
  it("forma: cifre 9, maiuscole A, minuscole a, spazi compressi, accenti trattati come lettere", () => {
    assert.equal(maskShape("  Caffè  Bar 12,50 € "), "Aaaaa Aaa 99,99 €");
    assert.equal(maskShape("2026-01-05 14:03:00"), "9999-99-99 99:99:99");
    assert.equal(maskShape("x".repeat(60)).length, 41);
  });

  it("passo 1 su CSV windows-1252: codifica, separatore, fine riga e SOLE forme (nessun testo dei dati)", async () => {
    const r = await probeFile(B);
    assert.equal(r.file.kind, "text");
    assert.match(r.file.encoding ?? "", /non UTF-8/);
    assert.equal(r.file.delimiterUsed, ";");
    assert.equal(r.file.lineEndings, "CRLF");
    assert.equal(
      r.rows?.first[3]?.shapes.join("|"),
      "Aaaa aaaaaaaaa|Aaaa aaaaaa|Aaaaaaaaaaa|Aaaa|Aaaaa|Aaaa",
    );
    assert.equal(r.header, null);
    noSecrets(JSON.stringify(r));
  });

  it("passo 2: con la riga d'intestazione mostra i nomi delle colonne e le forme per colonna, non i valori", async () => {
    const r = await probeFile(B, { headerRow: 4 });
    assert.deepEqual(r.header?.names, [
      "Data contabile",
      "Data valuta",
      "Descrizione",
      "Dare",
      "Avere",
      "Note",
    ]);
    const col = (name: string) => r.header?.columns.find((c) => c.name === name);
    assert.deepEqual(col("Dare")?.topShapes, [{ shape: "9,99", count: 2 }]);
    // anche la riga di chiusura ("Saldo contabile ... 1.234,56") e' tra le righe dopo l'intestazione: stessa forma
    assert.deepEqual(col("Avere")?.topShapes, [{ shape: "9.999,99", count: 2 }]);
    assert.deepEqual(r.rows?.last.length, 0, "meno di 20 righe: nessun elenco separato delle ultime");
    assert.ok(
      r.header?.columns
        .find((c) => c.name === "Data contabile")
        ?.topShapes.some((s) => s.shape === "99/99/9999"),
    );
    noSecrets(JSON.stringify(r));
  });

  it("passo 3: valori distinti solo per poche voci; valori con cifre mascherati; colonne troppo varie rifiutate", async () => {
    const statusFile = aFile([
      { id: "P1", op: "2026-01-05 09:00:00", amount: "-1.00", stato: "COMPLETATA" },
      { id: "P2", op: "2026-01-05 10:00:00", amount: "-2.00", stato: "IN_ATTESA" },
      { id: "P3", op: "2026-01-05 11:00:00", amount: "-3.00", stato: "ANNULLATA" },
    ]);
    const r = await probeFile(statusFile, { headerRow: 1, valuesOf: ["Stato", "ID", "Inesistente"] });
    assert.deepEqual(r.values.Stato, { values: ["ANNULLATA", "COMPLETATA", "IN_ATTESA"] });
    assert.deepEqual(r.values.ID, { values: ["A9"] }, "valori con cifre mascherati (e forme uguali unite)");
    assert.deepEqual(r.values.Inesistente, { refused: "colonna non presente nell'intestazione" });

    const many = aFile(
      Array.from({ length: 16 }, (_, i) => ({
        id: `M${i}`,
        op: "2026-01-05 09:00:00",
        amount: "-1.00",
        desc: `Negozio ${String.fromCharCode(65 + i)}`,
      })),
    );
    const rm = await probeFile(many, { headerRow: 1, valuesOf: ["Descrizione"] });
    assert.match((rm.values.Descrizione as { refused: string }).refused, /16 valori distinti/);
    assert.ok(!JSON.stringify(rm).includes("Negozio A"));
    const without = await probeFile(statusFile, { valuesOf: ["Stato"] });
    assert.equal(without.values.Stato, undefined);
    assert.ok(without.warnings.some((w) => /richiede headerRow/.test(w)));
  });

  it("XLSX: tipi di cella per colonna (numeri, date come numeri, testo) e nessun valore in chiaro", async () => {
    const x = buildXlsx([
      {
        name: "Movimenti",
        rows: [
          ["Intestatario: PERSONA FITTIZIA"],
          ["Data", "Descrizione", "Importo"],
          [46027, "Caffè Bar Esempio", -3.5],
          ["06/01/2026", "Edicola Esempio", -2],
        ],
      },
    ]);
    const r = await probeFile(x, { headerRow: 2 });
    assert.equal(r.file.kind, "zip");
    assert.equal(r.file.sheet, "Aaaaaaaaa");
    const data = r.header?.columns.find((c) => c.name === "Data");
    assert.deepEqual(data?.types, { number: 1, string: 1 });
    assert.ok(data?.topShapes.some((s) => s.shape === "number:99999"));
    noSecrets(JSON.stringify(r));
  });

  it("formati non leggibili: .xls binario, HTML, PDF riconosciuti con un avviso e nessuna riga letta", async () => {
    for (const [bytes, pattern] of [
      [Uint8Array.from([0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1, 0, 0]), /Excel 97-2003/],
      [new TextEncoder().encode("<html><table><tr><td>x</td></tr></table></html>"), /HTML/],
      [new TextEncoder().encode("%PDF-1.7"), /PDF/],
    ] as const) {
      const r = await probeFile(bytes);
      assert.equal(r.rows, null);
      assert.ok(r.warnings.some((w) => pattern.test(w)));
    }
  });

  it("CLI: solo lettura, JSON su stdout, promemoria su stderr", () => {
    const dir = mkdtempSync(join(tmpdir(), "cloud-finance-probe-"));
    after(() => rmSync(dir, { recursive: true, force: true }));
    const f = join(dir, "export-sintetico.csv");
    writeFileSync(f, B);
    const r = spawnSync(
      process.execPath,
      [new URL("../scripts/probe-format.ts", import.meta.url).pathname, "--file", f, "--header-row", "4"],
      {
        encoding: "utf8",
      },
    );
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.stderr, /Ricontrolla l'output/);
    const out = JSON.parse(r.stdout) as { header: { names: string[] } };
    assert.equal(out.header.names[2], "Descrizione");
    noSecrets(r.stdout);
  });
});
