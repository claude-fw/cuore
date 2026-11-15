/**
 * Correzioni dichiarate della data contabile (src/import/corrections.ts) con dati SINTETICI che ricalcano la forma
 * dello staging MPS: data valuta e data contabile separate, riconciliazione dopo la correzione, file non modificato.
 * Gira sia su SQLite via Node sia su D1 locale (CLOUD_FINANCE_TEST_DB=d1).
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import type { BookingDateCorrection } from "../src/import/corrections.ts";
import { importFile } from "../src/import/importer.ts";
import { MPS_STAGING_CSV } from "../src/import/parsers/dossier-formats.ts";
import { addManualBalance } from "../src/services/balances.ts";
import { reconcileAccount } from "../src/services/reconciliation.ts";
import { count, setupWithAccounts, type TestEnv } from "./helpers.ts";

const H =
  "source_id;booking_date;value_date;description_raw_redacted;direction;amount;currency;signed_amount;classification_status;category;counterparty;source_format;source_file;transaction_id_source;import_status";
const line = (booking: string, value: string, signed: string, desc = "BONIFICO DEMO") =>
  `S1;${booking};${value};${desc};${signed.startsWith("-") ? "out" : "in"};${signed.replace("-", "")};EUR;${signed};unclassified;;;pdf;demo.pdf;;staging_only`;
const csv = (lines: string[]) => new TextEncoder().encode(`﻿${[H, ...lines].join("\r\n")}\r\n`);

/** Staging sintetico con lo stesso difetto di quello reale: data contabile = valuta in tre righe. */
const STAGING = csv([
  line("13/11/2025", "13/11/2025", "-90.0", "BONIFICO DISPOSTO DEMO"),
  line("13/11/2025", "13/11/2025", "100.0", "ACCREDITO DEMO"),
  line("09/03/2026", "09/03/2026", "-200.0", "BONIFICO DISPOSTO DEMO"),
  line("19/04/2026", "19/04/2026", "-100.0", "BONIFICO DISPOSTO DEMO"),
]);
const evidence = "estratto sintetico DEMO, pagina 2, colonna Data";
const CORRECTIONS: BookingDateCorrection[] = [
  {
    match: { value_date: "13/11/2025", signed_amount: "-90.0" },
    bookingDate: "2025-11-17",
    reason: "data contabile del PDF",
    evidence,
  },
  {
    match: { value_date: "13/11/2025", signed_amount: "100.0" },
    bookingDate: "2025-11-17",
    reason: "data contabile del PDF",
    evidence,
  },
  {
    match: { value_date: "19/04/2026", signed_amount: "-100.0" },
    bookingDate: "2026-04-20",
    reason: "data contabile del PDF",
    evidence,
  },
];
const imp = (
  env: TestEnv,
  corrections: BookingDateCorrection[],
  content = STAGING,
  fileName = "staging.csv",
) =>
  importFile(env.ctx, { accountId: "conto", parserId: MPS_STAGING_CSV.id, fileName, content, corrections });

describe("correzioni dichiarate della data contabile", () => {
  it("applicate: data contabile corretta, valuta invariata, grezzo intatto, correzioni nell'audit", async () => {
    const env = await setupWithAccounts();
    const r = await imp(env, CORRECTIONS);
    assert.equal(r.status, "committed", JSON.stringify(r.issues));
    assert.deepEqual(
      r.corrections.map((c) => [c.from, c.to]),
      [
        ["2025-11-13", "2025-11-17"],
        ["2025-11-13", "2025-11-17"],
        ["2026-04-19", "2026-04-20"],
      ],
    );
    assert.deepEqual(
      (
        await env.db.all<object>(
          "SELECT transaction_date, booking_date, amount_minor FROM transactions ORDER BY transaction_date, amount_minor",
        )
      ).map((x) => Object.assign({}, x)),
      [
        { transaction_date: "2025-11-13", booking_date: "2025-11-17", amount_minor: -9000 },
        { transaction_date: "2025-11-13", booking_date: "2025-11-17", amount_minor: 10000 },
        { transaction_date: "2026-03-09", booking_date: "2026-03-09", amount_minor: -20000 },
        { transaction_date: "2026-04-19", booking_date: "2026-04-20", amount_minor: -10000 },
      ],
    );
    // il grezzo conserva la data dello staging (fonte non modificata)
    const raw = await env.db.all<{ raw_json: string }>(
      "SELECT raw_json FROM raw_records ORDER BY line_number",
    );
    assert.equal(
      (JSON.parse(raw[0]?.raw_json ?? "{}") as { booking_date: string }).booking_date,
      "13/11/2025",
    );
    const audit = await env.db.first<{ after_json: string }>(
      "SELECT after_json FROM audit_log WHERE action = 'import_committed'",
    );
    assert.equal((JSON.parse(audit?.after_json ?? "{}") as { corrections: unknown[] }).corrections.length, 3);
    // stesso file con le stesse correzioni: gia' importato
    assert.equal((await imp(env, CORRECTIONS)).status, "duplicate_file");
  });

  it("riconciliazione per data contabile: con la correzione quadra, senza la correzione no", async () => {
    const withFix = await setupWithAccounts();
    const without = await setupWithAccounts();
    await imp(withFix, CORRECTIONS);
    await imp(without, []);
    for (const env of [withFix, without]) {
      await addManualBalance(env.ctx, "conto", "2025-11-06", 0);
      await addManualBalance(env.ctx, "conto", "2025-11-14", 0); // nessun movimento contabilizzato fino al 26/12
      await addManualBalance(env.ctx, "conto", "2025-12-31", 1000);
    }
    const periods = async (env: TestEnv) => [
      (await reconcileAccount(env.ctx, "conto", "2025-11-06", "2025-11-14")).status,
      (await reconcileAccount(env.ctx, "conto", "2025-11-14", "2025-12-31")).status,
    ];
    assert.deepEqual(await periods(withFix), ["ok", "ok"]);
    assert.deepEqual(await periods(without), ["difference", "difference"]);
  });

  it("correzione senza riga, ambigua, doppia, superata o senza prova: file rifiutato, nulla scritto", async () => {
    const env = await setupWithAccounts();
    const base = CORRECTIONS[0] as BookingDateCorrection;
    const cases: [BookingDateCorrection[], string][] = [
      [[{ ...base, match: { value_date: "01/01/2030" } }], "correction_not_matched"],
      [[{ ...base, match: { value_date: "13/11/2025" } }], "correction_ambiguous"],
      [[base, { ...base, bookingDate: "2025-12-30" }], "correction_conflict"],
      [[{ ...base, match: { value_date: "09/03/2026" }, bookingDate: "2026-03-09" }], "correction_noop"],
      [[{ ...base, evidence: " " }], "correction_invalid"],
      [[{ ...base, bookingDate: "2025-12-32" }], "correction_invalid"],
      [[{ ...base, match: {} }], "correction_invalid"],
      [[{ ...base, match: { colonna_inesistente: "x" } }], "correction_not_matched"],
    ];
    for (const [corrections, code] of cases) {
      const r = await imp(env, corrections, STAGING, `${code}-${Math.random()}.csv`);
      assert.equal(r.status, "rejected", code);
      assert.ok(
        r.issues.some((i) => i.code === code),
        `${code}: ${JSON.stringify(r.issues)}`,
      );
    }
    assert.equal(await count(env.db, "transactions"), 0);
  });
});
