/**
 * Aggiornamento automatico dalla banca (C-05) nel cloud: Enable Banking in sola lettura, fino a due volte al giorno.
 * Spento finche' mancano i due segreti del Worker:
 * - `BANCA_CHIAVE`: chiave privata dell'applicazione Enable Banking (PEM PKCS#8);
 * - `BANCA_COLLEGAMENTI`: JSON `{"appId", "conti": [{"conto", "uid", "scadenza"}]}` dal consenso fatto sul Mac
 *   (`finance/scripts/bank-sync.ts consenso` e `sessione`), con il login di chi e' titolare del conto in banca.
 * Ogni conto usa `syncAccount` del motore: finestra con sovrapposizione, antiduplicati, rilevamento degli spostamenti.
 */
import type { Ctx } from "../../finance/src/context.ts";
import { importFile } from "../../finance/src/import/importer.ts";
import { EnableBankingSource } from "../../finance/src/sync/enable-banking.ts";
import { type SyncReport, syncAccount } from "../../finance/src/sync/runner.ts";
import type { AccountDataSource } from "../../finance/src/sync/source.ts";

export interface BancaConfig {
  appId: string;
  /** `dal`: primo giorno della prima lettura (il giorno dopo l'ultimo estratto caricato), per non avere doppioni;
   * `specchio`: conto salvadanaio che la banca non condivide, ricostruito dai passaggi visibili su questo conto */
  conti: { conto: string; uid: string; scadenza: string | null; dal?: string; specchio?: string }[];
}

/** null = automazione spenta (segreti assenti); errore = segreti presenti ma non validi. */
export function leggiBanca(env: { BANCA_CHIAVE?: string; BANCA_COLLEGAMENTI?: string }): {
  config: BancaConfig;
  chiave: string;
} | null {
  if (!env.BANCA_CHIAVE || !env.BANCA_COLLEGAMENTI) return null;
  const c = JSON.parse(env.BANCA_COLLEGAMENTI) as BancaConfig;
  if (typeof c?.appId !== "string" || !Array.isArray(c.conti) || c.conti.some((x) => !x.conto || !x.uid))
    throw new Error("BANCA_COLLEGAMENTI non valido: servono appId e conti [{conto, uid}]");
  return { config: c, chiave: env.BANCA_CHIAVE };
}

/** Giorni che mancano alla scadenza del consenso (negativo = scaduto), null se non dichiarata. */
export function giorniAllaScadenza(scadenza: string | null, oggi: string): number | null {
  if (!scadenza) return null;
  return Math.floor((Date.parse(scadenza) - Date.parse(`${oggi}T00:00:00Z`)) / 86_400_000);
}

export async function sincronizzaBanca(
  ctx: Ctx,
  banca: { config: BancaConfig; chiave: string },
  oggi: string,
  /** fonte dei dati: Enable Banking con la chiave dei segreti (iniettabile nei test) */
  source?: AccountDataSource,
): Promise<{ conti: Pick<SyncReport, "accountId" | "status" | "fetched" | "error">[]; avvisi: string[] }> {
  const fonte = source ?? new EnableBankingSource({ appId: banca.config.appId, privateKeyPem: banca.chiave });
  const conti = [];
  const avvisi: string[] = [];
  for (const c of banca.config.conti) {
    const giorni = giorniAllaScadenza(c.scadenza, oggi);
    if (giorni !== null && giorni < 0) {
      avvisi.push(`${c.conto}: consenso scaduto, va rinnovato in banca`);
      continue;
    }
    if (giorni !== null && giorni <= 14) avvisi.push(`${c.conto}: il consenso scade fra ${giorni} giorni`);
    const historyDays = c.dal ? Math.max(1, (Date.parse(oggi) - Date.parse(c.dal)) / 86_400_000) : undefined;
    const r = await syncAccount(ctx, {
      accountId: c.conto,
      accountRef: c.uid,
      source: fonte,
      today: oggi,
      ...(historyDays ? { historyDays } : {}),
    });
    conti.push({ accountId: r.accountId, status: r.status, fetched: r.fetched, error: r.error });
    if (c.specchio && r.status !== "rifiutata") await specchiaDeposito(ctx, c.conto, c.specchio, oggi);
  }
  return { conti, avvisi };
}

const PAROLA_DEPOSITO = "%conto deposito senza vincoli%";
const csv = (head: string, rows: string[]) => new TextEncoder().encode([head, ...rows].join("\n"));
const eur = (minor: number) => (minor / 100).toFixed(2);

/**
 * Salvadanaio che la banca non condivide (es. deposito Revolut): ogni passaggio «conto deposito» visibile sul conto,
 * dopo l'ultimo movimento vero del deposito, si ricopia nel deposito con il segno opposto (id `specchio:<id>`, quindi
 * mai due volte), e il saldo del deposito si stima: ultimo saldo osservato + passaggi successivi. Gli interessi (pochi
 * centesimi) non si vedono: li corregge il prossimo estratto.
 */
export async function specchiaDeposito(ctx: Ctx, conto: string, deposito: string, oggi: string) {
  const ultimoVero =
    (
      await ctx.db.first<{ d: string | null }>(
        `SELECT MAX(COALESCE(booking_date, transaction_date)) AS d FROM transactions
         WHERE account_id = ? AND (source_txn_id IS NULL OR source_txn_id NOT LIKE 'specchio:%')`,
        [deposito],
      )
    )?.d ?? "0000-00-00";
  const righe = await ctx.db.all<{ id: string; d: string; a: number; descr: string }>(
    `SELECT id, COALESCE(booking_date, transaction_date) AS d, amount_minor AS a, description AS descr
     FROM transactions WHERE account_id = ? AND status = 'booked' AND lower(description) LIKE ?
       AND COALESCE(booking_date, transaction_date) > ? ORDER BY d, id`,
    [conto, PAROLA_DEPOSITO, ultimoVero],
  );
  if (righe.length > 0)
    await importFile(ctx, {
      accountId: deposito,
      parserId: "cloud-finance-csv",
      fileName: `specchio-${oggi}.csv`,
      content: csv(
        "transaction_id,transaction_date,booking_date,status,amount,currency,description,counterparty,original_amount,original_currency",
        righe.map(
          (t) =>
            `specchio:${t.id},${t.d},${t.d},booked,${eur(-t.a)},EUR,"Dal conto: ${t.descr.replace(/"/g, "'")}",,,`,
        ),
      ),
    });
  const base = await ctx.db.first<{ d: string; b: number }>(
    `SELECT as_of_date AS d, balance_minor AS b FROM balance_observations
     WHERE account_id = ? AND as_of_date < ? ORDER BY as_of_date DESC, created_at DESC LIMIT 1`,
    [deposito, oggi],
  );
  if (base === null) return;
  const dopo = await ctx.db.first<{ s: number | null }>(
    `SELECT SUM(amount_minor) AS s FROM transactions WHERE account_id = ? AND status = 'booked'
       AND COALESCE(booking_date, transaction_date) > ? AND COALESCE(booking_date, transaction_date) <= ?`,
    [deposito, base.d, oggi],
  );
  await importFile(ctx, {
    accountId: deposito,
    parserId: "cloud-finance-balances-csv",
    fileName: `saldo-stimato-${oggi}.csv`,
    content: csv("date,balance,currency", [`${oggi},${eur(base.b + (dopo?.s ?? 0))},EUR`]),
  });
}
