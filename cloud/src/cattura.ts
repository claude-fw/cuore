/**
 * Cattura Apple Pay (C3, SOLO dati sintetici, locale): schema chiuso per `POST /cattura` dal Comando Rapido
 * «Transazione» (). La cattura e' un movimento PROVVISORIO in una tabella
 * propria: non tocca `transactions` ne' i riepiloghi. Dopo la sincronizzazione bancaria il rilevatore propone un
 * abbinamento (stesso importo, +-3 giorni, esercente simile) come oggi propone i trasferimenti: una proposta, mai
 * una conferma automatica.
 *
 * Doppia barriera (§4.3): in produzione il token di servizio di Cloudflare Access protegge la rotta prima che la
 * richiesta arrivi qui. Questo modulo verifica SOLO il secondo strato, applicativo: `Authorization: Bearer`,
 * confrontato con l'impronta in `CATTURA_TOKEN_SHA256` a tempo costante (`checkToken`, chiamata da app.ts).
 *
 * Limite di frequenza: contatore ESATTO nel Durable Object (non approssimato), 10 al minuto e 100 al giorno
 * (ora di Roma); ogni tentativo autenticato si registra in `cloud_cattura_tentativi` prima del controllo. Tre
 * superamenti consecutivi sospendono il token: `cloud_cattura_stato` resta sospeso finche' il titolare non lo
 * riattiva con l'operazione `riattiva_cattura` (via `/api/finance/esegui`, dietro Cloudflare Access).
 */
import { type Ctx, NotFoundError, ValidationError } from "../../finance/src/context.ts";
import { stmt } from "../../finance/src/db/types.ts";
import { commitGuarded, readWriteSeq } from "../../finance/src/db/write.ts";
import { canonicalJson, type Json, sha256Hex } from "../../finance/src/domain/hash.ts";
import { isSupportedCurrency, MoneyError, parseAmountToMinor } from "../../finance/src/domain/money.ts";
import { normalizeText } from "../../finance/src/domain/text.ts";
import { auditStatement, reviewStatement } from "../../finance/src/services/audit.ts";
import { EXCLUDED_IDS_SQL } from "../../finance/src/services/effective.ts";

export const CATTURA_MAX_BODY = 2 * 1024;
export const CATTURA_LIMIT_MINUTO = 10;
export const CATTURA_LIMIT_GIORNO = 100;
const SOSPENSIONE_DOPO = 3;
const MATCH_WINDOW_DAYS = 3;
const CATTURA_ID = /^[A-Za-z0-9_.:-]{8,128}$/;

export const CATTURA_TABLES = [
  `CREATE TABLE IF NOT EXISTS cloud_catture (
    id TEXT PRIMARY KEY, impronta TEXT NOT NULL, creata TEXT NOT NULL, istante TEXT NOT NULL,
    importo_minor INTEGER NOT NULL, valuta TEXT NOT NULL, esercente TEXT NOT NULL, carta TEXT NOT NULL,
    stato TEXT NOT NULL DEFAULT 'provvisoria', txn_id TEXT, decisa TEXT
  ) STRICT`,
  "CREATE TABLE IF NOT EXISTS cloud_cattura_tentativi (id INTEGER PRIMARY KEY AUTOINCREMENT, ms INTEGER NOT NULL, giorno TEXT NOT NULL) STRICT",
  "CREATE TABLE IF NOT EXISTS cloud_cattura_stato (chiave TEXT PRIMARY KEY, valore TEXT NOT NULL) STRICT",
  `CREATE TABLE IF NOT EXISTS cloud_cattura_rifiuti (
    cattura_id TEXT NOT NULL, txn_id TEXT NOT NULL, motivo TEXT NOT NULL, creata TEXT NOT NULL,
    PRIMARY KEY (cattura_id, txn_id)
  ) STRICT`,
];

/** Confronto a tempo costante di due impronte SHA-256 (esadecimale, lunghezza fissa): mai un confronto diretto. */
function timingSafeEqualHex(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** Secondo strato della doppia barriera (§4.3): token applicativo contro l'impronta configurata. */
export async function checkToken(
  header: string | null,
  expectedSha256: string | undefined,
): Promise<boolean> {
  if (expectedSha256 === undefined) return false;
  const m = /^Bearer (.+)$/.exec(header ?? "");
  if (m === null) return false;
  const given = await sha256Hex(m[1] as string);
  return timingSafeEqualHex(given, expectedSha256.toLowerCase());
}

const romeDay = (ms: number) => new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Rome" }).format(ms);

async function stato(ctx: Ctx, chiave: string): Promise<string | null> {
  return (
    (
      await ctx.db.first<{ valore: string }>("SELECT valore FROM cloud_cattura_stato WHERE chiave = ?", [
        chiave,
      ])
    )?.valore ?? null
  );
}

async function setStato(ctx: Ctx, chiave: string, valore: string): Promise<void> {
  await ctx.db.batch([
    stmt(
      "INSERT INTO cloud_cattura_stato (chiave, valore) VALUES (?, ?) ON CONFLICT (chiave) DO UPDATE SET valore = excluded.valore",
      chiave,
      valore,
    ),
  ]);
}

export interface CatturaResult {
  status: number;
  body: Record<string, unknown>;
}

/** Limite di frequenza ESATTO (conta le righe, non le approssima) + sospensione dopo superamenti consecutivi. */
async function checkRateLimit(ctx: Ctx): Promise<CatturaResult | null> {
  if ((await stato(ctx, "sospeso")) === "1")
    return { status: 403, body: { ok: false, errore: "token_sospeso" } };
  const now = Date.now();
  const giorno = romeDay(now);
  const alMinuto =
    (
      await ctx.db.first<{ n: number }>("SELECT COUNT(*) AS n FROM cloud_cattura_tentativi WHERE ms > ?", [
        now - 60_000,
      ])
    )?.n ?? 0;
  const alGiorno =
    (
      await ctx.db.first<{ n: number }>(
        "SELECT COUNT(*) AS n FROM cloud_cattura_tentativi WHERE giorno = ?",
        [giorno],
      )
    )?.n ?? 0;
  await ctx.db.batch([stmt("INSERT INTO cloud_cattura_tentativi (ms, giorno) VALUES (?, ?)", now, giorno)]);
  if (alMinuto >= CATTURA_LIMIT_MINUTO || alGiorno >= CATTURA_LIMIT_GIORNO) {
    const consecutivi = Number((await stato(ctx, "superamenti")) ?? "0") + 1;
    await setStato(ctx, "superamenti", String(consecutivi));
    if (consecutivi >= SOSPENSIONE_DOPO) await setStato(ctx, "sospeso", "1");
    return { status: 429, body: { ok: false, errore: "troppe_richieste" } };
  }
  await setStato(ctx, "superamenti", "0");
  return null;
}

interface CatturaInput {
  id: string;
  importo: string;
  valuta: string;
  esercente: string;
  carta: string;
  istante: string;
}

/** Schema chiuso (§4.3): solo questi campi, nient'altro accettato. */
function parseInput(body: unknown): CatturaInput {
  if (typeof body !== "object" || body === null || Array.isArray(body))
    throw new ValidationError("richiesta non valida");
  const b = body as Record<string, unknown>;
  const extra = Object.keys(b).filter(
    (k) => !["id", "importo", "valuta", "esercente", "carta", "istante"].includes(k),
  );
  if (extra.length > 0) throw new ValidationError(`campi non ammessi: ${extra.join(", ")}`);
  if (typeof b.id !== "string" || !CATTURA_ID.test(b.id)) throw new ValidationError("id non valido");
  if (typeof b.valuta !== "string" || !isSupportedCurrency(b.valuta))
    throw new ValidationError(`valuta non supportata: ${String(b.valuta)}`);
  if (typeof b.esercente !== "string" || b.esercente.trim() === "" || b.esercente.length > 80)
    throw new ValidationError("esercente non valido (1-80 caratteri)");
  if (typeof b.carta !== "string" || b.carta.trim() === "" || b.carta.length > 40)
    throw new ValidationError("carta non valida (1-40 caratteri)");
  if (typeof b.istante !== "string" || !Number.isFinite(Date.parse(b.istante)))
    throw new ValidationError("istante non valido");
  if (typeof b.importo !== "string" || b.importo.trim() === "") {
    throw new ValidationError("importo_vuoto");
  }
  return {
    id: b.id,
    importo: b.importo,
    valuta: b.valuta,
    esercente: b.esercente.trim(),
    carta: b.carta.trim(),
    istante: b.istante,
  };
}

/** Registra una cattura: valida, applica il limite di frequenza, idempotente per `id`. */
export async function registraCattura(ctx: Ctx, body: unknown): Promise<CatturaResult> {
  const limited = await checkRateLimit(ctx);
  if (limited !== null) return limited;

  let input: CatturaInput;
  try {
    input = parseInput(body);
  } catch (err) {
    const motivo = err instanceof Error ? err.message : String(err);
    if (motivo === "importo_vuoto") {
      // "rifiutata e segnalata" (criteri C3): l'importo manca, ma l'id e' noto; si segnala per la revisione.
      const id =
        typeof (body as Record<string, unknown> | null)?.id === "string"
          ? ((body as Record<string, unknown>).id as string)
          : crypto.randomUUID();
      await commitGuarded(ctx.db, await readWriteSeq(ctx.db), [
        reviewStatement({
          id: ctx.newId(),
          kind: "cattura_rifiutata",
          entity: "cattura",
          entityId: id,
          detail: { motivo: "importo vuoto", corpo: body },
          createdAt: ctx.now(),
        }),
      ]);
      return { status: 422, body: { ok: false, errore: "importo_vuoto" } };
    }
    return { status: 422, body: { ok: false, errore: "non_valido", motivo } };
  }

  const impronta = await sha256Hex(canonicalJson(input as unknown as Json));
  const existing = await ctx.db.first<{ impronta: string }>(
    "SELECT impronta FROM cloud_catture WHERE id = ?",
    [input.id],
  );
  if (existing !== null) {
    if (existing.impronta !== impronta)
      return { status: 409, body: { ok: false, errore: "richiesta_riusata" } };
    return {
      status: 200,
      body: { ok: true, risultato: { id: input.id, stato: "provvisoria" }, ripetuta: true },
    };
  }

  let importoMinor: number;
  try {
    importoMinor = parseAmountToMinor(input.importo, input.valuta);
  } catch (err) {
    const motivo = err instanceof MoneyError ? err.message : String(err);
    return { status: 422, body: { ok: false, errore: "non_valido", motivo } };
  }
  if (importoMinor <= 0)
    return { status: 422, body: { ok: false, errore: "non_valido", motivo: "importo non positivo" } };

  await ctx.db.batch([
    stmt(
      `INSERT INTO cloud_catture (id, impronta, creata, istante, importo_minor, valuta, esercente, carta, stato)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'provvisoria')`,
      input.id,
      impronta,
      ctx.now(),
      input.istante,
      importoMinor,
      input.valuta,
      input.esercente,
      input.carta,
    ),
  ]);
  return { status: 200, body: { ok: true, risultato: { id: input.id, stato: "provvisoria" } } };
}

interface BankTxn {
  id: string;
  account_id: string;
  currency: string;
  amount_minor: number;
  transaction_date: string;
  description: string;
}

const daysApart = (a: string, b: string) => Math.abs((Date.parse(a) - Date.parse(b)) / 86_400_000);

const similar = (esercente: string, descrizione: string): boolean => {
  const a = normalizeText(esercente);
  const b = normalizeText(descrizione);
  return a !== "" && (b.includes(a) || a.includes(b));
};

export interface DetectCatturaCounts {
  aperte: number;
  proposte: number;
  ambigue: number;
}

/** Dopo la sincronizzazione: propone l'abbinamento banca <-> cattura (mai una conferma automatica). */
export async function detectCatturaMatches(ctx: Ctx): Promise<DetectCatturaCounts> {
  const aperte = await ctx.db.all<{
    id: string;
    importo_minor: number;
    valuta: string;
    esercente: string;
    istante: string;
  }>("SELECT id, importo_minor, valuta, esercente, istante FROM cloud_catture WHERE stato = 'provvisoria'");
  if (aperte.length === 0) return { aperte: 0, proposte: 0, ambigue: 0 };

  const banche = await ctx.db.all<BankTxn>(
    `SELECT id, account_id, currency, amount_minor, transaction_date, description FROM transactions
     WHERE origin = 'import' AND id NOT IN (${EXCLUDED_IDS_SQL})`,
  );
  const rifiutate = new Set(
    (
      await ctx.db.all<{ cattura_id: string; txn_id: string }>(
        "SELECT cattura_id, txn_id FROM cloud_cattura_rifiuti",
      )
    ).map((r) => `${r.cattura_id}|${r.txn_id}`),
  );
  const giaProposta = new Set(
    (await ctx.db.all<{ txn_id: string }>("SELECT txn_id FROM cloud_catture WHERE txn_id IS NOT NULL")).map(
      (r) => r.txn_id,
    ),
  );

  let proposte = 0;
  let ambigue = 0;
  const statements = [];
  for (const c of aperte) {
    const data = c.istante.slice(0, 10);
    const candidati = banche.filter(
      (b) =>
        b.currency === c.valuta &&
        b.amount_minor === -c.importo_minor &&
        daysApart(b.transaction_date, data) <= MATCH_WINDOW_DAYS &&
        similar(c.esercente, b.description) &&
        !rifiutate.has(`${c.id}|${b.id}`) &&
        !giaProposta.has(b.id),
    );
    if (candidati.length === 1) {
      proposte++;
      statements.push(
        stmt(
          "UPDATE cloud_catture SET stato = 'proposta', txn_id = ? WHERE id = ?",
          candidati[0]?.id as string,
          c.id,
        ),
      );
    } else if (candidati.length > 1) {
      ambigue++;
      statements.push(
        reviewStatement({
          id: crypto.randomUUID(),
          kind: "cattura_ambigua",
          entity: "cattura",
          entityId: c.id,
          detail: { candidati: candidati.map((b) => b.id) },
          createdAt: ctx.now(),
        }),
      );
    }
  }
  if (statements.length > 0) await commitGuarded(ctx.db, await readWriteSeq(ctx.db), statements);
  return { aperte: aperte.length, proposte, ambigue };
}

async function loadCattura(
  ctx: Ctx,
  id: string,
): Promise<{ id: string; stato: string; txn_id: string | null }> {
  const row = await ctx.db.first<{ id: string; stato: string; txn_id: string | null }>(
    "SELECT id, stato, txn_id FROM cloud_catture WHERE id = ?",
    [id],
  );
  if (row === null) throw new NotFoundError("cattura", id);
  return row;
}

export async function confermaCattura(ctx: Ctx, id: string): Promise<{ id: string; stato: string }> {
  const c = await loadCattura(ctx, id);
  if (c.stato !== "proposta" || c.txn_id === null)
    throw new ValidationError("nessuna proposta da confermare");
  await ctx.db.batch([
    stmt("UPDATE cloud_catture SET stato = 'abbinata', decisa = ? WHERE id = ?", ctx.now(), id),
  ]);
  return { id, stato: "abbinata" };
}

export async function rifiutaCattura(ctx: Ctx, id: string, motivo: string): Promise<{ id: string }> {
  if (typeof motivo !== "string" || motivo.trim() === "") throw new ValidationError("motivo obbligatorio");
  const c = await loadCattura(ctx, id);
  if (c.stato !== "proposta" || c.txn_id === null) throw new ValidationError("nessuna proposta da rifiutare");
  await ctx.db.batch([
    stmt("UPDATE cloud_catture SET stato = 'provvisoria', txn_id = NULL WHERE id = ?", id),
    stmt(
      "INSERT INTO cloud_cattura_rifiuti (cattura_id, txn_id, motivo, creata) VALUES (?, ?, ?, ?)",
      id,
      c.txn_id,
      motivo.trim(),
      ctx.now(),
    ),
  ]);
  return { id };
}

export async function scartaCattura(ctx: Ctx, id: string, motivo: string): Promise<{ id: string }> {
  if (typeof motivo !== "string" || motivo.trim() === "") throw new ValidationError("motivo obbligatorio");
  await loadCattura(ctx, id);
  await ctx.db.batch([
    stmt("UPDATE cloud_catture SET stato = 'scartata', decisa = ? WHERE id = ?", ctx.now(), id),
  ]);
  return { id };
}

export async function riattivaCattura(ctx: Ctx, motivo: string): Promise<{ sospeso: boolean }> {
  if (typeof motivo !== "string" || motivo.trim() === "") throw new ValidationError("motivo obbligatorio");
  await setStato(ctx, "sospeso", "0");
  await setStato(ctx, "superamenti", "0");
  await commitGuarded(ctx.db, await readWriteSeq(ctx.db), [
    auditStatement({
      at: ctx.now(),
      actor: "user",
      action: "riattiva_cattura",
      entity: "cattura_token",
      entityId: "cattura",
      after: { sospeso: false },
      reason: motivo.trim(),
    }),
  ]);
  return { sospeso: false };
}

export const CATTURA_READS: Record<string, (ctx: Ctx) => Promise<unknown>> = {
  catture: (ctx) =>
    ctx.db.all(
      `SELECT id, creata, istante, importo_minor, valuta, esercente, carta, stato, txn_id FROM cloud_catture
       ORDER BY creata DESC, id LIMIT 50`,
    ),
  stato_cattura: async (ctx) => ({
    sospeso: (await stato(ctx, "sospeso")) === "1",
    al_minuto:
      (
        await ctx.db.first<{ n: number }>("SELECT COUNT(*) AS n FROM cloud_cattura_tentativi WHERE ms > ?", [
          Date.now() - 60_000,
        ])
      )?.n ?? 0,
    al_giorno:
      (
        await ctx.db.first<{ n: number }>(
          "SELECT COUNT(*) AS n FROM cloud_cattura_tentativi WHERE giorno = ?",
          [romeDay(Date.now())],
        )
      )?.n ?? 0,
  }),
};

// biome-ignore lint/suspicious/noExplicitAny: ingresso JSON validato qui
export const CATTURA_WRITES: Record<string, (ctx: Ctx, i: Record<string, any>) => Promise<unknown>> = {
  conferma_cattura: (ctx, i) => confermaCattura(ctx, String(i.id ?? "")),
  rifiuta_cattura: (ctx, i) => rifiutaCattura(ctx, String(i.id ?? ""), i.motivo),
  scarta_cattura: (ctx, i) => scartaCattura(ctx, String(i.id ?? ""), i.motivo),
  riattiva_cattura: (ctx, i) => riattivaCattura(ctx, i.motivo),
};
