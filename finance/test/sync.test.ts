/**
 * Sincronizzazione Open Banking (src/sync): parser del pacchetto, esecutore incrementale, client Enable Banking.
 * Solo dati SINTETICI e una fonte finta in memoria; il client usa un trasporto finto e una chiave generata nel test.
 * Nessuna rete, nessuna banca, nessun dato reale.
 */
import assert from "node:assert/strict";
import type { webcrypto } from "node:crypto";
import { describe, it } from "node:test";
import { createAccount, createInstitution } from "../src/services/accounts.ts";
import { EnableBankingSource, type Transport } from "../src/sync/enable-banking.ts";
import { encodePayload, openBankingJsonV1, PAYLOAD_SCHEMA } from "../src/sync/open-banking-json.ts";
import { addDays, lastWindow, syncAccount, syncAccounts } from "../src/sync/runner.ts";
import {
  type AccountDataSource,
  type FetchStrategy,
  SourceError,
  type TransactionsPage,
} from "../src/sync/source.ts";
import { count, setupWithAccounts, type TestEnv } from "./helpers.ts";

type Tx = Record<string, unknown>;
const tx = (id: string | null, date: string, amount: string, dir: "CRDT" | "DBIT", extra: Tx = {}): Tx => ({
  ...(id ? { transaction_id: id } : {}),
  transaction_amount: { amount, currency: "EUR" },
  credit_debit_indicator: dir,
  status: "BOOK",
  booking_date: date,
  transaction_date: date,
  remittance_information: [`Movimento SINTETICO ${id ?? date}`],
  ...(dir === "DBIT" ? { creditor: { name: "Negozio Esempio" } } : { debtor: { name: "Azienda Esempio" } }),
  ...extra,
});
const clbd = (day: string, amount: string) => ({
  balance_type: "CLBD",
  reference_date: day,
  balance_amount: { amount, currency: "EUR" },
});

/** Fonte finta: transazioni per data, pagine da `pageSize`, errori programmati per chiamata. */
class FakeSource implements AccountDataSource {
  readonly provider = "finto";
  calls: { from: string; to: string; key: string | null; strategy: FetchStrategy }[] = [];
  /** errore restituito alla strategia `default` (es. periodo non disponibile) */
  defaultError: SourceError | null = null;
  emptyFirst = false;
  failAt: Map<number, SourceError> = new Map();
  balanceError: SourceError | null = null;
  loopKey = false;
  data: Tx[];
  bal: unknown[];
  private readonly pageSize: number;
  constructor(data: Tx[], bal: unknown[] = [], pageSize = 2) {
    this.data = data;
    this.bal = bal;
    this.pageSize = pageSize;
  }
  async transactions(
    _ref: string,
    from: string,
    to: string,
    key: string | null,
    strategy: FetchStrategy = "default",
  ): Promise<TransactionsPage> {
    this.calls.push({ from, to, key, strategy });
    const err = this.failAt.get(this.calls.length);
    if (err) throw err;
    if (strategy === "default" && this.defaultError) throw this.defaultError;
    if (this.emptyFirst && key === null) return { transactions: [], continuationKey: "0" }; // pagina vuota con chiave
    // `longest`: dal primo movimento disponibile, `to` ignorato (come documentato da Enable Banking)
    const inWin = this.data.filter((t) => {
      const d = String(t.transaction_date ?? t.booking_date);
      return strategy === "longest" ? true : d >= from && d <= to;
    });
    const start = key === null ? 0 : Number(key);
    const next = start + this.pageSize < inWin.length ? String(start + this.pageSize) : null;
    return {
      transactions: inWin.slice(start, start + this.pageSize),
      continuationKey: this.loopKey ? "sempre" : next,
    };
  }
  async balances(): Promise<readonly unknown[]> {
    if (this.balanceError) throw this.balanceError;
    return this.bal;
  }
}

const TODAY = "2026-03-31";
const sync = (env: TestEnv, source: AccountDataSource, today = TODAY, accountId = "conto") =>
  syncAccount(env.ctx, { accountId, accountRef: "uid-sintetico", source, today });
const txns = (env: TestEnv, account = "conto") =>
  env.db.all<{
    source_txn_id: string | null;
    status: string;
    amount_minor: number;
    booking_date: string | null;
    counterparty: string | null;
    source_type: string | null;
  }>(
    "SELECT source_txn_id, status, amount_minor, booking_date, counterparty, source_type FROM transactions WHERE account_id = ? ORDER BY transaction_date, source_txn_id",
    [account],
  );

describe("sincronizzazione: pacchetto open-banking-json", () => {
  const parse = (transactions: unknown[], balances: unknown[] = []) =>
    openBankingJsonV1.parseFile(
      encodePayload({
        schema: PAYLOAD_SCHEMA,
        provider: "finto",
        window: { from: "2026-01-01", to: "2026-01-31" },
        transactions,
        balances,
      }),
      { accountCurrency: "EUR" },
    );

  it("segno dall'indicatore, stati, esclusioni dichiarate, solo saldi di chiusura", async () => {
    const r = await parse(
      [
        tx("t1", "2026-01-02", "1200.00", "CRDT"),
        tx("t2", "2026-01-03", "45.20", "DBIT", {
          bank_transaction_code: { code: "PMNT", sub_code: "CCRD" },
        }),
        {
          ...tx("t3", "2026-01-04", "9.99", "DBIT"),
          status: "PDNG",
          booking_date: null,
          merchant_category_code: "5812",
        },
        { ...tx("t4", "2026-01-05", "5.00", "DBIT"), status: "CNCL" },
      ],
      [
        clbd("2026-01-31", "1144.81"),
        { balance_type: "ITAV", balance_amount: { amount: "1100.00", currency: "EUR" } },
      ],
    );
    assert.deepEqual(r.issues, []);
    assert.deepEqual(
      r.transactions.map((t) => [t.sourceTxnId, t.status, t.amountMinor, t.bookingDate]),
      [
        ["t1", "booked", 120000, "2026-01-02"],
        ["t2", "booked", -4520, "2026-01-03"],
        ["t3", "pending", -999, null],
      ],
    );
    assert.deepEqual(
      [r.transactions[1]?.counterparty, r.transactions[1]?.sourceType, r.transactions[2]?.sourceType],
      ["Negozio Esempio", "PMNT/CCRD", "MCC 5812"],
    );
    assert.equal(r.excluded?.[0]?.sourceTxnId, "t4");
    assert.match(r.excluded?.[0]?.reason ?? "", /CNCL/);
    assert.deepEqual(
      r.balances.map((b) => [b.asOfDate, b.balanceMinor]),
      [["2026-01-31", 114481]],
    );
    assert.equal(r.notices?.[0]?.code, "balance_type_ignored");
    assert.ok(JSON.parse(r.transactions[0]?.raw.json ?? "{}").transaction_amount); // dato grezzo integro
  });

  it("senza saldo di chiusura: ripiego sul saldo che si vede nell'app (data del saldo o fine della finestra)", async () => {
    const solo = (balances: unknown[]) => parse([tx("a", "2026-01-10", "1.00", "DBIT")], balances);
    const rev = await solo([
      {
        balance_type: "ITAV",
        reference_date: "2026-01-30",
        balance_amount: { amount: "168.44", currency: "EUR" },
      },
    ]);
    assert.deepEqual(rev.issues, []);
    assert.deepEqual(
      rev.balances.map((x) => [x.asOfDate, x.balanceMinor]),
      [["2026-01-30", 16844]],
    );
    assert.equal(rev.notices?.[0]?.code, "balance_type_fallback");
    const mps = await solo([
      { balance_type: "OTHR", balance_amount: { amount: "1758.62", currency: "EUR" } },
      { balance_type: "XPCD", balance_amount: { amount: "1758.60", currency: "EUR" } },
    ]);
    assert.deepEqual(
      mps.balances.map((x) => [x.asOfDate, x.balanceMinor]),
      [["2026-01-31", 175860]],
      "XPCD prima di OTHR",
    );
  });

  it("problemi espliciti: valuta, segno contraddittorio, direzione o data assenti, pacchetto non conforme", async () => {
    const r = await parse([
      { ...tx("a", "2026-01-02", "1.00", "CRDT"), transaction_amount: { amount: "1.00", currency: "USD" } },
      tx("b", "2026-01-02", "-1.00", "CRDT"),
      { ...tx("c", "2026-01-02", "1.00", "CRDT"), credit_debit_indicator: undefined },
      { ...tx("d", "2026-01-02", "1.00", "CRDT"), booking_date: undefined, transaction_date: undefined },
      tx("e", "2026-01-02", "1.001", "DBIT"),
    ]);
    assert.deepEqual(r.issues.map((i) => i.code).sort(), [
      "currency_mismatch",
      "missing_booking_date",
      "missing_date",
      "missing_direction",
      "sign_conflict",
      "too_many_decimals",
    ]);
    const bad = await openBankingJsonV1.parseFile(new TextEncoder().encode('{"schema":"altro"}'), {
      accountCurrency: "EUR",
    });
    assert.equal(bad.issues[0]?.code, "payload_schema");
  });
});

describe("sincronizzazione: esecutore incrementale", () => {
  it("prima sincronizzazione: 90 giorni, pagine complete, saldi, provenienza nella finestra", async () => {
    const env = await setupWithAccounts();
    const src = new FakeSource(
      [
        tx("t1", "2026-01-15", "100.00", "CRDT"),
        tx("t2", "2026-03-01", "20.00", "DBIT"),
        tx("t3", "2026-03-30", "5.00", "DBIT"),
        tx("vecchio", "2025-12-01", "1.00", "DBIT"), // prima del limite di 90 giorni: arriva con `longest`
      ],
      [clbd("2026-03-30", "75.00")],
    );
    const r = await sync(env, src);
    assert.equal(r.status, "importata");
    assert.deepEqual(
      [r.window, r.firstSync, r.gap, r.pages, r.fetched],
      [{ from: "2025-12-01", to: TODAY }, true, null, 2, { transactions: 4, balances: 1 }],
    );
    assert.deepEqual([r.import?.counts.created, r.import?.counts.balancesCreated], [4, 1]);
    assert.deepEqual(await lastWindow(env.ctx, "conto", "finto"), { from: "2025-12-01", to: TODAY });
    assert.equal(await count(env.db, "raw_records"), 5);
    const run = await env.db.first<{ parser_id: string; file_name: string }>(
      "SELECT parser_id, file_name FROM import_runs",
    );
    assert.deepEqual(
      { ...run },
      { parser_id: "open-banking-json", file_name: `sync:finto:2025-12-01..${TODAY}` },
    );
  });

  it("incrementale: sovrapposizione senza doppioni, pending -> booked con audit, nuovi movimenti", async () => {
    const env = await setupWithAccounts();
    const src = new FakeSource([
      tx("t1", "2026-03-25", "10.00", "DBIT"),
      { ...tx("p1", "2026-03-30", "7.50", "DBIT"), status: "PDNG", booking_date: null },
    ]);
    await sync(env, src);
    src.data = [
      tx("t1", "2026-03-25", "10.00", "DBIT"),
      tx("p1", "2026-03-30", "7.50", "DBIT", { booking_date: "2026-04-01" }),
      tx("t9", "2026-04-02", "3.00", "DBIT"),
    ];
    const r = await sync(env, src, "2026-04-02");
    assert.equal(r.window.from, addDays(TODAY, -10)); // ultima copertura - 10 giorni: t1 riletto
    assert.equal(r.firstSync, false);
    assert.deepEqual(
      [r.import?.counts.created, r.import?.counts.updated, r.import?.counts.unchanged],
      [1, 1, 1],
    );
    assert.deepEqual(
      (await txns(env)).map((t) => [t.source_txn_id, t.status, t.booking_date]),
      [
        ["t1", "booked", "2026-03-25"],
        ["p1", "booked", "2026-04-01"],
        ["t9", "booked", "2026-04-02"],
      ],
    );
    assert.equal(await count(env.db, "audit_log", "entity = 'transaction' AND action = 'book'"), 1);
    const again = await sync(env, src, "2026-04-02"); // stesso giorno, stessi dati
    assert.equal(again.status, "nessun_dato_nuovo");
    assert.deepEqual(
      [again.import?.status, again.import?.counts.created, again.import?.counts.updated],
      ["committed", 0, 0],
    );
    assert.equal(await count(env.db, "transactions"), 3);
  });

  it("errore a meta' paginazione: nessuna scrittura, ripresa dalla stessa finestra al giro successivo", async () => {
    const env = await setupWithAccounts();
    const src = new FakeSource([
      tx("a", "2026-03-01", "1.00", "DBIT"),
      tx("b", "2026-03-02", "1.00", "DBIT"),
      tx("c", "2026-03-03", "1.00", "DBIT"),
    ]);
    src.failAt.set(2, new SourceError("temporary", "rete", 503));
    const r = await sync(env, src);
    assert.deepEqual([r.status, r.error?.kind, r.import, r.pages], ["interrotta", "temporary", null, 1]);
    assert.equal(await count(env.db, "transactions"), 0);
    assert.equal(await count(env.db, "import_runs"), 0);
    const ok = await sync(env, src);
    assert.deepEqual([ok.status, ok.window.from, ok.import?.counts.created], ["importata", r.window.from, 3]);
  });

  it("consenso scaduto, limiti, paginazione infinita, troppe pagine: interrotta con la categoria", async () => {
    const env = await setupWithAccounts();
    const expired = new FakeSource([tx("a", "2026-03-01", "1.00", "DBIT")]);
    expired.failAt.set(1, new SourceError("consent_expired", "consenso", 401));
    assert.equal((await sync(env, expired)).error?.kind, "consent_expired");
    const loop = new FakeSource([tx("a", "2026-03-01", "1.00", "DBIT")]);
    loop.loopKey = true;
    assert.equal((await sync(env, loop)).error?.kind, "invalid_response");
    const many = new FakeSource(
      Array.from({ length: 9 }, (_, i) => tx(`m${i}`, "2026-03-01", "1.00", "DBIT")),
      [],
      1,
    );
    const r = await syncAccount(env.ctx, {
      accountId: "conto",
      accountRef: "x",
      source: many,
      today: TODAY,
      maxPages: 3,
    });
    assert.deepEqual([r.status, r.error?.kind, r.pages], ["interrotta", "invalid_response", 3]);
    assert.equal(await count(env.db, "transactions"), 0);
  });

  it("saldi non leggibili: movimenti importati, saldi dichiarati non letti", async () => {
    const env = await setupWithAccounts();
    const src = new FakeSource([tx("a", "2026-03-01", "1.00", "DBIT")]);
    src.balanceError = new SourceError("rate_limited", "limite", 429);
    const r = await sync(env, src);
    assert.deepEqual([r.status, r.balancesRead, r.import?.counts.created], ["importata", false, 1]);
  });

  it("pacchetto rifiutato (valuta diversa): niente scritto, la finestra non avanza", async () => {
    const env = await setupWithAccounts();
    const src = new FakeSource([
      { ...tx("a", "2026-03-01", "1.00", "DBIT"), transaction_amount: { amount: "1.00", currency: "USD" } },
    ]);
    const r = await sync(env, src);
    assert.deepEqual([r.status, r.import?.status, r.import?.issues], ["rifiutata", "rejected", 1]);
    assert.equal(await count(env.db, "transactions"), 0);
    assert.equal(await lastWindow(env.ctx, "conto", "finto"), null);
  });

  it("copertura troppo vecchia: buco dichiarato, nessun dato inventato", async () => {
    const env = await setupWithAccounts();
    const src = new FakeSource([tx("a", "2026-01-10", "1.00", "DBIT")]);
    await sync(env, src, "2026-01-31");
    const r = await sync(env, src, "2026-09-30");
    assert.deepEqual(r.gap, { from: "2026-01-31", to: addDays("2026-09-30", -90) });
    assert.equal(r.window.from, addDays("2026-09-30", -90));
  });

  it("trasferimento tra due banche: proposto, mai confermato; un conto in errore non ferma l'altro", async () => {
    const env = await setupWithAccounts();
    await createInstitution(env.ctx, "rev", "Revolut DEMO");
    await createInstitution(env.ctx, "mps", "MPS DEMO");
    await createAccount(env.ctx, {
      id: "rev-eur",
      name: "Rev DEMO",
      type: "current",
      currency: "EUR",
      holderRole: "owner",
      institutionId: "rev",
    });
    await createAccount(env.ctx, {
      id: "mps-eur",
      name: "MPS DEMO",
      type: "current",
      currency: "EUR",
      holderRole: "owner",
      institutionId: "mps",
    });
    const out = new FakeSource([
      tx("r1", "2026-03-10", "250.00", "DBIT", { creditor: { name: "Titolare DEMO" } }),
    ]);
    const inn = new FakeSource([
      tx("m1", "2026-03-11", "250.00", "CRDT", { debtor: { name: "Titolare DEMO" } }),
    ]);
    const broken = new FakeSource([]);
    broken.failAt.set(1, new SourceError("consent_expired", "consenso", 403));
    const reports = await syncAccounts(env.ctx, [
      { accountId: "rev-eur", accountRef: "u1", source: out, today: TODAY },
      { accountId: "conto", accountRef: "u3", source: broken, today: TODAY },
      { accountId: "mps-eur", accountRef: "u2", source: inn, today: TODAY },
    ]);
    assert.deepEqual(
      reports.map((r) => r.status),
      ["importata", "interrotta", "importata"],
    );
    const links = await env.db.all<{ type: string; status: string }>(
      "SELECT type, status FROM transaction_links",
    );
    assert.deepEqual(
      links.map((l) => ({ ...l })),
      [{ type: "transfer", status: "proposed" }],
    );
  });
});

describe("sincronizzazione: client Enable Banking (trasporto finto, nessuna rete)", () => {
  async function keys() {
    const pair = (await crypto.subtle.generateKey(
      {
        name: "RSASSA-PKCS1-v1_5",
        modulusLength: 2048,
        publicExponent: new Uint8Array([1, 0, 1]),
        hash: "SHA-256",
      },
      true,
      ["sign", "verify"],
    )) as webcrypto.CryptoKeyPair;
    const der = Buffer.from(await crypto.subtle.exportKey("pkcs8", pair.privateKey)).toString("base64");
    return {
      pem: `-----BEGIN PRIVATE KEY-----\n${der.match(/.{1,64}/g)?.join("\n")}\n-----END PRIVATE KEY-----\n`,
      pub: pair.publicKey,
    };
  }
  const reply = (status: number, body: unknown) => ({ status, json: async () => body });

  it("JWT RS256 verificabile, richieste e paginazione nella forma documentata", async () => {
    const { pem, pub } = await keys();
    const seen: { url: string; method: string; auth: string; body?: string }[] = [];
    const transport: Transport = async (url, init) => {
      seen.push({ url, method: init.method, auth: init.headers.Authorization ?? "", body: init.body });
      if (url.includes("/transactions"))
        return reply(
          200,
          url.includes("continuation_key")
            ? { transactions: [tx("b", "2026-03-02", "1.00", "DBIT")] }
            : { transactions: [tx("a", "2026-03-01", "1.00", "DBIT")], continuation_key: "k2" },
        );
      if (url.endsWith("/balances")) return reply(200, { balances: [clbd("2026-03-02", "10.00")] });
      if (url.endsWith("/auth")) return reply(200, { url: "https://banca.example/consenso" });
      if (url.endsWith("/sessions"))
        return reply(200, {
          session_id: "s1",
          access: { valid_until: "2026-09-01T00:00:00+02:00" },
          accounts: [
            {
              uid: "acc-1",
              currency: "EUR",
              name: "Conto DEMO",
              account_id: { iban: "IT60 X054 2811 1010 0000 0012 345678" },
            },
          ],
        });
      return reply(404, {});
    };
    const eb = new EnableBankingSource({
      appId: "app-SINTETICA",
      privateKeyPem: pem,
      transport,
      nowSeconds: () => 1_800_000_000,
    });
    const env = await setupWithAccounts();
    const r = await syncAccount(env.ctx, {
      accountId: "conto",
      accountRef: "acc-1",
      source: eb,
      today: "2026-03-05",
    });
    assert.deepEqual(
      [r.status, r.pages, r.import?.counts.created, r.import?.counts.balancesCreated],
      ["importata", 2, 2, 1],
    );
    assert.ok(
      seen[0]?.url.startsWith(
        "https://api.enablebanking.com/accounts/acc-1/transactions?date_from=2025-12-05&date_to=2026-03-05",
      ),
    );
    assert.ok(seen[1]?.url.endsWith("&continuation_key=k2"));
    const [h, p, s] = (seen[0]?.auth ?? "").replace("Bearer ", "").split(".");
    const dec = (x = "") => JSON.parse(Buffer.from(x, "base64url").toString());
    assert.deepEqual(dec(h), { typ: "JWT", alg: "RS256", kid: "app-SINTETICA" });
    assert.deepEqual(dec(p), {
      iss: "enablebanking.com",
      aud: "api.enablebanking.com",
      iat: 1_800_000_000,
      exp: 1_800_003_600,
    });
    assert.ok(
      await crypto.subtle.verify(
        "RSASSA-PKCS1-v1_5",
        pub,
        Buffer.from(s ?? "", "base64url"),
        new TextEncoder().encode(`${h}.${p}`),
      ),
    );
    assert.deepEqual(
      await eb.startAuthorization({
        aspspName: "Banca DEMO",
        country: "IT",
        redirectUrl: "https://localhost/ritorno",
        validUntil: "2026-09-01T00:00:00+02:00",
        state: "x",
      }),
      { url: "https://banca.example/consenso" },
    );
    assert.deepEqual(JSON.parse(seen.at(-1)?.body ?? "{}").access, {
      valid_until: "2026-09-01T00:00:00+02:00",
    });
    assert.deepEqual(await eb.createSession("codice"), {
      sessionId: "s1",
      validUntil: "2026-09-01T00:00:00+02:00",
      accounts: [{ uid: "acc-1", currency: "EUR", ibanLast4: "5678", name: "Conto DEMO" }],
    });
  });

  it("errori HTTP in categorie, senza il corpo della risposta nel messaggio", async () => {
    const { pem } = await keys();
    for (const [status, kind] of [
      [401, "consent_expired"],
      [403, "consent_expired"],
      [429, "rate_limited"],
      [503, "temporary"],
      [400, "invalid_request"],
    ] as const) {
      const eb = new EnableBankingSource({
        appId: "a",
        privateKeyPem: pem,
        transport: async () => reply(status, { error: "IBAN IT00SEGRETO" }),
      });
      await assert.rejects(
        eb.transactions("u", "2026-01-01", "2026-01-31", null),
        (e: unknown) => e instanceof SourceError && e.kind === kind && !e.message.includes("SEGRETO"),
      );
    }
    const net = new EnableBankingSource({
      appId: "a",
      privateKeyPem: pem,
      transport: async () => {
        throw new TypeError("fetch failed");
      },
    });
    await assert.rejects(
      net.balances("u"),
      (e: unknown) => e instanceof SourceError && e.kind === "temporary",
    );
    const shape = new EnableBankingSource({
      appId: "a",
      privateKeyPem: pem,
      transport: async () => reply(200, { altro: 1 }),
    });
    await assert.rejects(
      shape.transactions("u", "2026-01-01", "2026-01-31", null),
      (e: unknown) => e instanceof SourceError && e.kind === "invalid_response",
    );
    assert.throws(() => new EnableBankingSource({ appId: "", privateKeyPem: pem }), SourceError);
  });
});

describe("sincronizzazione: strategia, periodi e attese (FAQ Enable Banking)", () => {
  it("prima acquisizione con `longest`, aggiornamenti con `default`, pagine vuote con chiave seguite", async () => {
    const env = await setupWithAccounts();
    const src = new FakeSource([
      tx("a", "2026-03-01", "1.00", "DBIT"),
      tx("b", "2026-03-02", "1.00", "DBIT"),
    ]);
    src.emptyFirst = true;
    const first = await sync(env, src);
    assert.deepEqual(
      [first.strategy, first.status, first.import?.counts.created],
      ["longest", "importata", 2],
    );
    assert.equal(src.calls[0]?.strategy, "longest");
    assert.ok(first.pages >= 2); // la pagina vuota non ferma
    src.emptyFirst = false;
    const next = await sync(env, src, "2026-04-02");
    assert.deepEqual([next.strategy, src.calls.at(-1)?.strategy], ["default", "default"]);
  });

  it("periodo non disponibile: una sola nuova lettura con `longest`, nessun doppione", async () => {
    const env = await setupWithAccounts();
    const src = new FakeSource([tx("a", "2026-03-01", "1.00", "DBIT")]);
    await sync(env, src);
    src.defaultError = new SourceError("period_unavailable", "periodo", 422);
    src.data.push(tx("b", "2026-03-31", "2.00", "DBIT"));
    const r = await sync(env, src, "2026-04-01");
    assert.deepEqual(
      [r.status, r.strategy, r.import?.counts.created, r.import?.counts.unchanged],
      ["importata", "longest", 1, 1],
    );
  });

  it("attesa consigliata per categoria: 6 ore dopo un limite, mai riprovare un consenso scaduto da soli", async () => {
    const env = await setupWithAccounts();
    for (const [kind, hours] of [
      ["rate_limited", 6],
      ["temporary", 1],
      ["consent_expired", null],
    ] as const) {
      const src = new FakeSource([]);
      src.failAt.set(1, new SourceError(kind, "x"));
      assert.equal((await sync(env, src)).retryAfterHours, hours, kind);
    }
  });

  it("client: WRONG_TRANSACTIONS_PERIOD riconosciuto senza il messaggio, elenco delle banche, strategia nella richiesta", async () => {
    const { webcrypto: wc } = await import("node:crypto");
    const pair = await wc.subtle.generateKey(
      {
        name: "RSASSA-PKCS1-v1_5",
        modulusLength: 2048,
        publicExponent: new Uint8Array([1, 0, 1]),
        hash: "SHA-256",
      },
      true,
      ["sign", "verify"],
    );
    const der = Buffer.from(await wc.subtle.exportKey("pkcs8", pair.privateKey)).toString("base64");
    const pem = `-----BEGIN PRIVATE KEY-----\n${der}\n-----END PRIVATE KEY-----\n`;
    const urls: string[] = [];
    const eb = new EnableBankingSource({
      appId: "a",
      privateKeyPem: pem,
      transport: async (url) => {
        urls.push(url);
        if (url.includes("/aspsps"))
          return {
            status: 200,
            json: async () => ({
              aspsps: [{ name: "Banca DEMO", country: "IT", maximum_consent_validity: 15552000 }],
            }),
          };
        return {
          status: 422,
          json: async () => ({ error: "WRONG_TRANSACTIONS_PERIOD", message: "IBAN IT00SEGRETO" }),
        };
      },
    });
    await assert.rejects(
      eb.transactions("u", "2026-01-01", "2026-01-31", null),
      (e: unknown) =>
        e instanceof SourceError && e.kind === "period_unavailable" && !e.message.includes("SEGRETO"),
    );
    await assert.rejects(eb.transactions("u", "2026-01-01", "2026-01-31", null, "longest"), SourceError);
    assert.ok(urls[1]?.includes("&strategy=longest"));
    assert.deepEqual(await eb.aspsps("IT"), [{ name: "Banca DEMO", country: "IT", maxConsentDays: 180 }]);
    assert.ok(urls[2]?.endsWith("/aspsps?country=IT"));
  });
});
