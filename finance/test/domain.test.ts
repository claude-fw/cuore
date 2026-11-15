import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { CsvError, parseCsv } from "../src/domain/csv.ts";
import { goalProgress } from "../src/domain/goals.ts";
import { canonicalJson, sha256Hex } from "../src/domain/hash.ts";
import { addMinor, convertMinor, formatMinor, MoneyError, parseAmountToMinor } from "../src/domain/money.ts";
import { reconcile } from "../src/domain/reconcile.ts";
import { budgetShares, type SummaryItem, summarize } from "../src/domain/summary.ts";
import { isIsoDate, normalizeText } from "../src/domain/text.ts";

describe("importi", () => {
  it("conversione testo -> unita' minime senza virgola mobile", () => {
    assert.equal(parseAmountToMinor("12.30", "EUR"), 1230);
    assert.equal(parseAmountToMinor("-0.01", "EUR"), -1);
    assert.equal(parseAmountToMinor("-0.00", "EUR"), 0);
    assert.equal(parseAmountToMinor("7", "EUR"), 700);
    assert.equal(parseAmountToMinor("0.1", "EUR"), 10);
    assert.equal(parseAmountToMinor("1500", "JPY"), 1500);
    assert.equal(parseAmountToMinor("0.29", "EUR") + parseAmountToMinor("0.01", "EUR"), 30);
  });

  it("rifiuta formati ambigui, decimali in eccesso, valute sconosciute, valori enormi", () => {
    for (const bad of ["1,50", "1.234,56", "+1.00", "1e3", " 1.00", "", "-", ".5", "1."]) {
      assert.throws(() => parseAmountToMinor(bad, "EUR"), MoneyError, bad);
    }
    assert.throws(() => parseAmountToMinor("1.005", "EUR"), /troppi decimali/);
    assert.throws(() => parseAmountToMinor("1.5", "JPY"), /troppi decimali/);
    assert.throws(() => parseAmountToMinor("1.00", "XYZ"), /valuta non supportata/);
    assert.throws(() => parseAmountToMinor("99999999999999999999.00", "EUR"), /fuori intervallo/);
    assert.throws(() => addMinor(Number.MAX_SAFE_INTEGER, 1), /fuori intervallo/);
  });

  it("formattazione", () => {
    assert.equal(formatMinor(-1230, "EUR"), "-12.30");
    assert.equal(formatMinor(5, "EUR"), "0.05");
    assert.equal(formatMinor(1500, "JPY"), "1500");
  });

  it("conversione di valuta con aritmetica intera e arrotondamento meta' lontano da zero", () => {
    assert.equal(convertMinor(10000, "USD", "EUR", "0.9234"), 9234);
    assert.equal(convertMinor(-3000, "USD", "EUR", "0.92166"), -2765); // -27.6498 -> -27.65
    assert.equal(convertMinor(1, "EUR", "USD", "1.5"), 2); // 0.015 -> 0.02
    assert.equal(convertMinor(-1, "EUR", "USD", "1.5"), -2);
    assert.equal(convertMinor(1000, "EUR", "JPY", "160.25"), 1603); // 10.00 EUR -> 1602.5 JPY -> 1603
    assert.throws(() => convertMinor(100, "EUR", "USD", "0"), /tasso nullo/);
    assert.throws(() => convertMinor(100, "EUR", "USD", "-1.1"), /tasso non valido/);
  });
});

describe("CSV", () => {
  it("apici, apici doppi, virgole e a capo nei campi, CRLF, BOM, numeri di riga", () => {
    const rows = parseCsv('﻿a,b\r\n"x, y","say ""hi"""\r\n"multi\nline",z\r\n');
    assert.deepEqual(
      rows.map((r) => [r.line, r.fields]),
      [
        [1, ["a", "b"]],
        [2, ["x, y", 'say "hi"']],
        [3, ["multi\nline", "z"]],
      ],
    );
  });

  it("errori strutturali con numero di riga", () => {
    assert.throws(() => parseCsv('a,b\n"aperto,c\n'), CsvError);
    assert.throws(() => parseCsv('a,b\nx"y,z\n'), /apice in mezzo/);
    assert.throws(() => parseCsv('a,b\n"x"y,z\n'), /dopo un campo tra apici/);
  });
});

describe("testo, date e hash", () => {
  it("normalizzazione e date di calendario", () => {
    assert.equal(normalizeText("  Bar   ESEMPIO\t"), "bar esempio");
    assert.ok(isIsoDate("2024-02-29"));
    assert.ok(!isIsoDate("2026-02-29"));
    assert.ok(!isIsoDate("2026-02-30"));
    assert.ok(!isIsoDate("02/01/2026"));
  });

  it("JSON canonico indipendente dall'ordine delle chiavi", async () => {
    assert.equal(canonicalJson({ b: 1, a: { d: 2, c: 3 } }), '{"a":{"c":3,"d":2},"b":1}');
    assert.equal(
      await sha256Hex(canonicalJson({ a: 1, b: 2 })),
      await sha256Hex(canonicalJson({ b: 2, a: 1 })),
    );
  });
});

describe("riepiloghi (funzioni pure)", () => {
  const item = (
    amountMinor: number,
    kind: SummaryItem["kind"],
    extra: Partial<SummaryItem> = {},
  ): SummaryItem => ({
    amountMinor,
    currency: "EUR",
    status: "booked",
    kind,
    categoryId: null,
    budgetGroup: null,
    ...extra,
  });

  it("entrate, spese, commissioni, rimborsi, trasferimenti, non classificate, pending", () => {
    const [eur, usd] = summarize([
      item(120000, "income"),
      item(-4520, "expense", { categoryId: "spesa", budgetGroup: "needs" }),
      item(-1299, "expense", { categoryId: "abbonamenti", budgetGroup: "wants" }),
      item(-100, "fee", { categoryId: "commissioni", budgetGroup: "needs" }),
      item(2765, "refund", { categoryId: "libri" }),
      item(-20000, "transfer"),
      item(20000, "transfer"),
      item(-350, "unknown"),
      item(1000, "unknown"),
      item(-2500, "expense", { status: "pending" }),
      item(-5000, "expense", { currency: "USD" }),
    ]);
    assert.ok(eur && usd);
    assert.equal(eur.income, 120000);
    assert.equal(eur.expenses, 5819);
    assert.equal(eur.fees, 100);
    assert.equal(eur.refunds, 2765);
    assert.equal(eur.netSpending, 5819 + 100 - 2765);
    assert.equal(eur.net, 120000 + 2765 - 5819 - 100);
    assert.equal(eur.transfersIn, 20000);
    assert.equal(eur.transfersOut, 20000);
    assert.equal(eur.unclassifiedOut, 350);
    assert.equal(eur.unclassifiedIn, 1000);
    assert.equal(eur.unclassifiedCount, 2);
    assert.equal(eur.pendingCount, 1);
    assert.equal(eur.pendingSum, -2500);
    assert.deepEqual(eur.spendingByCategory, { spesa: 4520, abbonamenti: 1299, commissioni: 100 });
    assert.deepEqual(eur.spendingByBudgetGroup, { needs: 4620, wants: 1299, savings: 0, unassigned: 0 });
    assert.equal(usd.expenses, 5000, "le valute non si sommano mai");
    const shares = budgetShares(eur);
    assert.equal(shares.needs + shares.wants + shares.savings + shares.unassigned, 10000);
  });

  it("riconciliazione: ok, differenza, dati mancanti, conflitto", () => {
    assert.deepEqual(
      reconcile({
        openingObservations: [50000],
        closingObservations: [143381],
        movements: [120000, -4520, -350, -350, -20000, -1299, -100],
      }),
      {
        status: "ok",
        opening: 50000,
        closing: 143381,
        movementsSum: 93381,
        difference: 0,
      },
    );
    assert.equal(
      reconcile({ openingObservations: [100], closingObservations: [90], movements: [-5] }).difference,
      5,
    );
    assert.equal(
      reconcile({ openingObservations: [], closingObservations: [90], movements: [] }).status,
      "insufficient_data",
    );
    assert.equal(
      reconcile({ openingObservations: [100, 101], closingObservations: [90], movements: [] }).status,
      "conflict",
    );
    assert.equal(
      reconcile({ openingObservations: [100, 100], closingObservations: [100], movements: [] }).status,
      "ok",
    );
  });

  it("obiettivi di risparmio", () => {
    assert.deepEqual(goalProgress(50000, [10000, 15000, -5000]), {
      allocated: 20000,
      remaining: 30000,
      progressBp: 4000,
      reached: false,
    });
    assert.equal(goalProgress(100, [150]).reached, true);
    assert.equal(goalProgress(100, [150]).remaining, 0);
    assert.throws(() => goalProgress(0, []));
  });
});
