/**
 * API di scrittura Finance del Durable Object (C2: vetrina sintetica oppure, con `CUORE_FINANCE_ORIGINE` = "mac", i dati
 * veri migrati). Stesse operazioni e stessi nomi del ponte (`finance/scripts/operazioni.ts`): i moduli D2 dell'app usano
 * un solo contratto, in locale e nel cloud.
 *
 * Richiesta: {"op", "input", "requestId"?, "versione"?}. Risposta: {"ok", "risultato" | "errore", "versione", ...}.
 * - Scritture: `requestId` obbligatorio. Stesso id e stessa richiesta = la risposta registrata (`ripetuta: true`), mai
 *   una seconda scrittura; stesso id con richiesta diversa = 409 `richiesta_riusata`. Si registra solo cio' che ha
 *   scritto davvero (`write_seq` cambiato): un «possibile doppione» o un rifiuto si possono ripetere con lo stesso id.
 * - `versione` facoltativa = `write_seq` visto dal client: se nel frattempo e' cambiato, 409 `conflitto` e nessuna
 *   scrittura. Ogni risposta riporta la versione corrente.
 * Esiti: 200, 400 (richiesta), 404 (non trovato), 409 (conflitto), 422 (ingresso rifiutato dal motore), 500.
 * Atomicita': il Durable Object conferma insieme le scritture SQL di un evento senza I/O esterno (nessun fetch qui),
 * quindi scrittura del motore e registrazione della richiesta avvengono entrambe o nessuna (da riverificare nel remoto).
 */
import { type Ctx, NotFoundError, ValidationError } from "../../finance/src/context.ts";
import { ConflictError, readWriteSeq } from "../../finance/src/db/write.ts";
import { importFile } from "../../finance/src/import/importer.ts";
import { runQuery, SYNTHETIC_QUERY_SCHEMA_VERSION } from "../../finance/src/query/operations.ts";
import { createAccount } from "../../finance/src/services/accounts.ts";
import { addManualBalance } from "../../finance/src/services/balances.ts";
import { budgetStatus, setBudget } from "../../finance/src/services/budgets.ts";
import {
  classifyTransactions,
  publishRuleSet,
  setClassification,
} from "../../finance/src/services/classification.ts";
import { decideLink } from "../../finance/src/services/links.ts";
import {
  addManualTransaction,
  correctManualTransaction,
  manualHistory,
  voidManualTransaction,
} from "../../finance/src/services/manual.ts";
import {
  confirmManualMatch,
  detectManualMatches,
  rejectManualMatch,
  reverseManualMatch,
} from "../../finance/src/services/manual-matching.ts";
import { createPlannedPurchase, setPurchaseStatus } from "../../finance/src/services/planning.ts";
import { applicaRegistro } from "./registro.ts";

// biome-ignore lint/suspicious/noExplicitAny: ingresso JSON validato dai servizi
type Input = Record<string, any>;
export type Op = (ctx: Ctx, i: Input, requestId: string) => Promise<unknown>;

const READS: Record<string, Op> = {
  consulta: (ctx, i) =>
    runQuery(ctx.db, i.operazione, i.input ?? {}, {
      today: i.oggi,
      expectedSchemaVersion: SYNTHETIC_QUERY_SCHEMA_VERSION,
    }),
  storico_manuale: (ctx, i) => manualHistory(ctx, i.id),
  stato_budget: (ctx, i) => budgetStatus(ctx, i.mese, i.valuta),
};

const WRITES: Record<string, Op> = {
  aggiungi_manuale: (ctx, i, requestId) => addManualTransaction(ctx, { ...i, requestId } as never),
  correggi_manuale: (ctx, i) => correctManualTransaction(ctx, i.id, i.modifiche ?? {}, i.motivo),
  annulla_manuale: (ctx, i) => voidManualTransaction(ctx, i.id, i.motivo),
  imposta_budget: (ctx, i) => setBudget(ctx, i as never),
  classifica: (ctx, i) =>
    setClassification(ctx, i.id, i.tipo, i.categoria ?? null, i.nota ?? null).then(() => ({ id: i.id })),
  crea_conto: (ctx, i) => createAccount(ctx, { holderRole: "owner", ...i } as never),
  // Saldo indicato a mano (contanti: cassaforte, portafoglio). Lo stesso saldo ripetuto non crea nulla.
  saldo_manuale: (ctx, i) => addManualBalance(ctx, i.conto, i.data, i.importo, i.nota ?? null),
  decidi_collegamento: (ctx, i) => decideLink(ctx, i.collegamento, i.decisione),
  rileva_abbinamenti: (ctx) => detectManualMatches(ctx),
  conferma_abbinamento: (ctx, i) => confirmManualMatch(ctx, i.collegamento),
  rifiuta_abbinamento: (ctx, i) => rejectManualMatch(ctx, i.collegamento, i.motivo),
  revoca_abbinamento: (ctx, i) => reverseManualMatch(ctx, i.collegamento, i.motivo),
  aggiungi_acquisto: (ctx, i) => createPlannedPurchase(ctx, i as never),
  // Regole di classificazione dal Mac: il file (dati personali: nomi dei negozi) arriva compresso (gzip, base64) per
  // stare nei 64 KiB delle scritture; si pubblica una nuova versione e si riclassifica tutto (le scelte a mano restano).
  pubblica_regole: async (ctx, i) => {
    const published = await publishRuleSet(ctx, await inflateJson(i.gzip));
    return { ...published, classificazione: await classifyTransactions(ctx) };
  },
  classifica_tutte: (ctx) => classifyTransactions(ctx),
  // Movimenti letti sul Mac dagli estratti (formato cloud-finance-csv, compresso come le regole). Un movimento gia'
  // presente si riconosce e si salta: ricaricare lo stesso file non crea doppioni.
  importa_movimenti: async (ctx, i) => {
    const r = await importFile(ctx, {
      accountId: i.conto,
      parserId: "cloud-finance-csv",
      fileName: i.nome ?? null,
      content: new TextEncoder().encode(await inflate(i.gzip)),
    });
    return { stato: r.status, conteggi: r.counts, problemi: r.issues.slice(0, 10) };
  },
  registro_crediti: async (ctx, i) => applicaRegistro(ctx, JSON.parse(await inflate(i.gzip))),
  segna_acquisto: (ctx, i) =>
    setPurchaseStatus(ctx, i.id, i.stato, i.transactionId ?? null).then(() => ({ id: i.id, stato: i.stato })),
};

/** Al massimo 1 MiB decompresso: un file di regole vero e' ~100 KiB. */
const MAX_INFLATED = 1024 * 1024;
async function inflateJson(b64: unknown): Promise<unknown> {
  const text = await inflate(b64);
  try {
    return JSON.parse(text);
  } catch {
    throw new ValidationError("gzip: il contenuto non e' JSON");
  }
}
async function inflate(b64: unknown): Promise<string> {
  if (typeof b64 !== "string" || b64.length === 0 || b64.length > 60_000)
    throw new ValidationError("gzip: file compresso in base64 (al massimo 60 000 caratteri)");
  let bytes: Uint8Array;
  try {
    bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  } catch {
    throw new ValidationError("gzip: base64 non valido");
  }
  const reader = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip")).getReader();
  const parts: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_INFLATED) throw new ValidationError("gzip: file troppo grande");
      parts.push(value);
    }
  } catch (e) {
    if (e instanceof ValidationError) throw e;
    throw new ValidationError("gzip: dati compressi non validi");
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(await new Blob(parts).arrayBuffer());
  } catch {
    throw new ValidationError("gzip: il contenuto non e' testo UTF-8");
  }
}

const REQUEST_ID = /^[A-Za-z0-9_.:-]{8,128}$/;

export const REQUESTS_TABLE = `CREATE TABLE IF NOT EXISTS cloud_richieste (
  request_id TEXT PRIMARY KEY, op TEXT NOT NULL, impronta TEXT NOT NULL, risposta TEXT NOT NULL, creata TEXT NOT NULL
) STRICT`;

async function sha256(s: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export interface ApiResult {
  status: number;
  body: Record<string, unknown>;
}

/** `extra`: operazioni aggiunte dal Durable Object (automazioni.ts), stesse regole. */
export async function execute(
  ctx: Ctx,
  req: unknown,
  extra: { reads?: Record<string, Op>; writes?: Record<string, Op> } = {},
): Promise<ApiResult> {
  const db = ctx.db;
  const versione = await readWriteSeq(db);
  const fail = (status: number, errore: string, extra: Record<string, unknown> = {}): ApiResult => ({
    status,
    body: { ok: false, errore, ...extra, versione },
  });
  if (typeof req !== "object" || req === null || Array.isArray(req)) return fail(400, "richiesta_non_valida");
  const { op, input = {}, requestId, versione: seen } = req as Record<string, unknown>;
  const read = { ...READS, ...extra.reads }[op as string];
  const write = { ...WRITES, ...extra.writes }[op as string];
  if (typeof op !== "string" || (read ?? write) === undefined) return fail(400, "operazione_sconosciuta");
  if (typeof input !== "object" || input === null || Array.isArray(input))
    return fail(400, "input_non_valido");
  if (seen !== undefined && !Number.isSafeInteger(seen)) return fail(400, "versione_non_valida");
  if (write !== undefined && (typeof requestId !== "string" || !REQUEST_ID.test(requestId)))
    return fail(400, "requestId_mancante");

  let impronta = "";
  if (write !== undefined) {
    impronta = await sha256(JSON.stringify({ op, input }));
    const prev = await db.first<{ impronta: string; risposta: string }>(
      "SELECT impronta, risposta FROM cloud_richieste WHERE request_id = ?",
      [requestId as string],
    );
    if (prev !== null)
      return prev.impronta === impronta
        ? { status: 200, body: { ...JSON.parse(prev.risposta), ripetuta: true, versione } }
        : fail(409, "richiesta_riusata");
    if (seen !== undefined && seen !== versione) return fail(409, "conflitto", { motivo: "dati cambiati" });
  }
  try {
    const risultato = await (read ?? write)?.(ctx, input as Input, requestId as string);
    const after = await readWriteSeq(db);
    const body = { ok: true, risultato };
    if (write !== undefined && after !== versione)
      await db.batch([
        {
          sql: "INSERT INTO cloud_richieste (request_id, op, impronta, risposta, creata) VALUES (?, ?, ?, ?, ?)",
          params: [requestId as string, op, impronta, JSON.stringify(body), ctx.now()],
        },
      ]);
    return { status: 200, body: { ...body, versione: after } };
  } catch (err) {
    const motivo = err instanceof Error ? err.message : String(err);
    if (err instanceof ValidationError) return fail(422, "non_valido", { motivo });
    if (err instanceof NotFoundError) return fail(404, "non_trovato", { motivo });
    if (err instanceof ConflictError) return fail(409, "conflitto", { motivo });
    return fail(500, "errore");
  }
}
