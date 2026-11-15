#!/usr/bin/env node
/**
 * Ripristino di un backup `cuore.finance.backup/1` (mandato L1) in un database SQLite NUOVO, sul Mac.
 *
 *   node cloud/scripts/ripristina-backup.ts --file <backup.cuorebak.json> --destinazione <nuovo.sqlite>
 *     --chiave-privata <privata.jwk.json> --chiave-firma <firma-pubblica.jwk.json>
 *
 * Passi: verifica firma e blocchi (`openBackup`, provata contro ogni destinatario dell'intestazione con la chiave
 * data); database NUOVO con le stesse migrazioni del motore (mai sopra un file esistente); svuota le righe create
 * dalle migrazioni (`meta`, `sources`, `schema_migrations`, ...) tabella per tabella appena prima di inserire i dati
 * del backup, con `foreign_keys` disattivate; richiude e riapre con le chiavi esterne attive per `foreign_key_check`,
 * `integrity_check`, `compareSchema` contro una migrazione di riferimento in memoria e l'impronta logica del database
 * ripristinato contro quella del manifesto firmato.
 * Uscita: SOLO conteggi, versioni ed esiti (mai righe, importi o descrizioni). Nessuna rete, nessun file reale.
 */
import type { webcrypto } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { parseArgs } from "node:util";
import { migrate } from "../../finance/src/db/migrate.ts";
import { PENDING_MIGRATIONS } from "../../finance/src/db/migrations/007_manual_and_budgets.ts";
import { MIGRATIONS } from "../../finance/src/db/migrations/index.ts";
import { NodeSqliteDb } from "../../finance/src/db/node-sqlite.ts";
import { compareSchema, readSchema } from "../../finance/src/db/schema-check.ts";
import { type SqlValue, stmt } from "../../finance/src/db/types.ts";
import { type BackupFile, logicalChecksumPortable, type OpenedBackup, openBackup } from "../src/backup.ts";

const ALL_MIGRATIONS = [...MIGRATIONS, ...PENDING_MIGRATIONS];
/** Versione dello schema del motore (attive + in attesa): quella attesa per difetto nel backup. */
export const VERSIONE_MOTORE = ALL_MIGRATIONS.length;

interface Args {
  file: string;
  destinazione: string;
  /** percorso di un file JWK oppure la JWK gia' letta (es. dal Portachiavi, `chiavi-mac.ts`) */
  chiavePrivata: string | webcrypto.JsonWebKey;
  chiaveFirma: string | webcrypto.JsonWebKey;
  versioneSchema: number;
}

export function parseCli(argv: readonly string[]): Args {
  const { values } = parseArgs({
    args: [...argv],
    options: {
      file: { type: "string" },
      destinazione: { type: "string" },
      "chiave-privata": { type: "string" },
      "chiave-firma": { type: "string" },
      "versione-schema": { type: "string" },
    },
  });
  const { file, destinazione } = values;
  const chiavePrivata = values["chiave-privata"];
  const chiaveFirma = values["chiave-firma"];
  if (!file || !destinazione || !chiavePrivata || !chiaveFirma)
    throw new Error("argomenti richiesti: --file --destinazione --chiave-privata --chiave-firma");
  return {
    file,
    destinazione,
    chiavePrivata,
    chiaveFirma,
    versioneSchema: values["versione-schema"] ? Number(values["versione-schema"]) : VERSIONE_MOTORE,
  };
}

async function importJwk(
  src: string | webcrypto.JsonWebKey,
  algorithm: { name: string; hash?: string; namedCurve?: string },
  usages: readonly ("decrypt" | "verify")[],
): Promise<webcrypto.CryptoKey> {
  const jwk = typeof src === "string" ? JSON.parse(readFileSync(src, "utf8")) : src;
  return crypto.subtle.importKey("jwk", jwk, algorithm, true, usages);
}

/** Il file non porta il `kid` corretto da provare: si tenta con ogni destinatario dell'intestazione. */
async function apriConOgniDestinatario(
  file: BackupFile,
  privateKey: webcrypto.CryptoKey,
  verifyingKey: webcrypto.CryptoKey,
  expectedVersioneSchema: number,
): Promise<OpenedBackup> {
  let ultimoErrore: unknown = new Error("nessun destinatario nell'intestazione");
  for (const dest of file.header.destinatari) {
    try {
      return await openBackup(file, {
        recipient: { kid: dest.kid, privateKey },
        verifyingKey,
        expectedVersioneSchema,
      });
    } catch (err) {
      ultimoErrore = err;
    }
  }
  throw ultimoErrore;
}

/** Svuota e reinserisce ogni tabella del manifesto, nell'ordine del backup, con le chiavi esterne disattivate. */
async function scriviDati(destinazione: string, opened: OpenedBackup): Promise<void> {
  const db = new NodeSqliteDb(destinazione, { foreignKeys: false });
  try {
    await migrate(db, ALL_MIGRATIONS, () => new Date().toISOString());
    for (const t of opened.tables) {
      await db.batch([
        stmt(`DELETE FROM "${t.table}"`),
        ...t.rows.map((row) =>
          stmt(
            `INSERT INTO "${t.table}" (${t.columns.map((c) => `"${c}"`).join(", ")}) VALUES (${t.columns.map(() => "?").join(", ")})`,
            ...(row as readonly SqlValue[]),
          ),
        ),
      ]);
    }
  } finally {
    db.close();
  }
}

export interface RestoreReport {
  destinazione: string;
  versioneSchema: number;
  writeSeq: number;
  tabelle: { table: string; righe: number }[];
  sqliteSequenceCoerente: boolean;
  integrityCheck: string;
  foreignKeyErrori: number;
  schemaCoerente: boolean;
  /** impronta logica del database ripristinato (formula di backup.ts) e confronto con quella del manifesto firmato */
  impronta: string;
  improntaCoerente: boolean;
}

export async function ripristina(args: Args): Promise<RestoreReport> {
  if (existsSync(args.destinazione))
    throw new Error(`destinazione gia' esistente, non sovrascritta: ${args.destinazione}`);
  const file = JSON.parse(readFileSync(args.file, "utf8")) as BackupFile;
  const privateKey = await importJwk(args.chiavePrivata, { name: "RSA-OAEP", hash: "SHA-256" }, ["decrypt"]);
  const verifyingKey = await importJwk(args.chiaveFirma, { name: "ECDSA", namedCurve: "P-256" }, ["verify"]);
  const opened = await apriConOgniDestinatario(file, privateKey, verifyingKey, args.versioneSchema);

  await scriviDati(args.destinazione, opened);

  const db = new NodeSqliteDb(args.destinazione);
  try {
    const sqliteSequence = await db.all<{ name: string; seq: number }>(
      "SELECT name, seq FROM sqlite_sequence ORDER BY name",
    );
    const sqliteSequenceCoerente =
      JSON.stringify(sqliteSequence.map((r) => [r.name, r.seq])) === JSON.stringify(opened.sqliteSequence);

    const integrityCheck = db.integrityCheck();
    const foreignKeyErrori = (await db.all("PRAGMA foreign_key_check")).length;

    const reference = new NodeSqliteDb(":memory:");
    await migrate(reference, ALL_MIGRATIONS, () => new Date().toISOString());
    const diff = compareSchema(await readSchema(reference), await readSchema(db));
    reference.close();
    const schemaCoerente = diff.missing.length + diff.unexpected.length + diff.changed.length === 0;
    const impronta = await logicalChecksumPortable(db);

    return {
      destinazione: args.destinazione,
      versioneSchema: args.versioneSchema,
      writeSeq: opened.header.write_seq,
      tabelle: opened.tables.map((t) => ({ table: t.table, righe: t.rows.length })),
      sqliteSequenceCoerente,
      integrityCheck,
      foreignKeyErrori,
      schemaCoerente,
      impronta,
      improntaCoerente: impronta === opened.impronta_logica,
    };
  } finally {
    db.close();
  }
}

if (import.meta.main) {
  ripristina(parseCli(process.argv.slice(2)))
    .then((report) => {
      console.log(JSON.stringify(report, null, 2));
      if (
        report.integrityCheck !== "ok" ||
        report.foreignKeyErrori > 0 ||
        !report.schemaCoerente ||
        !report.sqliteSequenceCoerente ||
        !report.improntaCoerente
      )
        process.exitCode = 1;
    })
    .catch((err) => {
      console.error(`ripristino bloccato: ${err instanceof Error ? err.message : String(err)}`);
      process.exitCode = 1;
    });
}
