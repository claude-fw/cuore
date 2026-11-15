#!/usr/bin/env node
/**
 * M3 — migrazione una tantum dei dati Finance dal Mac al Durable Object (lato Mac). PROVATA SOLO SU DATABASE SINTETICI.
 *
 *   A VUOTO (predefinito, nessuna rete):
 *     node cloud/scripts/migra-finance.ts --database <finance.sqlite3>
 *   INVIO VERO (solo con autorizzazione del titolare, decisione M3):
 *     CF_ACCESS_CLIENT_ID=... CF_ACCESS_CLIENT_SECRET=... node cloud/scripts/migra-finance.ts --database <file> \
 *       --invia --url https://cuore.<sottodominio>.workers.dev --conferma-impronta <prime 12 cifre dell'impronta>
 *
 * Legge il database da un'ISTANTANEA IN MEMORIA (`readImmutable`: mai aperto in scrittura, nessun -wal/-shm creato,
 * rifiuta se c'e' una scrittura in corso), controlla che lo schema sia quello del motore, prepara l'esportazione logica
 * del backup L1 (stesse tabelle, stesso ordine, stessa impronta logica) divisa in blocchi piccoli, e:
 *   - a vuoto: stampa SOLO conteggi per tabella, numero di blocchi, byte e impronta logica;
 *   - con `--invia`: manda manifesto, blocchi e chiusura a `POST /api/finance/migrazione` (protocollo
 *     `cuore.finance.migrazione/1`, src/migrazione.ts). Servono `--url` in https, `--conferma-impronta` uguale alle
 *     prime 12 cifre dell'impronta mostrata dalla prova a vuoto (si conferma QUEL database) e il token di servizio di
 *     Access nelle variabili d'ambiente (mai sulla riga di comando, mai stampato).
 * Il cloud rifiuta se non aspetta i dati del Mac, se ha gia' dati, se schema o impronta non tornano: nulla di parziale.
 */
import { randomUUID } from "node:crypto";
import { parseArgs } from "node:util";
import { migrate } from "../../finance/src/db/migrate.ts";
import { PENDING_MIGRATIONS } from "../../finance/src/db/migrations/007_manual_and_budgets.ts";
import { MIGRATIONS } from "../../finance/src/db/migrations/index.ts";
import { NodeSqliteDb, readImmutable } from "../../finance/src/db/node-sqlite.ts";
import { compareSchema, readSchema } from "../../finance/src/db/schema-check.ts";
import type { Db } from "../../finance/src/db/types.ts";
import { logicalChecksumFromTables, readExportableTables, readSqliteSequence } from "../src/backup.ts";
import {
  type BloccoMigrazione,
  type FineMigrazione,
  type ManifestoMigrazione,
  MIGRAZIONE_MAX_BODY,
  MIGRAZIONE_PATH,
  MIGRAZIONE_SCHEMA,
  type Valore,
} from "../src/migrazione.ts";

const ALL_MIGRATIONS = [...MIGRATIONS, ...PENDING_MIGRATIONS];
/** Righe per blocco (come i blocchi del backup L1, ~0,33 ms di CPU per 200 righe) e tetto in byte per richiesta. */
export const RIGHE_PER_BLOCCO = 200;
export const MAX_BYTE_BLOCCO = 256 * 1024;

export interface PianoMigrazione {
  readonly manifesto: ManifestoMigrazione;
  readonly blocchi: readonly BloccoMigrazione[];
  readonly byte: number;
}

/** Versione dello schema del database (ultima migrazione applicata) e confronto con lo schema del motore a quella versione. */
async function controllaSchema(db: Db): Promise<number> {
  const row = await db.first<{ v: number | null }>("SELECT MAX(version) AS v FROM schema_migrations");
  const versione = row?.v ?? 0;
  if (!Number.isSafeInteger(versione) || versione < 1 || versione > ALL_MIGRATIONS.length)
    throw new Error(
      `versione dello schema non gestita: ${versione} (motore fino alla ${ALL_MIGRATIONS.length})`,
    );
  const ref = new NodeSqliteDb(":memory:");
  try {
    await migrate(ref, ALL_MIGRATIONS.slice(0, versione), () => new Date().toISOString());
    const diff = compareSchema(await readSchema(ref), await readSchema(db));
    if (diff.missing.length + diff.unexpected.length + diff.changed.length > 0)
      throw new Error(`schema del database diverso da quello del motore v${versione}: nessun invio`);
  } finally {
    ref.close();
  }
  return versione;
}

/** Prepara manifesto e blocchi dall'esportazione logica L1. Nessuna scrittura, nessuna rete. */
export async function preparaMigrazione(
  db: Db,
  opts: { righePerBlocco?: number; maxByte?: number; id?: string } = {},
): Promise<PianoMigrazione> {
  const righePerBlocco = opts.righePerBlocco ?? RIGHE_PER_BLOCCO;
  const maxByte = Math.min(opts.maxByte ?? MAX_BYTE_BLOCCO, MIGRAZIONE_MAX_BODY / 2);
  const versione = await controllaSchema(db);
  const tabelle = await readExportableTables(db);
  const id = opts.id ?? randomUUID();
  const blocchi: BloccoMigrazione[] = [];
  let byte = 0;
  for (const t of tabelle) {
    for (const r of t.rows)
      for (const v of r)
        if (!(v === null || typeof v === "string" || (typeof v === "number" && Number.isFinite(v))))
          throw new Error(`valore non trasferibile nella tabella ${t.table} (solo testo, numeri e null)`);
    let corrente: Valore[][] = [];
    let dim = 0;
    const chiudi = () => {
      if (corrente.length === 0) return;
      blocchi.push({
        schema: MIGRAZIONE_SCHEMA,
        fase: "blocco",
        id,
        indice: blocchi.length + 1,
        tabella: t.table,
        colonne: t.columns,
        righe: corrente,
      });
      byte += dim;
      corrente = [];
      dim = 0;
    };
    for (const r of t.rows as Valore[][]) {
      const n = JSON.stringify(r).length + 1;
      if (n > maxByte) throw new Error(`una riga della tabella ${t.table} supera ${maxByte} byte`);
      if (corrente.length >= righePerBlocco || dim + n > maxByte) chiudi();
      corrente.push(r);
      dim += n;
    }
    chiudi();
  }
  const manifesto: ManifestoMigrazione = {
    schema: MIGRAZIONE_SCHEMA,
    fase: "inizio",
    id,
    versione_schema: versione,
    tabelle: tabelle.map((t) => ({ table: t.table, columns: t.columns, righe: t.rows.length })),
    sqlite_sequence: await readSqliteSequence(db),
    impronta_logica: await logicalChecksumFromTables(tabelle),
    blocchi: blocchi.length,
  };
  return { manifesto, blocchi, byte };
}

/** Riepilogo in italiano: solo conteggi, versione, blocchi, byte e impronta (mai righe, importi o descrizioni). */
export function riepilogo(p: PianoMigrazione): string {
  const m = p.manifesto;
  const piene = m.tabelle.filter((t) => t.righe > 0);
  return [
    `Migrazione Finance verso il cloud — PROVA A VUOTO (nessuna rete, nessuna scrittura)`,
    `Versione dello schema: v${m.versione_schema} · tabelle: ${m.tabelle.length} (${piene.length} con righe)`,
    ...piene.map((t) => `  ${t.table}: ${t.righe}`),
    `Blocchi da inviare: ${m.blocchi} (+ inizio e fine) · circa ${Math.ceil(p.byte / 1024)} KiB di righe`,
    `Impronta logica: ${m.impronta_logica}`,
    `Per l'invio vero (solo con autorizzazione): --invia --url https://... --conferma-impronta ${m.impronta_logica.slice(0, 12)}`,
  ].join("\n");
}

export type Fetch = (url: string, init: RequestInit) => Promise<Response>;

export interface EsitoInvio {
  readonly ok: boolean;
  readonly fase: "inizio" | "blocco" | "fine";
  readonly status: number;
  readonly risposta: Record<string, unknown>;
}

/** Invia inizio, blocchi e fine in ordine; si ferma al primo rifiuto. `base` = origine del Worker (https in uso reale). */
export async function inviaMigrazione(
  piano: PianoMigrazione,
  opts: { base: string; fetch: Fetch; clientId: string; clientSecret: string },
): Promise<EsitoInvio> {
  const url = new URL(MIGRAZIONE_PATH, opts.base).toString();
  const manda = async (
    fase: EsitoInvio["fase"],
    body: ManifestoMigrazione | BloccoMigrazione | FineMigrazione,
  ) => {
    const r = await opts.fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "CF-Access-Client-Id": opts.clientId,
        "CF-Access-Client-Secret": opts.clientSecret,
      },
      body: JSON.stringify(body),
      redirect: "manual",
    });
    let risposta: Record<string, unknown> = {};
    try {
      risposta = (await r.json()) as Record<string, unknown>;
    } catch {
      risposta = { errore: "risposta_non_json" }; // es. la pagina di login di Access: token sbagliato
    }
    return { ok: r.status === 200 && risposta.ok === true, fase, status: r.status, risposta };
  };
  const primo = await manda("inizio", piano.manifesto);
  if (!primo.ok) return primo;
  for (const b of piano.blocchi) {
    const e = await manda("blocco", b);
    if (!e.ok) return e;
  }
  return manda("fine", { schema: MIGRAZIONE_SCHEMA, fase: "fine", id: piano.manifesto.id });
}

export interface Cli {
  readonly database: string;
  readonly invia: false | { base: string; conferma: string };
}

/** Argomenti: a vuoto per difetto; l'invio vero chiede `--invia`, un URL https senza credenziali e la conferma. */
export function parseCli(argv: readonly string[]): Cli {
  const { values } = parseArgs({
    args: [...argv],
    options: {
      database: { type: "string" },
      invia: { type: "boolean", default: false },
      url: { type: "string" },
      "conferma-impronta": { type: "string" },
    },
  });
  if (!values.database) throw new Error("argomento richiesto: --database <file SQLite di Finance>");
  if (!values.invia) {
    if (values.url !== undefined || values["conferma-impronta"] !== undefined)
      throw new Error("--url e --conferma-impronta valgono solo con --invia");
    return { database: values.database, invia: false };
  }
  let u: URL;
  try {
    u = new URL(values.url ?? "");
  } catch {
    throw new Error("--invia richiede --url https://<indirizzo del Worker>");
  }
  if (u.protocol !== "https:") throw new Error("solo indirizzi https");
  if (u.username !== "" || u.password !== "") throw new Error("niente credenziali nell'indirizzo");
  if ((u.pathname !== "/" && u.pathname !== "") || u.search !== "" || u.hash !== "")
    throw new Error("indicare solo l'indirizzo del Worker, senza percorso");
  const conferma = values["conferma-impronta"] ?? "";
  if (!/^[0-9a-f]{12}$/.test(conferma))
    throw new Error("--conferma-impronta: le prime 12 cifre dell'impronta mostrata dalla prova a vuoto");
  return { database: values.database, invia: { base: u.origin, conferma } };
}

/** Ingresso del comando (separato per i test: `fetch` e ambiente iniettati). */
export async function main(
  argv: readonly string[],
  deps: { fetch: Fetch; env: Record<string, string | undefined>; log: (s: string) => void },
): Promise<number> {
  const cli = parseCli(argv);
  const piano = await readImmutable(cli.database, (db) => preparaMigrazione(db));
  deps.log(riepilogo(piano));
  if (cli.invia === false) return 0;
  if (piano.manifesto.impronta_logica.slice(0, 12) !== cli.invia.conferma) {
    deps.log("Invio fermato: l'impronta confermata non e' quella di questo database.");
    return 1;
  }
  const clientId = deps.env.CF_ACCESS_CLIENT_ID ?? "";
  const clientSecret = deps.env.CF_ACCESS_CLIENT_SECRET ?? "";
  if (clientId === "" || clientSecret === "") {
    deps.log("Invio fermato: mancano CF_ACCESS_CLIENT_ID e CF_ACCESS_CLIENT_SECRET (token di servizio).");
    return 1;
  }
  const e = await inviaMigrazione(piano, { base: cli.invia.base, fetch: deps.fetch, clientId, clientSecret });
  const impronta = e.risposta.impronta_logica;
  deps.log(
    e.ok
      ? `Migrazione completata. Impronta nel cloud ${impronta === piano.manifesto.impronta_logica ? "UGUALE" : "DIVERSA"} a quella del Mac.`
      : `Migrazione NON completata: fase ${e.fase}, HTTP ${e.status}, ${String(e.risposta.errore ?? "")} ${String(e.risposta.motivo ?? "")}`.trim(),
  );
  return e.ok && impronta === piano.manifesto.impronta_logica ? 0 : 1;
}

if (import.meta.main) {
  main(process.argv.slice(2), { fetch: (u, i) => fetch(u, i), env: process.env, log: (s) => console.log(s) })
    .then((code) => {
      process.exitCode = code;
    })
    .catch((err) => {
      console.error(`migrazione bloccata: ${err instanceof Error ? err.message : String(err)}`);
      process.exitCode = 1;
    });
}
