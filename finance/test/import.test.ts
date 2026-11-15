import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { importFile } from "../src/import/importer.ts";
import { getParser } from "../src/import/parsers/registry.ts";
import { listReviewItems } from "../src/services/review.ts";
import {
  count,
  csvBytes,
  FailingDb,
  HEADER,
  importTx,
  row,
  setupWithAccounts,
  type TestEnv,
} from "./helpers.ts";

interface Txn {
  id: string;
  status: string;
  amount_minor: number;
  booking_date: string | null;
  description: string;
  source_txn_id: string | null;
  occurrence: number | null;
  original_amount_minor: number | null;
  original_currency: string | null;
}

const txns = (env: TestEnv, account = "conto") =>
  env.db.all<Txn>("SELECT * FROM transactions WHERE account_id = ? ORDER BY transaction_date, id", [account]);

const BASE = [
  row("2026-01-02", "1200.00", "Stipendio DEMO", { counterparty: "Azienda Esempio" }),
  row("2026-01-03", "-45.20", "Spesa supermercato", {
    counterparty: "Supermercato Esempio",
    booking: "2026-01-04",
  }),
  row("2026-01-05", "-3.50", "Caffe", { counterparty: "Bar Esempio" }),
  row("2026-01-05", "-3.50", "Caffe", { counterparty: "Bar Esempio" }),
  row("2026-01-15", "-25.00", "Ristorante Esempio", { status: "pending" }),
];

describe("importazione: idempotenza e doppioni", () => {
  it("prima importazione: conteggi, storico grezzo, provenienza, audit", async () => {
    const env = await setupWithAccounts();
    const r = await importTx(env, "conto", BASE);
    assert.equal(r.status, "committed");
    assert.equal(r.parser.verifiedAgainstRealExports, false);
    assert.deepEqual(
      { rows: r.counts.rows, created: r.counts.created, unchanged: r.counts.unchanged },
      { rows: 5, created: 5, unchanged: 0 },
    );
    assert.equal(await count(env.db, "transactions"), 5);
    assert.equal(await count(env.db, "raw_records", "import_run_id = ?", [r.runId]), 5);
    assert.equal(await count(env.db, "transaction_observations", "outcome = 'created'"), 5);
    assert.equal(await count(env.db, "audit_log", "entity = 'transaction' AND action = 'create'"), 5);
    const raw = await env.db.first<{ raw_json: string; line_number: number }>(
      "SELECT raw_json, line_number FROM raw_records WHERE import_run_id = ? ORDER BY line_number LIMIT 1",
      [r.runId],
    );
    assert.equal(raw?.line_number, 2);
    assert.equal(
      JSON.parse(raw?.raw_json ?? "{}").amount,
      "1200.00",
      "il grezzo conserva il testo originale",
    );
  });

  it("stesso file importato due volte: nessun duplicato, esito duplicate_file", async () => {
    const env = await setupWithAccounts();
    const first = await importTx(env, "conto", BASE);
    const second = await importTx(env, "conto", BASE);
    assert.equal(second.status, "duplicate_file");
    assert.equal(second.duplicateOf, first.runId);
    assert.equal(await count(env.db, "transactions"), 5);
    assert.equal(await count(env.db, "raw_records"), 5);
  });

  it("stesso contenuto in un file diverso (CRLF, righe riordinate): 0 create, tutte invariate", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "conto", BASE);
    const reordered = [...BASE].reverse();
    const r = await importFile(env.ctx, {
      accountId: "conto",
      parserId: "cloud-finance-csv",
      fileName: "altro.csv",
      content: csvBytes(reordered, "\r\n"),
    });
    assert.equal(r.status, "committed");
    assert.equal(r.counts.created, 0);
    assert.equal(r.counts.unchanged, 5);
    assert.equal(await count(env.db, "transactions"), 5);
  });

  it("righe identiche nello stesso file restano distinte e non raddoppiano reimportando", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "conto", BASE);
    const caffe = (await txns(env)).filter((t) => t.description === "Caffe");
    assert.equal(caffe.length, 2);
    assert.deepEqual(caffe.map((t) => t.occurrence).sort(), [1, 2]);
    // periodo sovrapposto con un terzo caffe' identico: solo quello nuovo viene creato
    const r = await importTx(env, "conto", [
      ...BASE.slice(2, 4),
      row("2026-01-05", "-3.50", "Caffe", { counterparty: "Bar Esempio" }),
    ]);
    assert.equal(r.counts.created, 1);
    assert.equal(r.counts.unchanged, 2);
    assert.equal((await txns(env)).filter((t) => t.description === "Caffe").length, 3);
  });

  it("righe quasi identiche (stesso giorno e importo, descrizione diversa) non vengono unite", async () => {
    const env = await setupWithAccounts();
    const r = await importTx(env, "conto", [
      row("2026-01-05", "-3.50", "Caffe", { counterparty: "Bar Esempio" }),
      row("2026-01-05", "-3.50", "Cornetto", { counterparty: "Bar Esempio" }),
    ]);
    assert.equal(r.counts.created, 2);
    assert.equal(
      await count(env.db, "transaction_links"),
      0,
      "nessun collegamento tra righe dello stesso file",
    );
  });

  it("stessa operazione con descrizione cambiata in un export successivo: possibile doppione PROPOSTO, non unito", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "conto", [row("2026-01-05", "-3.50", "Caffe", { counterparty: "Bar Esempio" })]);
    const r = await importTx(env, "conto", [
      row("2026-01-05", "-3.50", "BAR ESEMPIO SRL", { counterparty: "Bar Esempio" }),
    ]);
    assert.equal(r.counts.created, 1);
    assert.equal(await count(env.db, "transactions"), 2);
    assert.equal(
      await count(env.db, "transaction_links", "type = 'possible_duplicate' AND status = 'proposed'"),
      1,
    );
    const open = await listReviewItems(env.ctx);
    assert.ok(open.some((i) => i.kind === "possible_duplicate"));
  });

  it("differenze solo di maiuscole/spazi nella descrizione: stessa transazione, testo originale conservato", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "conto", [row("2026-01-05", "-3.50", "Caffe Bar", {})]);
    const r = await importTx(env, "conto", [row("2026-01-05", "-3.50", "CAFFE   bar", {})]);
    assert.equal(r.counts.unchanged, 1);
    assert.equal((await txns(env))[0]?.description, "Caffe Bar");
  });
});

describe("importazione: stati, importi, valute", () => {
  it("pending con id che diventa contabilizzata: aggiornamento con audit, nessun doppione", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "carta", [
      row("2026-01-25", "-9.90", "Cinema Esempio", { id: "C-1", status: "pending" }),
    ]);
    const r = await importTx(env, "carta", [
      row("2026-01-25", "-9.90", "Cinema Esempio", { id: "C-1", booking: "2026-01-27" }),
    ]);
    assert.equal(r.counts.updated, 1);
    const [t] = await txns(env, "carta");
    assert.equal(t?.status, "booked");
    assert.equal(t?.booking_date, "2026-01-27");
    assert.equal(await count(env.db, "audit_log", "action = 'book'"), 1);
    assert.equal(await count(env.db, "transactions", "account_id = 'carta'"), 1);
  });

  it("pending che si contabilizza con importo diverso (es. cambio): aggiornata e segnalata", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "carta", [row("2026-01-25", "-27.00", "Libreria", { id: "C-2", status: "pending" })]);
    await importTx(env, "carta", [
      row("2026-01-25", "-27.65", "Libreria", { id: "C-2", booking: "2026-01-26" }),
    ]);
    assert.equal((await txns(env, "carta"))[0]?.amount_minor, -2765);
    assert.ok((await listReviewItems(env.ctx)).some((i) => i.kind === "amount_changed_on_booking"));
  });

  it("pending senza id che si contabilizza con la stessa impronta: aggiornata", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "conto", [row("2026-01-15", "-25.00", "Ristorante", { status: "pending" })]);
    const r = await importTx(env, "conto", [
      row("2026-01-15", "-25.00", "Ristorante", { booking: "2026-01-16" }),
    ]);
    assert.equal(r.counts.updated, 1);
    assert.equal(await count(env.db, "transactions", "status = 'booked'"), 1);
    assert.equal(await count(env.db, "transactions"), 1);
  });

  it("pending senza id contabilizzata con descrizione diversa: collegamento proposto, pending non cancellata", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "conto", [row("2026-01-15", "-25.00", "POS RISTORANTE", { status: "pending" })]);
    await importTx(env, "conto", [
      row("2026-01-16", "-25.00", "Ristorante Esempio Srl", { booking: "2026-01-17" }),
    ]);
    assert.equal(await count(env.db, "transactions"), 2);
    assert.equal(
      await count(env.db, "transaction_links", "type = 'pending_booked' AND status = 'proposed'"),
      1,
    );
  });

  it("transazione contabilizzata che cambia nella fonte: nessuna modifica, conflitto da verificare", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "carta", [row("2026-01-09", "-27.65", "Libreria", { id: "C-3" })]);
    const r = await importTx(env, "carta", [row("2026-01-09", "-30.00", "Libreria", { id: "C-3" })]);
    assert.equal(r.counts.conflicts, 1);
    assert.equal((await txns(env, "carta"))[0]?.amount_minor, -2765);
    const item = (await listReviewItems(env.ctx)).find((i) => i.kind === "source_changed");
    assert.ok(item);
    assert.deepEqual((item.detail as { cambiamenti: unknown }).cambiamenti, {
      amount_minor: { from: -2765, to: -3000 },
    });
  });

  it("contabilizzata che torna pending nella fonte: regressione segnalata, stato invariato", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "carta", [row("2026-01-09", "-5.00", "X", { id: "C-4" })]);
    await importTx(env, "carta", [row("2026-01-09", "-5.00", "X", { id: "C-4", status: "pending" })]);
    assert.equal((await txns(env, "carta"))[0]?.status, "booked");
    assert.ok((await listReviewItems(env.ctx)).some((i) => i.kind === "status_regression"));
  });

  it("pending gia' nota che sparisce da un export che copre la sua data: da verificare", async () => {
    const env = await setupWithAccounts();
    await importTx(env, "conto", [row("2026-01-15", "-25.00", "Ristorante", { status: "pending" })]);
    await importTx(env, "conto", [row("2026-01-10", "-1.00", "A"), row("2026-01-20", "-2.00", "B")]);
    assert.ok((await listReviewItems(env.ctx)).some((i) => i.kind === "pending_not_seen"));
    assert.equal(await count(env.db, "transactions", "status = 'pending'"), 1, "nessuna modifica automatica");
  });

  it("importi negativi, zero, valuta originale diversa", async () => {
    const env = await setupWithAccounts();
    const r = await importTx(env, "carta", [
      row("2026-01-09", "-27.65", "Libreria online", { id: "C-5", original: ["-30.00", "USD"] }),
      row("2026-01-10", "0.00", "Verifica carta", { id: "C-6" }),
    ]);
    assert.equal(r.status, "committed");
    const t = (await txns(env, "carta")).find((x) => x.source_txn_id === "C-5");
    assert.equal(t?.amount_minor, -2765);
    assert.equal(t?.original_amount_minor, -3000);
    assert.equal(t?.original_currency, "USD");
  });

  it("conto in USD: importi in USD, nessuna conversione implicita", async () => {
    const env = await setupWithAccounts();
    const r = await importTx(env, "usd", [row("2026-01-09", "-12.00", "Store", { currency: "USD" })]);
    assert.equal(r.status, "committed");
    assert.equal((await txns(env, "usd"))[0]?.amount_minor, -1200);
  });
});

describe("importazione: input non validi e rollback", () => {
  const rejectedWith = async (env: TestEnv, lines: string[], code: string, header?: string) => {
    const r = await importFile(env.ctx, {
      accountId: "conto",
      parserId: "cloud-finance-csv",
      fileName: "x.csv",
      content: csvBytes(lines, "\n", header),
    });
    assert.equal(r.status, "rejected", code);
    assert.ok(
      r.issues.some((i) => i.code === code),
      `atteso ${code}, trovati ${r.issues.map((i) => i.code).join(",")}`,
    );
    return r;
  };

  it("un solo errore rifiuta l'intero file: nessuna transazione, esecuzione registrata come rejected", async () => {
    const env = await setupWithAccounts();
    const r = await rejectedWith(
      env,
      [row("2026-01-02", "1.00", "ok"), row("2026-02-30", "1.00", "data errata")],
      "invalid_date",
    );
    assert.equal(r.issues[0]?.line, 3);
    assert.equal(await count(env.db, "transactions"), 0);
    assert.equal(await count(env.db, "raw_records"), 0);
    assert.equal(await count(env.db, "import_runs", "status = 'rejected'"), 1);
  });

  it("errori di riga: importo, decimali, stato, date di contabilizzazione, valuta, id ripetuti, descrizione", async () => {
    const env = await setupWithAccounts();
    // virgola decimale (formato italiano) tra apici: un campo solo, ma importo ambiguo -> rifiutato
    await rejectedWith(env, [row("2026-01-02", '"1,50"', "x")], "invalid_amount");
    // senza apici la virgola sposta le colonne: la riga ha un campo in piu' -> rifiutata
    await rejectedWith(env, [row("2026-01-02", "1,50", "x")], "wrong_field_count");
    await rejectedWith(env, [row("2026-01-02", "1.505", "x")], "too_many_decimals");
    await rejectedWith(env, [row("2026-01-02", "1.00", "x").replace(",booked,", ",done,")], "invalid_status");
    await rejectedWith(env, [row("2026-01-02", "1.00", "x", { booking: "" })], "missing_booking_date");
    await rejectedWith(
      env,
      [row("2026-01-02", "1.00", "x", { status: "pending", booking: "2026-01-02" })],
      "unexpected_booking_date",
    );
    await rejectedWith(env, [row("2026-01-02", "1.00", "x", { currency: "USD" })], "currency_mismatch");
    await rejectedWith(
      env,
      [row("2026-01-02", "1.00", "a", { id: "D" }), row("2026-01-03", "1.00", "b", { id: "D" })],
      "duplicate_id",
    );
    await rejectedWith(env, [row("2026-01-02", "1.00", "")], "missing_description");
    await rejectedWith(
      env,
      [row("2026-01-02", "-1.00", "x", { original: ["-1.00", ""] })],
      "original_incomplete",
    );
    await rejectedWith(
      env,
      [row("2026-01-02", "-1.00", "x", { original: ["1.00", "USD"] })],
      "original_sign_mismatch",
    );
    assert.equal(await count(env.db, "transactions"), 0);
  });

  it("errori di struttura: intestazione, colonne, campi, apici, file vuoto, UTF-8", async () => {
    const env = await setupWithAccounts();
    await rejectedWith(
      env,
      [row("2026-01-02", "1.00", "x")],
      "missing_column",
      HEADER.replace(",counterparty", ""),
    );
    await rejectedWith(env, [], "unknown_column", `${HEADER},extra`);
    await rejectedWith(env, [], "duplicate_column", `${HEADER},amount`);
    await rejectedWith(env, ["a,b"], "wrong_field_count");
    await rejectedWith(env, ['"aperto'], "csv_malformed");
    await rejectedWith(env, [], "no_rows");
    const empty = await importFile(env.ctx, {
      accountId: "conto",
      parserId: "cloud-finance-csv",
      fileName: null,
      content: new Uint8Array(),
    });
    assert.ok(empty.issues.some((i) => i.code === "empty_file"));
    const bad = await importFile(env.ctx, {
      accountId: "conto",
      parserId: "cloud-finance-csv",
      fileName: null,
      content: new Uint8Array([0xff, 0xfe, 0x00]),
    });
    assert.ok(bad.issues.some((i) => i.code === "invalid_utf8"));
    assert.equal(await count(env.db, "transactions"), 0);
  });

  it("formato o conto inesistenti: errore esplicito", async () => {
    const env = await setupWithAccounts();
    await assert.rejects(
      importFile(env.ctx, { accountId: "conto", parserId: "revolut", fileName: null, content: csvBytes([]) }),
      /formato sconosciuto/,
    );
    await assert.rejects(importTx(env, "nessuno", [row("2026-01-02", "1.00", "x")]), /conto non trovato/);
    assert.equal(getParser("cloud-finance-csv").verifiedAgainstRealExports, false);
  });

  it("errore durante la scrittura: rollback completo, nessun dato parziale", async () => {
    const env = await setupWithAccounts();
    const failing = new FailingDb(env.db, 8);
    const ctx = { ...env.ctx, db: failing };
    const r = await importFile(ctx, {
      accountId: "conto",
      parserId: "cloud-finance-csv",
      fileName: "f.csv",
      content: csvBytes(BASE),
    });
    assert.equal(r.status, "rejected");
    assert.ok(r.issues.some((i) => i.code === "write_failed"));
    assert.equal(await count(env.db, "transactions"), 0);
    assert.equal(await count(env.db, "raw_records"), 0);
    assert.equal(await count(env.db, "import_runs", "status = 'committed'"), 0);
    // dopo il rollback lo stesso file si importa normalmente
    const ok = await importTx(env, "conto", BASE);
    assert.equal(ok.status, "committed");
    assert.equal(ok.counts.created, 5);
  });
});
