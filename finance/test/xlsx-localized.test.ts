import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { describe, it } from "node:test";
import { crc32 as zlibCrc32 } from "node:zlib";
import {
  calendarDateIn,
  parseDate,
  parseDateTime,
  parseLocalizedAmount,
  parseSpreadsheetNumber,
  spreadsheetSerialToDate,
} from "../src/domain/localized.ts";
import { readSheet, XlsxError } from "../src/import/xlsx/sheet.ts";
import { parseXml, XmlError } from "../src/import/xlsx/xml.ts";
import { crc32 } from "../src/import/xlsx/zip.ts";
import { buildXlsx, buildXlsxParts, zip } from "./xlsx-builder.ts";

const IT = { decimal: ",", thousands: "." } as const;
const US = { decimal: ".", thousands: "," } as const;

describe("importi localizzati", () => {
  it("formati italiano e anglosassone, con e senza migliaia", () => {
    assert.equal(parseLocalizedAmount("1.234,56", "EUR", IT), 123456);
    assert.equal(parseLocalizedAmount("-1.234.567,8", "EUR", IT), -123456780);
    assert.equal(parseLocalizedAmount("45,20", "EUR", IT), 4520);
    assert.equal(parseLocalizedAmount("7", "EUR", IT), 700);
    assert.equal(parseLocalizedAmount("1,234.56", "EUR", US), 123456);
    assert.equal(parseLocalizedAmount("1234.56", "EUR", { decimal: ".", thousands: null }), 123456);
  });

  it("rifiuta gruppi di migliaia sbagliati, simboli, segni non previsti, decimali in eccesso", () => {
    for (const bad of [
      "1.23",
      "1.2345,00",
      "€ 12,00",
      "12,00 €",
      "+12,00",
      "(12,00)",
      "12,",
      ",5",
      "1 234,00",
      "--1",
      "",
    ]) {
      assert.throws(() => parseLocalizedAmount(bad, "EUR", IT), Error, bad);
    }
    assert.throws(() => parseLocalizedAmount("1,505", "EUR", IT), /troppi decimali/);
    assert.throws(
      () => parseLocalizedAmount("1.234", "EUR", { decimal: ".", thousands: null }),
      /troppi decimali/,
    );
  });

  it("numeri binari dei fogli: solo rumore di virgola mobile viene arrotondato", () => {
    assert.equal(parseSpreadsheetNumber("-45.200000000000003", "EUR"), -4520);
    assert.equal(parseSpreadsheetNumber("0.30000000000000004", "EUR"), 30);
    assert.equal(parseSpreadsheetNumber("1200", "EUR"), 120000);
    assert.equal(parseSpreadsheetNumber("1.5E3", "EUR"), 150000);
    assert.throws(() => parseSpreadsheetNumber("45.205", "EUR"), /troppi decimali/);
    assert.throws(() => parseSpreadsheetNumber("abc", "EUR"), /non valido/);
  });
});

describe("date e fusi", () => {
  it("formati dichiarati, date impossibili e date ambigue rifiutate", () => {
    assert.equal(parseDate("05/01/2026", "DD/MM/YYYY"), "2026-01-05");
    assert.equal(parseDate("05.01.2026", "DD.MM.YYYY"), "2026-01-05");
    assert.equal(parseDate("2026-01-05", "YYYY-MM-DD"), "2026-01-05");
    assert.equal(parseDate("01/13/2026", "DD/MM/YYYY"), null, "mese 13: formato americano non indovinato");
    assert.equal(parseDate("31/02/2026", "DD/MM/YYYY"), null);
    assert.equal(parseDate("5/1/2026", "DD/MM/YYYY"), null, "niente cifre singole");
    assert.equal(parseDate("05/01/26", "DD/MM/YYYY"), null, "niente anni a due cifre");
    assert.equal(parseDate("2026-01-05", "DD/MM/YYYY"), null);
  });

  it("data e ora; conversione UTC -> Europe/Rome attraverso la mezzanotte e l'ora legale", () => {
    const dt = parseDateTime("2025-12-31 23:30:00", "YYYY-MM-DD HH:mm:ss");
    assert.ok(dt);
    assert.equal(calendarDateIn(dt, "UTC", "Europe/Rome"), "2026-01-01");
    assert.equal(calendarDateIn(dt, "Europe/Rome", "Europe/Rome"), "2025-12-31");
    const summer = parseDateTime("2026-07-01T22:30:00", "YYYY-MM-DDTHH:mm:ss");
    assert.ok(summer);
    assert.equal(calendarDateIn(summer, "UTC", "Europe/Rome"), "2026-07-02"); // UTC+2
    const winter = parseDateTime("2026-01-15 22:30:00", "YYYY-MM-DD HH:mm:ss");
    assert.ok(winter);
    assert.equal(calendarDateIn(winter, "UTC", "Europe/Rome"), "2026-01-15"); // UTC+1 -> 23:30
    assert.equal(parseDateTime("2026-01-15 24:00:00", "YYYY-MM-DD HH:mm:ss"), null);
    assert.equal(parseDateTime("05/01/2026 14:03", "DD/MM/YYYY HH:mm")?.date, "2026-01-05");
    assert.throws(() => calendarDateIn(winter, "America/New_York", "Europe/Rome"), /non supportato/);
  });

  it("numeri di serie dei fogli (sistema 1900 e 1904)", () => {
    assert.equal(spreadsheetSerialToDate("45658", false), "2025-01-01");
    assert.equal(spreadsheetSerialToDate("44196", true), "2025-01-01");
    assert.equal(spreadsheetSerialToDate("45658.5", false), null, "parte oraria: fuso non dichiarato");
    assert.equal(spreadsheetSerialToDate("60", false), null, "29/02/1900 inesistente");
  });
});

describe("XML minimo", () => {
  it("entita', CDATA, commenti, namespace", () => {
    const el = parseXml(
      '<?xml version="1.0"?><!-- c --><x:a k="1 &amp; 2"><b>&lt;t&gt;&#233;&#xE8;</b><![CDATA[<raw>]]></x:a>',
    );
    assert.equal(el.name, "a");
    assert.equal(el.attrs.k, "1 & 2");
    assert.deepEqual(el.children, [{ name: "b", attrs: {}, children: ["<t>éè"] }, "<raw>"]);
  });

  it("rifiuta DTD con entita', annidamento errato, entita' sconosciute", () => {
    assert.throws(() => parseXml('<!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>'), XmlError);
    assert.throws(() => parseXml("<a><b></a></b>"), XmlError);
    assert.throws(() => parseXml("<a>&nbsp;</a>"), XmlError);
    assert.throws(() => parseXml("<a>"), XmlError);
  });
});

describe("lettore XLSX", () => {
  it("CRC32 coincide con quello di zlib", () => {
    for (let i = 0; i < 20; i += 1) {
      const b = new Uint8Array(randomBytes(1 + i * 37));
      assert.equal(crc32(b), zlibCrc32(b));
    }
  });

  for (const compress of [true, false]) {
    it(`celle di ogni tipo, celle mancanti, testo ricco, fonetica esclusa (${compress ? "deflate" : "stored"})`, async () => {
      const bytes = buildXlsx(
        [
          {
            name: "Movimenti",
            rows: [
              ["Data", null, "Importo", { inline: "Nota & co" }],
              [45658, { rich: ["Caff", "è"], phonetic: "KA" }, -45.2, { bool: true }],
              [{ error: "#N/A" }, "x", { raw: "abc", type: "str" }],
            ],
            rowNumbers: [1, 2, 5],
          },
        ],
        { compress },
      );
      const sheet = await readSheet(bytes);
      assert.equal(sheet.name, "Movimenti");
      assert.equal(sheet.date1904, false);
      assert.deepEqual(
        sheet.rows.map((r) => [r.line, r.cells.map((c) => `${c.type}:${c.text}`)]),
        [
          [1, ["string:Data", "empty:", "string:Importo", "string:Nota & co"]],
          [2, ["number:45658", "string:Caffè", "number:-45.2", "boolean:TRUE"]],
          [5, ["error:#N/A", "string:x", "string:abc"]],
        ],
      );
    });
  }

  it("foglio per nome e sistema di date 1904", async () => {
    const bytes = buildXlsx(
      [
        { name: "Primo", rows: [["a"]] },
        { name: "Secondo", rows: [["b"]] },
      ],
      { date1904: true },
    );
    const s = await readSheet(bytes, "Secondo");
    assert.equal(s.rows[0]?.cells[0]?.text, "b");
    assert.equal(s.date1904, true);
    await assert.rejects(readSheet(bytes, "Terzo"), /foglio non trovato/);
  });

  it("errori espliciti: non ZIP, CRC errato, cifrato, metodo ignoto, parti mancanti, stringa condivisa inesistente", async () => {
    await assert.rejects(readSheet(new TextEncoder().encode("a,b\n1,2\n")), XlsxError);
    const ok = buildXlsx([{ name: "S", rows: [["abc"]] }], { compress: false });
    const tampered = new Uint8Array(ok);
    const at = Buffer.from(tampered).indexOf("sheetData");
    tampered[at] = "S".charCodeAt(0);
    await assert.rejects(readSheet(tampered), /CRC errato/);
    await assert.rejects(
      readSheet(buildXlsx([{ name: "S", rows: [["a"]] }], { encrypted: true })),
      /cifrata/,
    );
    await assert.rejects(
      readSheet(buildXlsx([{ name: "S", rows: [["a"]] }], { compress: false, method: 12 })),
      /metodo/,
    );
    const parts = buildXlsxParts([{ name: "S", rows: [["a"]] }]);
    parts.delete("xl/workbook.xml");
    await assert.rejects(readSheet(zip(parts as Map<string, string>)), /parte mancante/);
    const bad = buildXlsx([{ name: "S", rows: [[{ raw: "99", type: "s" }]] }]);
    await assert.rejects(readSheet(bad), /stringa condivisa inesistente/);
    await assert.rejects(
      readSheet(buildXlsx([{ name: "S", rows: [[{ raw: "2026-01-01", type: "d" }]] }])),
      /t="d"/,
    );
  });
});
