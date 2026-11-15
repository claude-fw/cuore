/**
 * Motore a specifica con formati SINTETICI ("esempio-*"): non sono formati bancari e non vengono registrati.
 * Verificano le regole che un formato reale dovra' rispettare: date, fusi, segni, stati, esclusioni,
 * commissioni, righe prima/dopo la tabella, CSV italiano, XLSX, idempotenza, rollback.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { importFile } from "../src/import/importer.ts";
import { PENDING_BANK_FORMATS } from "../src/import/parsers/bank-formats.ts";
import { FormatNotAvailableError, getParser, listFormats } from "../src/import/parsers/registry.ts";
import { createTabularParser, sniffFileKind } from "../src/import/tabular/engine.ts";
import { type TabularSpec, validateTabularSpec } from "../src/import/tabular/spec.ts";
import type { StatementParser } from "../src/import/types.ts";
import { classifyTransactions, publishRuleSet } from "../src/services/classification.ts";
import { decideLink, detectLinks, listLinks } from "../src/services/links.ts";
import { periodReport } from "../src/services/reports.ts";
import { listReviewItems } from "../src/services/review.ts";
import {
  A_COLUMNS,
  type ARow,
  aFile,
  B_PREAMBLE,
  bFile,
  SPEC_A,
  SPEC_B,
  SPEC_C,
} from "./fixtures/synthetic-formats.ts";
import { count, FailingDb, setupWithAccounts, type TestEnv } from "./helpers.ts";
import { buildXlsx, type XCell } from "./xlsx-builder.ts";

const parserA = createTabularParser(SPEC_A);
const parserB = createTabularParser(SPEC_B);
const parserC = createTabularParser(SPEC_C);

const imp = (env: TestEnv, parser: StatementParser, content: Uint8Array, account = "conto") =>
  importFile(env.ctx, {
    accountId: account,
    parserId: parser.id,
    parser,
    fileName: `${parser.id}.dat`,
    content,
  });

const all = (env: TestEnv) =>
  env.db.all<{
    source_txn_id: string | null;
    status: string;
    transaction_date: string;
    booking_date: string | null;
    amount_minor: number;
    description: string;
    source_type: string | null;
    original_amount_minor: number | null;
    original_currency: string | null;
  }>("SELECT * FROM transactions ORDER BY transaction_date, source_txn_id, description");

describe("specifica dichiarativa: validazione", () => {
  it("le specifiche di esempio sono valide", () => {
    assert.deepEqual(validateTabularSpec(SPEC_A), []);
    assert.deepEqual(validateTabularSpec(SPEC_B), []);
  });

  it("rileva incoerenze prima di leggere qualsiasi file", () => {
    const broken: TabularSpec = {
      ...SPEC_A,
      reportingTimeZone: "Europe/Nowhere",
      input: { kind: "csv", encoding: "utf-8", delimiter: "," },
      numbers: { decimal: ",", thousands: "," },
      fields: {
        ...SPEC_A.fields,
        transactionDate: { column: "Data operazione", format: "YYYY-MM-DD HH:mm:ss" },
        bookingDate: { column: "Inesistente", format: "DD/MM/YYYY", timeZone: "UTC" },
        amount: { kind: "debit_credit", debit: "Importo", credit: "Importo", debitValues: "positive" },
        status: { column: "Stato", map: {} },
      },
    };
    const problems = validateTabularSpec(broken).join("\n");
    for (const frag of [
      "timeZone obbligatorio",
      "Inesistente",
      "timeZone ammesso solo",
      "stessa colonna",
      "status.map vuota",
      "separatori coincidenti",
      "separatore di colonna",
      "reportingTimeZone",
    ]) {
      assert.ok(problems.includes(frag), frag);
    }
    assert.throws(() => createTabularParser(broken), /non valida/);
  });
});

describe("formato sintetico A (CSV, UTC, stati, commissioni)", () => {
  it("importa con date convertite al fuso italiano, commissione separata, stato e tipo della fonte conservati", async () => {
    const env = await setupWithAccounts();
    const r = await imp(
      env,
      parserA,
      aFile([
        { id: "T1", op: "2025-12-31 23:30:00", amount: "-12.00", desc: "Cena", tipo: "PAGAMENTO" },
        {
          id: "T2",
          op: "2026-01-03 10:00:00",
          book: "2026-01-04 08:00:00",
          amount: "-27.65",
          fee: "0.50",
          orig: ["-30.00", "USD"],
        },
        { id: "T3", op: "2026-01-05 09:00:00", stato: "IN_ATTESA", amount: "-9.90" },
        { id: "T4", op: "2026-01-06 09:00:00", stato: "ANNULLATA", amount: "-5.00" },
        { id: "T5", op: "2026-01-10 12:00:00", amount: "27.65", tipo: "RIMBORSO", desc: "Negozio Esempio" },
      ]),
    );
    assert.equal(r.status, "committed", JSON.stringify(r.issues));
    assert.deepEqual(
      {
        rows: r.counts.rows,
        transactions: r.counts.transactions,
        excluded: r.counts.excluded,
        created: r.counts.created,
      },
      { rows: 5, transactions: 5, excluded: 1, created: 5 },
    );
    const txns = await all(env);
    const byId = new Map(txns.map((t) => [t.source_txn_id, t]));
    assert.equal(byId.get("T1")?.transaction_date, "2026-01-01", "23:30 UTC del 31/12 = 1 gennaio in Italia");
    assert.equal(byId.get("T2")?.amount_minor, -2765);
    assert.equal(byId.get("T2")?.original_amount_minor, -3000);
    assert.equal(byId.get("T2")?.original_currency, "USD");
    assert.equal(byId.get("T2#fee")?.amount_minor, -50);
    assert.equal(byId.get("T2#fee")?.source_type, "derived:fee");
    assert.equal(byId.get("T3")?.status, "pending");
    assert.equal(byId.get("T3")?.booking_date, null);
    assert.equal(byId.get("T4"), undefined, "la riga annullata non diventa una transazione");
    assert.equal(byId.get("T5")?.source_type, "RIMBORSO");
    const ex = await env.db.first<{ reason: string; source_txn_id: string; raw_json: string }>(
      "SELECT * FROM excluded_rows",
    );
    assert.equal(ex?.reason, "operazione annullata dalla fonte");
    assert.equal(ex?.source_txn_id, "T4");
    assert.equal(
      JSON.parse(ex?.raw_json ?? "{}").Importo,
      "-5.00",
      "la riga esclusa e' conservata integralmente",
    );
    assert.equal(
      await count(env.db, "raw_records"),
      4,
      "un record grezzo per riga importata (T2 e la sua commissione condividono la riga)",
    );
  });

  it("stessa importazione due volte e stesso contenuto riordinato: nessun doppione", async () => {
    const env = await setupWithAccounts();
    const rows: ARow[] = [
      { id: "T1", op: "2026-01-02 10:00:00", amount: "-1.00" },
      { id: "T2", op: "2026-01-03 10:00:00", amount: "-2.00", fee: "0.10" },
    ];
    await imp(env, parserA, aFile(rows));
    assert.equal((await imp(env, parserA, aFile(rows))).status, "duplicate_file");
    const again = await imp(env, parserA, aFile([...rows].reverse()));
    assert.deepEqual([again.counts.created, again.counts.unchanged], [0, 3]);
  });

  it("pending che si completa: aggiornata; pending poi annullata: verifica, nessuna modifica automatica", async () => {
    const env = await setupWithAccounts();
    await imp(
      env,
      parserA,
      aFile([
        { id: "P1", op: "2026-01-05 09:00:00", stato: "IN_ATTESA", amount: "-9.90" },
        { id: "P2", op: "2026-01-05 10:00:00", stato: "IN_ATTESA", amount: "-4.00" },
      ]),
    );
    const r = await imp(
      env,
      parserA,
      aFile([
        { id: "P1", op: "2026-01-05 09:00:00", book: "2026-01-06 07:00:00", amount: "-9.90" },
        { id: "P2", op: "2026-01-05 10:00:00", stato: "ANNULLATA", amount: "-4.00" },
      ]),
    );
    assert.equal(r.counts.updated, 1);
    const txns = await all(env);
    assert.equal(txns.find((t) => t.source_txn_id === "P1")?.status, "booked");
    assert.equal(
      txns.find((t) => t.source_txn_id === "P2")?.status,
      "pending",
      "nessuna cancellazione automatica",
    );
    assert.ok((await listReviewItems(env.ctx)).some((i) => i.kind === "pending_excluded"));
  });

  it("stato sconosciuto, valuta diversa, id ripetuto, commissione non numerica: file rifiutato senza scritture", async () => {
    const env = await setupWithAccounts();
    const cases: [ARow[], string][] = [
      [[{ id: "X", op: "2026-01-02 10:00:00", amount: "-1.00", stato: "RIFIUTATA" }], "unknown_status"],
      [[{ id: "X", op: "2026-01-02 10:00:00", amount: "-1.00", cur: "USD" }], "currency_mismatch"],
      [[{ id: "X", op: "2026-01-02 10:00:00", amount: "-1.00", cur: "XYZ" }], "unsupported_currency"],
      [
        [
          { id: "X", op: "2026-01-02 10:00:00", amount: "-1.00" },
          { id: "X", op: "2026-01-03 10:00:00", amount: "-2.00" },
        ],
        "duplicate_id",
      ],
      [[{ id: "X", op: "2026-01-02 10:00:00", amount: "-1.00", fee: "abc" }], "invalid_amount"],
      [[{ id: "X", op: "02/01/2026 10:00", amount: "-1.00" }], "invalid_date"],
      [[{ id: "", op: "2026-01-02 10:00:00", amount: "-1.00" }], "missing_value"],
      [[{ id: "X", op: "2026-01-02 10:00:00", amount: "" }], "missing_value"],
      [
        [{ id: "X", op: "2026-01-02 10:00:00", amount: "-1.00", orig: ["30.00", "USD"] }],
        "original_sign_mismatch",
      ],
      [
        [{ id: "X", op: "2026-01-02 10:00:00", amount: "-1.00", orig: ["-2.00", "EUR"] }],
        "original_mismatch",
      ],
      [
        [
          {
            id: "X",
            op: "2026-01-02 10:00:00",
            stato: "IN_ATTESA",
            book: "2026-01-03 10:00:00",
            amount: "-1.00",
          },
        ],
        "unexpected_booking_date",
      ],
    ];
    for (const [rows, code] of cases) {
      const r = await imp(env, parserA, aFile(rows));
      assert.equal(r.status, "rejected", code);
      assert.ok(
        r.issues.some((i) => i.code === code),
        `${code}: ${r.issues.map((i) => i.code).join(",")}`,
      );
    }
    const r = await imp(
      env,
      parserA,
      aFile([{ id: "X", op: "2026-01-02 10:00:00", amount: "-1.00" }], `${A_COLUMNS.join(",")},Extra`),
    );
    assert.ok(r.issues.some((i) => i.code === "unknown_column"));
    const missing = await imp(env, parserA, aFile([], A_COLUMNS.slice(1).join(",")));
    assert.ok(missing.issues.some((i) => i.code === "header_not_found"));
    assert.equal(await count(env.db, "transactions"), 0);
    assert.equal(await count(env.db, "excluded_rows"), 0);
  });

  it("errore di scrittura a meta': nessuna transazione, nessuna riga esclusa, nessun grezzo", async () => {
    const env = await setupWithAccounts();
    const ctx = { ...env.ctx, db: new FailingDb(env.db, 9) };
    const r = await importFile(ctx, {
      accountId: "conto",
      parserId: parserA.id,
      parser: parserA,
      fileName: "a.csv",
      content: aFile([
        { id: "T1", op: "2026-01-02 10:00:00", amount: "-1.00", fee: "0.10" },
        { id: "T2", op: "2026-01-03 10:00:00", amount: "-2.00", stato: "ANNULLATA" },
      ]),
    });
    assert.equal(r.status, "rejected");
    for (const t of ["transactions", "excluded_rows", "raw_records", "transaction_observations"]) {
      assert.equal(await count(env.db, t), 0, t);
    }
  });

  it("rimborso etichettato dalla fonte: nessuna classificazione automatica senza una regola esplicita", async () => {
    const env = await setupWithAccounts();
    await imp(
      env,
      parserA,
      aFile([{ id: "R1", op: "2026-01-10 12:00:00", amount: "27.65", tipo: "RIMBORSO" }]),
    );
    await publishRuleSet(env.ctx, {
      schema: "cloud-finance-rules/1",
      note: null,
      categories: [],
      rules: [
        {
          id: "altro",
          priority: 1,
          description: "non pertinente",
          when: { all: [{ field: "description", op: "equals", value: "zzz" }] },
          assign: { kind: "expense", category: null, confidence: "high" },
        },
      ],
    });
    await classifyTransactions(env.ctx);
    const c1 = await env.db.first<{ kind: string; status: string }>(
      "SELECT kind, status FROM current_classifications",
    );
    assert.deepEqual({ ...c1 }, { kind: "unknown", status: "needs_review" });
    // senza regola: NON e' un'entrata, resta "da classificare"
    const before = (await periodReport(env.ctx, "2026-01-01", "2026-01-31")).currencies[0];
    assert.deepEqual([before?.income, before?.refunds, before?.unclassifiedIn], [0, 0, 2765]);
    await publishRuleSet(env.ctx, {
      schema: "cloud-finance-rules/1",
      note: "v2",
      categories: [],
      rules: [
        {
          id: "rimborso-fonte",
          priority: 10,
          description: "tipo RIMBORSO dichiarato dalla fonte",
          when: {
            all: [
              { field: "source_type", op: "equals", value: "RIMBORSO" },
              { field: "direction", op: "equals", value: "in" },
            ],
          },
          assign: { kind: "refund", category: null, confidence: "high" },
        },
      ],
    });
    await classifyTransactions(env.ctx);
    const c2 = await env.db.first<{ kind: string; status: string }>(
      "SELECT kind, status FROM current_classifications",
    );
    assert.deepEqual({ ...c2 }, { kind: "refund", status: "auto" });
    // con la regola esplicita: rimborso (riduce la spesa netta), mai entrata
    const after = (await periodReport(env.ctx, "2026-01-01", "2026-01-31")).currencies[0];
    assert.deepEqual([after?.income, after?.refunds, after?.unclassifiedIn], [0, 2765, 0]);
  });

  it("stesso identificativo su una riga esclusa e su una riga importabile: file rifiutato (stato ambiguo)", async () => {
    const env = await setupWithAccounts();
    const r = await imp(
      env,
      parserA,
      aFile([
        { id: "D1", op: "2026-01-05 09:00:00", stato: "ANNULLATA", amount: "-4.00" },
        { id: "D1", op: "2026-01-05 09:00:00", amount: "-4.00" },
      ]),
    );
    assert.equal(r.status, "rejected");
    assert.ok(r.issues.some((i) => i.code === "duplicate_id" && i.line === 3));
    for (const t of ["transactions", "excluded_rows", "raw_records"])
      assert.equal(await count(env.db, t), 0, t);
    const twoExcluded = await imp(
      env,
      parserA,
      aFile([
        { id: "D2", op: "2026-01-05 09:00:00", stato: "ANNULLATA", amount: "-4.00" },
        { id: "D2", op: "2026-01-06 09:00:00", stato: "ANNULLATA", amount: "-4.00" },
      ]),
    );
    assert.ok(
      twoExcluded.issues.some((i) => i.code === "duplicate_id"),
      "anche tra due righe escluse",
    );
  });
});

describe("formato sintetico B (CSV italiano, windows-1252, dare/avere)", () => {
  it("importi italiani, accenti, righe prima/dopo la tabella non salvate ma contate", async () => {
    const env = await setupWithAccounts();
    const r = await imp(
      env,
      parserB,
      bFile([
        "05/01/2026;05/01/2026;Caffè Bar Esempio;3,50;;",
        "07/01/2026;08/01/2026;Bonifico in entrata DEMO;;1.200,00;nota libera",
        "09/01/2026;09/01/2026;Spesa supermercato;1.045,20;;",
      ]),
    );
    assert.equal(r.status, "committed", JSON.stringify(r.issues));
    const txns = await all(env);
    assert.deepEqual(
      txns.map((t) => [t.transaction_date, t.amount_minor, t.description]),
      [
        ["2026-01-05", -350, "Caffè Bar Esempio"],
        ["2026-01-07", 120000, "Bonifico in entrata DEMO"],
        ["2026-01-09", -104520, "Spesa supermercato"],
      ],
    );
    const codes = Object.fromEntries(r.notices.map((n) => [n.code, n.count]));
    assert.deepEqual(codes, { rows_before_header: 3, empty_rows: 1, footer_rows: 1 });
    const raws = await env.db.all<{ raw_json: string }>("SELECT raw_json FROM raw_records");
    assert.ok(
      !raws.some((x) => x.raw_json.includes("Intestatario")),
      "i dati prima della tabella non vengono salvati",
    );
    assert.equal(
      JSON.parse(raws[1]?.raw_json ?? "{}").Note,
      "nota libera",
      "colonne extra conservate nel grezzo",
    );
  });

  it("senza identificativi: righe identiche restano distinte; un export sovrapposto aggiunge solo le nuove", async () => {
    const env = await setupWithAccounts();
    const caffe = "05/01/2026;05/01/2026;Caffè Bar Esempio;3,50;;";
    const first = await imp(
      env,
      parserB,
      bFile([caffe, caffe, "06/01/2026;06/01/2026;Edicola Esempio;2,00;;"]),
    );
    assert.equal(first.counts.created, 3);
    // export successivo, periodo sovrapposto: stessi due caffe' + uno nuovo identico + un movimento nuovo
    const second = await imp(
      env,
      parserB,
      bFile([
        caffe,
        caffe,
        caffe,
        "06/01/2026;06/01/2026;Edicola Esempio;2,00;;",
        "07/01/2026;07/01/2026;Bonifico DEMO;;10,00;",
      ]),
    );
    assert.deepEqual([second.counts.created, second.counts.unchanged], [2, 3]);
    assert.equal(await count(env.db, "transactions", "description = 'Caffè Bar Esempio'"), 3);
    // stesso file reimportato: nessuna modifica
    const again = await imp(
      env,
      parserB,
      bFile([
        caffe,
        caffe,
        caffe,
        "06/01/2026;06/01/2026;Edicola Esempio;2,00;;",
        "07/01/2026;07/01/2026;Bonifico DEMO;;10,00;",
      ]),
    );
    assert.equal(again.status, "duplicate_file");
    assert.equal(await count(env.db, "transactions"), 5);
  });

  it("righe ambigue o malformate: dare e avere insieme o assenti, migliaia ambigue, date americane, campi in eccesso", async () => {
    const env = await setupWithAccounts();
    const cases: [string, string][] = [
      ["05/01/2026;05/01/2026;X;3,50;3,50;", "ambiguous_amount"],
      ["05/01/2026;05/01/2026;X;;;", "ambiguous_amount"],
      ["05/01/2026;05/01/2026;X;1.23;;", "invalid_amount"],
      ["01/13/2026;01/13/2026;X;1,00;;", "invalid_date"],
      ["05/01/2026;05/01/2026;X;-1,00;;", "unexpected_sign"],
      ["05/01/2026;05/01/2026;;1,00;;", "missing_description"],
      ["05/01/2026;05/01/2026;X;1,00;;;oltre", "wrong_field_count"],
    ];
    for (const [line, code] of cases) {
      const r = await imp(env, parserB, bFile([line]));
      assert.equal(r.status, "rejected", code);
      assert.ok(
        r.issues.some((i) => i.code === code),
        `${code}: ${r.issues.map((i) => i.code).join(",")}`,
      );
    }
    const long = await imp(
      env,
      parserB,
      bFile(["05/01/2026;05/01/2026;X;1,00;;"], [], [...B_PREAMBLE, "a", "b"]),
    );
    assert.ok(
      long.issues.some((i) => i.code === "header_not_found"),
      "intestazione oltre le righe ammesse",
    );
    // windows-1252 (WHATWG) assegna un carattere a ogni byte: la codifica non e' verificabile, l'intestazione si'
    const garbage = await imp(env, parserB, Uint8Array.from([0x81, 0x8d, 0x8f]));
    assert.ok(garbage.issues.some((i) => i.code === "header_not_found"));
    const notUtf8 = await imp(env, parserA, Uint8Array.from([0xff, 0xfe, 0x00, 0x41]));
    assert.ok(notUtf8.issues.some((i) => i.code === "invalid_encoding"));
    assert.equal(await count(env.db, "transactions"), 0);
  });
});

describe("formato sintetico C (XLSX) e tipi di file non supportati", () => {
  const sheetRows = (rows: XCell[][]): XCell[][] => [
    ["Lista movimenti DEMO"],
    [],
    ["Data contabile", "Data valuta", "Descrizione", "Dare", "Avere"],
    ...rows,
    [],
    ["Saldo contabile", null, null, null, 1234.56],
  ];

  it("numeri e date del foglio (serie e testo), con rumore di virgola mobile", async () => {
    const env = await setupWithAccounts();
    const r = await imp(
      env,
      parserC,
      buildXlsx([
        {
          name: "Foglio1",
          rows: sheetRows([
            [46027, 46027, "Caffè Bar Esempio", 3.5, null],
            ["07/01/2026", "08/01/2026", "Bonifico in entrata DEMO", null, 1200],
            [46031, 46031, "Spesa supermercato", { raw: "1045.2000000000001", type: "n" }, null],
          ]),
        },
      ]),
    );
    assert.equal(r.status, "committed", JSON.stringify(r.issues));
    assert.deepEqual(
      (await all(env)).map((t) => [t.transaction_date, t.amount_minor]),
      [
        ["2026-01-05", -350],
        ["2026-01-07", 120000],
        ["2026-01-09", -104520],
      ],
    );
  });

  it("stessi movimenti da CSV e da XLSX: riconosciuti come gli stessi (nessun doppione)", async () => {
    const env = await setupWithAccounts();
    await imp(env, parserB, bFile(["05/01/2026;05/01/2026;Caffè Bar Esempio;3,50;;"]));
    const r = await imp(
      env,
      parserC,
      buildXlsx([{ name: "S", rows: sheetRows([[46027, 46027, "Caffè Bar Esempio", 3.5, null]]) }]),
    );
    assert.deepEqual([r.counts.created, r.counts.unchanged], [0, 1]);
  });

  it("celle errore, booleani o importi con troppi decimali: rifiuto", async () => {
    const env = await setupWithAccounts();
    for (const [row, code] of [
      [[46027, 46027, "X", { error: "#VALUE!" }, null], "unexpected_cell_type"],
      [[46027, 46027, "X", { bool: true }, null], "unexpected_cell_type"],
      [[46027, 46027, "X", 3.505, null], "too_many_decimals"],
      [[46027.5, 46027, "X", 3.5, null], "invalid_date"],
    ] as [XCell[], string][]) {
      const r = await imp(env, parserC, buildXlsx([{ name: "S", rows: sheetRows([row]) }]));
      assert.equal(r.status, "rejected", code);
      assert.ok(
        r.issues.some((i) => i.code === code),
        `${code}: ${r.issues.map((i) => i.code).join(",")}`,
      );
    }
  });

  it("riconosce e rifiuta .xls binario, HTML travestito, PDF, CSV al posto di XLSX e viceversa", async () => {
    const env = await setupWithAccounts();
    const ole2 = Uint8Array.from([0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1, 0, 0]);
    const html = new TextEncoder().encode("  <html><body><table><tr><td>x</td></tr></table></body></html>");
    const pdf = new TextEncoder().encode("%PDF-1.7\n...");
    assert.equal(sniffFileKind(ole2), "ole2");
    assert.equal(sniffFileKind(html), "html");
    assert.equal(sniffFileKind(pdf), "pdf");
    for (const [parser, bytes] of [
      [parserC, ole2],
      [parserC, html],
      [parserB, pdf],
      [parserC, bFile(["05/01/2026;05/01/2026;X;1,00;;"])],
      [parserB, buildXlsx([{ name: "S", rows: [["a"]] }])],
    ] as [StatementParser, Uint8Array][]) {
      const r = await imp(env, parser, bytes);
      assert.equal(r.status, "rejected");
      assert.ok(
        r.issues.some((i) => i.code === "unsupported_file_type"),
        r.issues.map((i) => i.code).join(","),
      );
    }
    const corrupt = buildXlsx([{ name: "S", rows: [["a"]] }]).slice(0, 200);
    const r = await imp(env, parserC, corrupt);
    assert.ok(r.issues.some((i) => i.code === "xlsx_malformed"));
  });
});

describe("trasferimento tra due conti propri importati da formati diversi", () => {
  it("prima della conferma non e' ne' entrata ne' spesa; confermato esce da entrate e spese", async () => {
    const env = await setupWithAccounts();
    // conto con formato A (es. carta prepagata): uscita verso l'altro conto; conto con formato B: entrata (avere)
    await imp(
      env,
      parserA,
      aFile([
        {
          id: "T9",
          op: "2026-01-12 09:00:00",
          amount: "-50.00",
          tipo: "TRASFERIMENTO",
          desc: "Verso conto DEMO",
        },
      ]),
      "carta",
    );
    await imp(env, parserB, bFile(["12/01/2026;12/01/2026;Bonifico da carta DEMO;;50,00;"]), "conto");
    const eurBefore = (await periodReport(env.ctx, "2026-01-01", "2026-01-31")).currencies[0];
    assert.deepEqual(
      [eurBefore?.income, eurBefore?.expenses, eurBefore?.unclassifiedIn, eurBefore?.unclassifiedOut],
      [0, 0, 5000, 5000],
      "da classificare, mai entrata/spesa",
    );
    assert.equal((await detectLinks(env.ctx)).transfersProposed, 1);
    const [link] = await listLinks(env.ctx, "proposed");
    assert.ok(link);
    await decideLink(env.ctx, link.id, "confirmed");
    const eur = (await periodReport(env.ctx, "2026-01-01", "2026-01-31")).currencies[0];
    assert.deepEqual(
      [eur?.income, eur?.expenses, eur?.transfersIn, eur?.transfersOut, eur?.unclassifiedCount],
      [0, 0, 5000, 5000, 0],
    );
  });
});

describe("formati bancari predisposti (non implementati)", () => {
  it("non sono utilizzabili e spiegano cosa manca", () => {
    for (const f of PENDING_BANK_FORMATS) {
      assert.throws(
        () => getParser(f.id),
        (err: unknown) => err instanceof FormatNotAvailableError && /PREDISPOSTO/.test(err.message),
      );
      assert.ok(f.needed.length >= 5);
      for (const v of f.verified) assert.match(v.source, /^https:\/\/(help\.revolut\.com|www\.mps\.it)\//);
    }
    const formats = listFormats();
    assert.deepEqual(
      formats
        .filter((f) => f.status === "predisposto")
        .map((f) => f.id)
        .sort(),
      ["mps-digital-banking-movimenti", "revolut-personal-statement"],
    );
    assert.ok(
      formats.every((f) => f.verifiedAgainstRealExports === false),
      "nessun formato e' verificato su esportazioni reali",
    );
  });
});
