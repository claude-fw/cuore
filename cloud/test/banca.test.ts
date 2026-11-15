/**
 * Aggiornamento dalla banca nel cloud (banca.ts): spento senza segreti, fonte finta (nessuna rete, nessuna chiave),
 * avvisi sul consenso, prima lettura dal giorno indicato. La firma verso Enable Banking e' provata nei test del motore.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { stmt } from "../../finance/src/db/types.ts";
import { createAccount } from "../../finance/src/services/accounts.ts";
import { addManualBalance } from "../../finance/src/services/balances.ts";
import type { AccountDataSource } from "../../finance/src/sync/source.ts";
import { setupWithAccounts } from "../../finance/test/helpers.ts";
import {
  AUTOMATION_TABLES,
  aggiornaBancaAMano,
  aggiornaBancaSeServe,
  BANCA_CRON,
  BASE_CRON,
  plan,
} from "../src/automazioni.ts";
import { giorniAllaScadenza, leggiBanca, sincronizzaBanca, specchiaDeposito } from "../src/banca.ts";

class BancaFinta implements AccountDataSource {
  readonly provider = "enable-banking";
  letture: { conto: string; da: string }[] = [];
  async transactions(conto: string, da: string) {
    this.letture.push({ conto, da });
    return {
      continuationKey: null,
      transactions: [
        {
          transaction_id: "b1",
          transaction_amount: { amount: "3.75", currency: "EUR" },
          credit_debit_indicator: "DBIT",
          status: "BOOK",
          booking_date: "2026-11-04",
          transaction_date: "2026-11-04",
          remittance_information: ["Movimento SINTETICO"],
          creditor: { name: "Negozio Esempio" },
        },
      ],
    };
  }
  async balances() {
    return [
      {
        balance_type: "CLBD",
        reference_date: "2026-11-04",
        balance_amount: { amount: "10.00", currency: "EUR" },
      },
    ];
  }
}

describe("banca nel cloud", () => {
  it("spenta senza i due segreti; segreti presenti ma sbagliati = errore", () => {
    assert.equal(leggiBanca({}), null);
    assert.equal(leggiBanca({ BANCA_CHIAVE: "x" }), null);
    assert.throws(() => leggiBanca({ BANCA_CHIAVE: "x", BANCA_COLLEGAMENTI: '{"conti":[]}' }), /appId/);
    assert.equal(giorniAllaScadenza("2026-11-21T00:00:00Z", "2026-11-11"), 10);
    assert.equal(giorniAllaScadenza(null, "2026-11-11"), null);
  });

  it("importa dalla fonte, parte dal giorno indicato, avvisa se il consenso scade, salta i consensi scaduti", async () => {
    const env = await setupWithAccounts();
    const banca = new BancaFinta();
    const r = await sincronizzaBanca(
      env.ctx,
      {
        chiave: "non usata",
        config: {
          appId: "app-SINTETICA",
          conti: [
            { conto: "conto", uid: "acc-1", scadenza: "2026-11-20T00:00:00Z", dal: "2026-11-03" },
            { conto: "conto", uid: "acc-scaduto", scadenza: "2026-11-01T00:00:00Z" },
          ],
        },
      },
      "2026-11-11",
      banca,
    );
    assert.deepEqual(
      r.conti.map((c) => [c.status, c.fetched.transactions]),
      [["importata", 1]],
    );
    assert.deepEqual(banca.letture[0], { conto: "acc-1", da: "2026-11-03" });
    assert.ok(banca.letture.every((l) => l.conto !== "acc-scaduto"));
    assert.deepEqual(r.avvisi, [
      "conto: il consenso scade fra 9 giorni",
      "conto: consenso scaduto, va rinnovato in banca",
    ]);
  });

  it("salvadanaio non condiviso: i passaggi del conto si ricopiano una volta sola e il saldo si stima", async () => {
    const env = await setupWithAccounts();
    await createAccount(env.ctx, {
      id: "salvadanaio",
      name: "Deposito DEMO",
      type: "savings",
      currency: "EUR",
      holderRole: "owner",
    });
    await addManualBalance(env.ctx, "salvadanaio", "2026-11-09", 10000);
    const csv = [
      "transaction_id,transaction_date,booking_date,status,amount,currency,description,counterparty,original_amount,original_currency",
      "x1,2026-11-10,2026-11-10,booked,-1.00,EUR,A EUR Conto deposito senza vincoli,,,",
      "x2,2026-11-11,2026-11-11,booked,-3.50,EUR,Caffè DEMO,Bar DEMO,,",
      "x3,2026-11-11,2026-11-11,booked,0.50,EUR,Da EUR Conto deposito senza vincoli,,,",
    ].join("\n");
    const { importFile } = await import("../../finance/src/import/importer.ts");
    await importFile(env.ctx, {
      accountId: "conto",
      parserId: "cloud-finance-csv",
      fileName: "c.csv",
      content: new TextEncoder().encode(csv),
    });
    await specchiaDeposito(env.ctx, "conto", "salvadanaio", "2026-11-12");
    await specchiaDeposito(env.ctx, "conto", "salvadanaio", "2026-11-12");
    const dep = await env.db.all<{ source_txn_id: string; amount_minor: number }>(
      "SELECT source_txn_id, amount_minor FROM transactions WHERE account_id = 'salvadanaio' ORDER BY transaction_date",
    );
    assert.deepEqual(
      dep.map((d) => d.amount_minor),
      [100, -50],
    );
    const b = await env.db.first<{ balance_minor: number }>(
      "SELECT balance_minor FROM balance_observations WHERE account_id = 'salvadanaio' AND as_of_date = '2026-11-12'",
    );
    assert.equal(b?.balance_minor, 10050);
  });

  it("aggiorna a mano: al massimo 2 volte al giorno, poi un messaggio chiaro", async () => {
    const env = await setupWithAccounts();
    await env.db.batch(AUTOMATION_TABLES.map((sql) => stmt(sql)));
    let giri = 0;
    const a = {
      ctx: env.ctx,
      vapid: null,
      fetch: async () => new Response(null),
      banca: async () => {
        giri++;
        return { avvisi: [] };
      },
    };
    const ms = Date.parse("2026-11-12T19:00:00Z");
    assert.equal((await aggiornaBancaAMano(a, ms)).stato, "succeeded");
    assert.equal((await aggiornaBancaAMano(a, ms)).stato, "succeeded");
    await assert.rejects(aggiornaBancaAMano(a, ms), /2 volte/);
    assert.equal(giri, 2);
    await assert.rejects(aggiornaBancaAMano({ ...a, banca: undefined }, ms), /non è collegata/);
  });

  it("all'apertura dell'app: legge solo se l'ultima lettura ha più di 3 ore, mai un errore", async (t) => {
    const env = await setupWithAccounts();
    await env.db.batch(AUTOMATION_TABLES.map((sql) => stmt(sql)));
    let giri = 0;
    const a = {
      ctx: env.ctx,
      vapid: null,
      fetch: async () => new Response(null),
      banca: async () => {
        giri++;
        return { avvisi: [] };
      },
    };
    const ms = Date.parse("2026-11-14T07:00:00Z");
    t.mock.timers.enable({ apis: ["Date"], now: ms }); // l'ora dei giri registrati viene dall'orologio
    const alle = async (ore: number) => {
      t.mock.timers.setTime(ms + ore * 3_600_000);
      return (await aggiornaBancaSeServe(a, ms + ore * 3_600_000)) as Record<string, unknown>;
    };
    assert.equal((await alle(0)).stato, "succeeded", "nessuna lettura: si legge");
    assert.equal((await alle(1)).saltato, "recente");
    assert.equal((await alle(4)).stato, "succeeded");
    assert.equal((await alle(8)).saltato, "limite");
    assert.equal(giri, 2);
    const spenta = (await aggiornaBancaSeServe({ ...a, banca: undefined }, ms)) as Record<string, unknown>;
    assert.equal(spenta.saltato, "banca_non_collegata");
  });

  it("giri automatici alle 11 e alle 20 ora italiana (solare e legale), mai con gli altri Cron", async () => {
    const env = await setupWithAccounts();
    await env.db.batch(AUTOMATION_TABLES.map((sql) => stmt(sql)));
    const a = {
      ctx: env.ctx,
      vapid: null,
      fetch: async () => new Response(null),
      banca: async () => ({ avvisi: [] }),
    };
    for (const [utc, cron] of [
      ["2026-12-01T09:00:00Z", BANCA_CRON], // 10:00 in Italia: niente
      ["2026-12-01T10:00:00Z", BANCA_CRON], // 11:00
      ["2026-12-01T11:00:00Z", BASE_CRON], // 12:00, Cron storico: niente
      ["2026-12-01T19:00:00Z", BANCA_CRON], // 20:00
      ["2026-07-01T09:00:00Z", BANCA_CRON], // 11:00 con l'ora legale
      ["2026-07-01T10:00:00Z", BANCA_CRON], // 12:00 con l'ora legale: niente
    ] as const)
      await plan(a, Date.parse(utc), cron);
    const giri = await env.db.all<{ key: string }>(
      "SELECT key FROM cloud_lavori WHERE key LIKE 'banca:%' ORDER BY key",
    );
    assert.deepEqual(
      giri.map((r) => r.key),
      ["banca:2026-07-01:mattina", "banca:2026-12-01:mattina", "banca:2026-12-01:sera"],
    );
  });
});
