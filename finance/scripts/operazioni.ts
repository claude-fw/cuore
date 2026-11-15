/**
 * Operazioni del titolare comuni ai due processi di scrittura: `prova.ts` (copia sintetica) e `scrivi.ts` (database
 * indicato dal ponte reale). Un solo elenco, una sola busta d'uscita, gli stessi codici d'errore.
 *
 * Ingresso `{"op": ..., "input": {...}}`; uscita `{ok: true, risultato}` oppure `{ok: false, errore, codice, motivo}`
 * con `codice` = `non_valido` (ingresso rifiutato dai servizi) | `non_trovato` | `errore`.
 */
import type { Ctx } from "../src/context.ts";
import { NotFoundError, ValidationError } from "../src/context.ts";
import { runQuery } from "../src/query/operations.ts";
import { createAccount } from "../src/services/accounts.ts";
import { budgetStatus, setBudget } from "../src/services/budgets.ts";
import { setClassification } from "../src/services/classification.ts";
import {
  addManualTransaction,
  correctManualTransaction,
  manualHistory,
  voidManualTransaction,
} from "../src/services/manual.ts";
import {
  confirmManualMatch,
  detectManualMatches,
  rejectManualMatch,
  reverseManualMatch,
} from "../src/services/manual-matching.ts";
import { createPlannedPurchase, setPurchaseStatus } from "../src/services/planning.ts";

// biome-ignore lint/suspicious/noExplicitAny: ingresso JSON validato dai servizi
export type Input = Record<string, any>;

export interface OpOptions {
  /** versione dello schema attesa da `consulta` (assente = quella corrente) */
  expectedSchemaVersion?: number;
}

export const OPS: Record<string, (ctx: Ctx, i: Input, o: OpOptions) => Promise<unknown>> = {
  consulta: (ctx, i, o) =>
    runQuery(ctx.db, i.operazione, i.input ?? {}, {
      today: i.oggi,
      ...(o.expectedSchemaVersion === undefined ? {} : { expectedSchemaVersion: o.expectedSchemaVersion }),
    }),
  aggiungi_manuale: (ctx, i) => addManualTransaction(ctx, i as never),
  correggi_manuale: (ctx, i) => correctManualTransaction(ctx, i.id, i.modifiche ?? {}, i.motivo),
  annulla_manuale: (ctx, i) => voidManualTransaction(ctx, i.id, i.motivo),
  storico_manuale: (ctx, i) => manualHistory(ctx, i.id),
  imposta_budget: (ctx, i) => setBudget(ctx, i as never),
  stato_budget: (ctx, i) => budgetStatus(ctx, i.mese, i.valuta),
  rileva_abbinamenti: (ctx) => detectManualMatches(ctx),
  conferma_abbinamento: (ctx, i) => confirmManualMatch(ctx, i.collegamento),
  rifiuta_abbinamento: (ctx, i) => rejectManualMatch(ctx, i.collegamento, i.motivo),
  revoca_abbinamento: (ctx, i) => reverseManualMatch(ctx, i.collegamento, i.motivo),
  crea_conto: (ctx, i) => createAccount(ctx, { holderRole: "owner", ...i } as never),
  classifica: (ctx, i) =>
    setClassification(ctx, i.id, i.tipo, i.categoria ?? null, i.nota ?? null).then(() => ({ id: i.id })),
  // Da comprare (brief 06/11): nuova voce della lista e cambio di stato (purchased richiede la transazione reale).
  aggiungi_acquisto: (ctx, i) => createPlannedPurchase(ctx, i as never),
  segna_acquisto: (ctx, i) =>
    setPurchaseStatus(ctx, i.id, i.stato, i.transactionId ?? null).then(() => ({ id: i.id, stato: i.stato })),
};

/** Operazioni che non scrivono: nessun backup, eseguibili su un'istantanea in sola lettura. */
export const READ_ONLY_OPS: ReadonlySet<string> = new Set(["consulta", "storico_manuale", "stato_budget"]);

export function out(doc: unknown, code = 0): number {
  process.stdout.write(`${JSON.stringify(doc)}\n`);
  return code;
}

/** Busta d'errore dei servizi (uscita 1). */
export function serviceError(err: unknown): number {
  const codice =
    err instanceof ValidationError ? "non_valido" : err instanceof NotFoundError ? "non_trovato" : "errore";
  return out(
    { ok: false, errore: "errore", codice, motivo: err instanceof Error ? err.message : String(err) },
    1,
  );
}
