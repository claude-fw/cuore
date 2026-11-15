/**
 * M3 — migrazione una tantum dei dati Finance dal Mac al Durable Object (lato cloud). SOLO PROVATA SU DATI SINTETICI.
 *
 * Formato: la stessa esportazione LOGICA del backup L1 (`backup.ts`: tabelle del motore portabile, righe in ordine di
 * `rowid`, `sqlite_sequence` a parte, impronta logica `logicalChecksumPortable`), senza la cifratura per destinatari:
 * qui il destinatario e' il Durable Object stesso, che deve leggere i dati in chiaro per usarli; la riservatezza in
 * transito la danno TLS e il token di servizio di Access (decisione C-03: i dati Finance possono stare nel cloud).
 * Le API di scrittura C2 non bastano: ricreerebbero i movimenti con id, impronte, audit e istanti nuovi, quindi
 * un'impronta diversa da quella del Mac e nessun confronto possibile.
 *
 * Protocollo `cuore.finance.migrazione/1`, tre fasi idempotenti (stesso `id`):
 *   1. `inizio`: manifesto (tabelle, colonne, conteggi, `sqlite_sequence`, impronta logica, numero di blocchi). Rifiutato
 *      se il Durable Object non e' in attesa dei dati del Mac (`CUORE_FINANCE_ORIGINE` = "mac"), se ha gia' dati del
 *      motore o una migrazione completata, se tabelle o colonne non coincidono con lo schema del motore nel cloud.
 *   2. `blocco` (1..N): righe di una tabella, parcheggiate in `cloud_migrazione_blocchi` (nessuna tabella del motore
 *      toccata); lo stesso blocco ripetuto = `ripetuto`, un blocco diverso con lo stesso indice = 409.
 *   3. `fine`: impronta logica delle righe ricevute = quella del manifesto, poi UNA transazione: svuota le righe create
 *      dalle migrazioni, inserisce tutto con le chiavi esterne differite, ripristina `sqlite_sequence`, rilegge ogni
 *      tabella e la confronta riga per riga con quanto ricevuto, `foreign_key_check` vuoto; qualunque differenza =
 *      eccezione = nulla scritto (rollback). Dopo: ricalcola l'impronta logica e la restituisce.
 * Le risposte contengono solo conteggi, stati e impronte: mai righe, importi o descrizioni.
 */

import type { Db } from "../../finance/src/db/types.ts";
import { sha256Hex } from "../../finance/src/domain/hash.ts";
import {
  logicalChecksumFromTables,
  logicalChecksumPortable,
  PROJECT_TABLES_SQL,
  type TableExport,
} from "./backup.ts";
import type { DurableSqlStorage } from "./durable-db.ts";

export const MIGRAZIONE_SCHEMA = "cuore.finance.migrazione/1";
export const MIGRAZIONE_PATH = "/api/finance/migrazione";
export const MIGRAZIONE_MAX_BODY = 1024 * 1024;

/** `CUORE_FINANCE_ORIGINE` = "mac": il Durable Object non carica gli esempi e aspetta i dati del Mac (M3). */
export const origineMac = (v: string | undefined) => v?.trim().toLowerCase() === "mac";

export const MIGRAZIONE_TABLES = [
  `CREATE TABLE IF NOT EXISTS cloud_migrazione (
    id TEXT PRIMARY KEY,
    stato TEXT NOT NULL CHECK (stato IN ('aperta', 'completata', 'fallita')),
    manifesto TEXT NOT NULL,
    creata TEXT NOT NULL,
    conclusa TEXT,
    esito TEXT
  )`,
  `CREATE TABLE IF NOT EXISTS cloud_migrazione_blocchi (
    migrazione TEXT NOT NULL,
    indice INTEGER NOT NULL,
    tabella TEXT NOT NULL,
    impronta TEXT NOT NULL,
    righe TEXT NOT NULL,
    PRIMARY KEY (migrazione, indice)
  )`,
];

export type Valore = string | number | null;

export interface ManifestoMigrazione {
  readonly schema: typeof MIGRAZIONE_SCHEMA;
  readonly fase: "inizio";
  readonly id: string;
  readonly versione_schema: number;
  readonly tabelle: readonly { table: string; columns: readonly string[]; righe: number }[];
  readonly sqlite_sequence: readonly [string, number][];
  readonly impronta_logica: string;
  readonly blocchi: number;
}

export interface BloccoMigrazione {
  readonly schema: typeof MIGRAZIONE_SCHEMA;
  readonly fase: "blocco";
  readonly id: string;
  readonly indice: number;
  readonly tabella: string;
  readonly colonne: readonly string[];
  readonly righe: readonly (readonly Valore[])[];
}

export interface FineMigrazione {
  readonly schema: typeof MIGRAZIONE_SCHEMA;
  readonly fase: "fine";
  readonly id: string;
}

export interface Risposta {
  readonly status: number;
  readonly body: Record<string, unknown>;
}

const ID = /^[A-Za-z0-9-]{8,64}$/;
const NOME = /^[a-z_][a-z0-9_]{0,62}$/;
const IMPRONTA = /^[0-9a-f]{64}$/;
const MAX_BLOCCHI = 100_000;

const no = (status: number, errore: string, extra: Record<string, unknown> = {}): Risposta => ({
  status,
  body: { ok: false, errore, ...extra },
});
const isInt = (v: unknown, min: number, max: number): v is number =>
  typeof v === "number" && Number.isSafeInteger(v) && v >= min && v <= max;
const isNomi = (v: unknown): v is string[] =>
  Array.isArray(v) && v.every((c) => typeof c === "string" && NOME.test(c));
const isValore = (v: unknown): v is Valore =>
  v === null || typeof v === "string" || (typeof v === "number" && Number.isFinite(v));

function rows(sql: DurableSqlStorage, query: string, ...params: Valore[]): Record<string, unknown>[] {
  return sql.sql.exec(query, ...params).toArray();
}

/** Tabelle del motore nel Durable Object con le colonne vere (`PRAGMA table_info`, non dedotte dalle righe). */
function schemaMotore(sql: DurableSqlStorage): Map<string, string[]> {
  const out = new Map<string, string[]>();
  for (const { name } of rows(sql, PROJECT_TABLES_SQL) as { name: string }[])
    out.set(
      name,
      (rows(sql, `PRAGMA table_info("${name}")`) as { name: string }[]).map((c) => c.name),
    );
  return out;
}

/** Nessun dato del motore: solo le righe che `migrate()` crea (`meta.write_seq` = 0 e la fonte `manual`). */
function motoreVuoto(sql: DurableSqlStorage, tabelle: Iterable<string>): boolean {
  for (const t of tabelle) {
    const n = Number(rows(sql, `SELECT COUNT(*) AS n FROM "${t}"`)[0]?.n ?? 0);
    if (t === "meta") {
      const m = rows(sql, "SELECT key, value FROM meta");
      if (!(m.length === 1 && m[0]?.key === "write_seq" && m[0]?.value === "0")) return false;
    } else if (t === "sources") {
      if (n > 1 || (n === 1 && rows(sql, "SELECT id FROM sources")[0]?.id !== "manual")) return false;
    } else if (n > 0) return false;
  }
  return true;
}

interface Riga {
  id: string;
  stato: string;
  manifesto: string;
}

function migrazioni(sql: DurableSqlStorage): Riga[] {
  return rows(sql, "SELECT id, stato, manifesto FROM cloud_migrazione") as unknown as Riga[];
}

/** Il Durable Object ha gia' ricevuto i dati del Mac (le altre rotte del motore si aprono solo dopo). */
export function migrazioneCompletata(sql: DurableSqlStorage): boolean {
  return rows(sql, "SELECT 1 AS x FROM cloud_migrazione WHERE stato = 'completata'").length > 0;
}

function controllaManifesto(m: Record<string, unknown>, versioneMotore: number): string | null {
  if (!isInt(m.versione_schema, 1, versioneMotore)) return "versione_schema_non_ammessa";
  if (!Array.isArray(m.tabelle) || m.tabelle.length === 0 || m.tabelle.length > 200)
    return "tabelle_non_valide";
  for (const t of m.tabelle as Record<string, unknown>[]) {
    if (typeof t !== "object" || t === null) return "tabelle_non_valide";
    if (
      typeof t.table !== "string" ||
      !NOME.test(t.table) ||
      !isNomi(t.columns) ||
      !isInt(t.righe, 0, 10_000_000)
    )
      return "tabelle_non_valide";
  }
  if (
    !Array.isArray(m.sqlite_sequence) ||
    !m.sqlite_sequence.every(
      (p) =>
        Array.isArray(p) &&
        p.length === 2 &&
        typeof p[0] === "string" &&
        NOME.test(p[0]) &&
        isInt(p[1], 0, Number.MAX_SAFE_INTEGER),
    )
  )
    return "sqlite_sequence_non_valida";
  if (typeof m.impronta_logica !== "string" || !IMPRONTA.test(m.impronta_logica))
    return "impronta_non_valida";
  if (!isInt(m.blocchi, 0, MAX_BLOCCHI)) return "blocchi_non_validi";
  return null;
}

/** Tabelle e colonne del manifesto = schema del motore nel cloud (stesso insieme, stesse colonne nello stesso ordine). */
function confrontaSchema(m: ManifestoMigrazione, schema: Map<string, string[]>): string | null {
  const nomi = m.tabelle.map((t) => t.table);
  if (new Set(nomi).size !== nomi.length) return "tabella_ripetuta";
  const attese = [...schema.keys()].sort();
  if (JSON.stringify([...nomi].sort()) !== JSON.stringify(attese)) return "tabelle_diverse_dal_motore";
  for (const t of m.tabelle) {
    const vere = schema.get(t.table) ?? [];
    if (t.righe > 0 && JSON.stringify(t.columns) !== JSON.stringify(vere))
      return `colonne_diverse:${t.table}`;
    if (t.righe === 0 && t.columns.length > 0 && JSON.stringify(t.columns) !== JSON.stringify(vere))
      return `colonne_diverse:${t.table}`;
  }
  return null;
}

function inizio(
  sql: DurableSqlStorage,
  m: ManifestoMigrazione,
  versioneMotore: number,
  now: string,
): Risposta {
  const problema = controllaManifesto(m as unknown as Record<string, unknown>, versioneMotore);
  if (problema !== null) return no(422, problema);
  const esistenti = migrazioni(sql);
  if (esistenti.some((r) => r.stato === "completata")) return no(409, "migrazione_gia_completata");
  const testo = JSON.stringify(m);
  const stessa = esistenti.find((r) => r.id === m.id && r.stato === "aperta");
  if (stessa !== undefined)
    return stessa.manifesto === testo
      ? { status: 200, body: { ok: true, id: m.id, stato: "aperta", ripetuto: true, attesi: m.blocchi } }
      : no(409, "manifesto_diverso_per_lo_stesso_id");
  const schema = schemaMotore(sql);
  const diverso = confrontaSchema(m, schema);
  if (diverso !== null) return no(422, diverso);
  if (!motoreVuoto(sql, schema.keys())) return no(409, "durable_object_non_vuoto");
  // una prova precedente non conclusa (rete caduta, blocco sbagliato) si scarta: non aveva scritto nel motore
  sql.transactionSync(() => {
    sql.sql.exec("DELETE FROM cloud_migrazione_blocchi");
    sql.sql.exec("DELETE FROM cloud_migrazione WHERE stato <> 'completata'");
    sql.sql.exec(
      "INSERT INTO cloud_migrazione (id, stato, manifesto, creata) VALUES (?, 'aperta', ?, ?)",
      m.id,
      testo,
      now,
    );
  });
  return { status: 200, body: { ok: true, id: m.id, stato: "aperta", attesi: m.blocchi } };
}

function aperta(sql: DurableSqlStorage, id: string): ManifestoMigrazione | Risposta {
  const r = migrazioni(sql).find((x) => x.id === id);
  if (r === undefined) return no(404, "migrazione_sconosciuta");
  if (r.stato === "completata") return no(409, "migrazione_gia_completata");
  if (r.stato !== "aperta") return no(409, "migrazione_non_aperta");
  return JSON.parse(r.manifesto) as ManifestoMigrazione;
}

async function blocco(sql: DurableSqlStorage, b: BloccoMigrazione): Promise<Risposta> {
  const m = aperta(sql, b.id);
  if ("status" in m) return m;
  if (!isInt(b.indice, 1, m.blocchi)) return no(422, "indice_non_valido");
  const t = m.tabelle.find((x) => x.table === b.tabella);
  if (t === undefined) return no(422, "tabella_non_nel_manifesto");
  if (!isNomi(b.colonne) || JSON.stringify(b.colonne) !== JSON.stringify(t.columns))
    return no(422, "colonne_diverse_dal_manifesto");
  if (
    !Array.isArray(b.righe) ||
    b.righe.length === 0 ||
    !b.righe.every((r) => Array.isArray(r) && r.length === b.colonne.length && r.every(isValore))
  )
    return no(422, "righe_non_valide");
  const testo = JSON.stringify(b.righe);
  const impronta = await sha256Hex(`${b.tabella}\n${testo}`);
  const gia = rows(
    sql,
    "SELECT impronta FROM cloud_migrazione_blocchi WHERE migrazione = ? AND indice = ?",
    b.id,
    b.indice,
  )[0];
  if (gia !== undefined)
    return gia.impronta === impronta
      ? { status: 200, body: { ok: true, indice: b.indice, ripetuto: true } }
      : no(409, "blocco_diverso_per_lo_stesso_indice");
  sql.sql.exec(
    "INSERT INTO cloud_migrazione_blocchi (migrazione, indice, tabella, impronta, righe) VALUES (?, ?, ?, ?, ?)",
    b.id,
    b.indice,
    b.tabella,
    impronta,
    testo,
  );
  return { status: 200, body: { ok: true, indice: b.indice } };
}

class Rifiuto extends Error {}

function fallita(sql: DurableSqlStorage, id: string, esito: string, now: string): void {
  sql.sql.exec(
    "UPDATE cloud_migrazione SET stato = 'fallita', conclusa = ?, esito = ? WHERE id = ? AND stato = 'aperta'",
    now,
    esito,
    id,
  );
}

async function fine(sql: DurableSqlStorage, db: Db, f: FineMigrazione, now: string): Promise<Risposta> {
  const m = aperta(sql, f.id);
  if ("status" in m) return m;
  const ricevuti = rows(
    sql,
    "SELECT indice, tabella, righe FROM cloud_migrazione_blocchi WHERE migrazione = ? ORDER BY indice",
    f.id,
  ) as { indice: number; tabella: string; righe: string }[];
  if (ricevuti.length !== m.blocchi)
    return no(409, "blocchi_mancanti", { ricevuti: ricevuti.length, attesi: m.blocchi });
  const perTabella = new Map<string, Valore[][]>(m.tabelle.map((t) => [t.table, []]));
  for (const r of ricevuti) perTabella.get(r.tabella)?.push(...(JSON.parse(r.righe) as Valore[][]));
  const tabelle: TableExport[] = m.tabelle.map((t) => ({
    table: t.table,
    columns: t.columns,
    rows: perTabella.get(t.table) ?? [],
  }));
  for (const t of m.tabelle)
    if ((perTabella.get(t.table)?.length ?? 0) !== t.righe) {
      fallita(sql, f.id, `conteggio_diverso:${t.table}`, now);
      return no(422, "conteggio_diverso", { tabella: t.table });
    }
  if ((await logicalChecksumFromTables(tabelle)) !== m.impronta_logica) {
    fallita(sql, f.id, "impronta_diversa", now);
    return no(422, "impronta_diversa");
  }

  try {
    sql.transactionSync(() => {
      const schema = schemaMotore(sql);
      if (!motoreVuoto(sql, schema.keys())) throw new Rifiuto("durable_object_non_vuoto");
      sql.sql.exec("PRAGMA defer_foreign_keys = ON");
      for (const t of tabelle) {
        sql.sql.exec(`DELETE FROM "${t.table}"`);
        const insert = `INSERT INTO "${t.table}" (${t.columns.map((c) => `"${c}"`).join(", ")}) VALUES (${t.columns.map(() => "?").join(", ")})`;
        for (const r of t.rows) sql.sql.exec(insert, ...(r as Valore[]));
      }
      for (const [name, seq] of m.sqlite_sequence) {
        sql.sql.exec("UPDATE sqlite_sequence SET seq = ? WHERE name = ?", seq, name);
        if (rows(sql, "SELECT 1 AS x FROM sqlite_sequence WHERE name = ?", name).length === 0)
          sql.sql.exec("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", name, seq);
      }
      const seq = rows(sql, "SELECT name, seq FROM sqlite_sequence ORDER BY name")
        .filter((r) => !String(r.name).startsWith("cloud_") && !String(r.name).startsWith("_cf_"))
        .map((r) => [r.name, r.seq]);
      const attesa = [...m.sqlite_sequence].sort((a, b) => (a[0] < b[0] ? -1 : 1));
      if (JSON.stringify(seq) !== JSON.stringify(attesa)) throw new Rifiuto("sqlite_sequence_diversa");
      for (const t of tabelle) {
        const lette = rows(sql, `SELECT * FROM "${t.table}" ORDER BY rowid`).map((r) =>
          t.columns.map((c) => r[c] ?? null),
        );
        if (JSON.stringify(lette) !== JSON.stringify(t.rows)) throw new Rifiuto(`righe_diverse:${t.table}`);
      }
      if (rows(sql, "PRAGMA foreign_key_check").length > 0) throw new Rifiuto("chiavi_esterne_non_valide");
      if (rows(sql, "SELECT 1 AS x FROM sqlite_master WHERE name = 'transactions_search'").length > 0)
        sql.sql.exec("INSERT INTO transactions_search (transactions_search) VALUES ('rebuild')");
      sql.sql.exec(
        "UPDATE cloud_migrazione SET stato = 'completata', conclusa = ?, esito = 'ok' WHERE id = ?",
        now,
        f.id,
      );
      sql.sql.exec("DELETE FROM cloud_migrazione_blocchi WHERE migrazione = ?", f.id);
    });
  } catch (err) {
    // solo il nome del controllo fallito (o il messaggio generico di SQLite): mai valori delle righe
    const esito = err instanceof Rifiuto ? err.message : "errore_sqlite";
    fallita(sql, f.id, esito, now);
    return no(422, "migrazione_rifiutata", { motivo: esito });
  }
  const impronta = await logicalChecksumPortable(db);
  return {
    status: 200,
    body: {
      ok: true,
      id: f.id,
      stato: "completata",
      tabelle: m.tabelle.map((t) => ({ table: t.table, righe: t.righe })),
      impronta_logica: impronta,
      impronta_coerente: impronta === m.impronta_logica,
    },
  };
}

/** `POST /migrazione` nel Durable Object: smista le tre fasi. `attesa` = il DO aspetta i dati del Mac. */
export async function gestisciMigrazione(
  sql: DurableSqlStorage,
  db: Db,
  body: unknown,
  opts: { attesa: boolean; versioneMotore: number; now: () => string },
): Promise<Risposta> {
  if (!opts.attesa) return no(403, "migrazione_non_prevista");
  if (typeof body !== "object" || body === null) return no(400, "richiesta_non_valida");
  const b = body as Record<string, unknown>;
  if (b.schema !== MIGRAZIONE_SCHEMA) return no(422, "schema_non_riconosciuto");
  if (typeof b.id !== "string" || !ID.test(b.id)) return no(422, "id_non_valido");
  if (b.fase === "inizio")
    return inizio(sql, b as unknown as ManifestoMigrazione, opts.versioneMotore, opts.now());
  if (b.fase === "blocco") return blocco(sql, b as unknown as BloccoMigrazione);
  if (b.fase === "fine") return fine(sql, db, b as unknown as FineMigrazione, opts.now());
  return no(422, "fase_non_valida");
}
