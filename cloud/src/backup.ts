/**
 * Formato di backup `cuore.finance.backup/1` (mandato L1, docs/mandati/2026-10-30-l1-backup-esportazione-do.md).
 * Esportazione logica (tabelle escluse `sqlite_*` e `_cf_*`) cifrata AES-GCM a blocchi, firmata ECDSA P-256, con
 * impronta logica portabile (stessa formula di `finance/scripts/import-plan.ts` `logicalChecksum`, ma con
 * `sha256Hex` di Web Crypto: identica in workerd e in Node, verificata nei test contro quella di Node).
 *
 * Solo Web Crypto (`crypto.subtle`), nessuna libreria esterna: lo stesso codice gira nel Durable Object e sul Mac.
 * Chiave dati AES-256 nuova a ogni backup, cifrata per due destinatari (Mac + recupero offline, C-09 decisione 3).
 *
 * Un blocco alterato, tolto, duplicato o scambiato viene rilevato: l'AAD della cifratura lega ogni blocco al suo
 * indice, al totale e all'intestazione; il manifesto (blocco 0) porta inoltre l'impronta di ciascun blocco dati,
 * confrontata dopo la decifratura; la firma copre intestazione e impronte dei blocchi.
 *
 * Solo il motore Finance portabile (`finance/src/db/migrations`): le tabelle operative del Durable Object, tutte
 * col prefisso `cloud_` (coda, lavori, notifiche, catture, richieste idempotenti: automazioni.ts, cattura.ts,
 * finance-api.ts), non hanno senso fuori dal cloud e non esistono nel motore sul Mac, quindi sono escluse.
 * Esclusa anche `schema_migrations`: le sue righe sono immutabili (un trigger ne impedisce la cancellazione,
 * `finance/src/db/migrate.ts`) e identiche su entrambi i lati quando il motore e' alla stessa versione — e'
 * `migrate()` sul Mac a ricrearla, non il ripristino.
 */
import type { webcrypto } from "node:crypto";
import { NOT_DERIVED } from "../../finance/src/db/schema-check.ts";
import type { Db } from "../../finance/src/db/types.ts";
import { canonicalJson, type Json, sha256Hex } from "../../finance/src/domain/hash.ts";

export const BACKUP_SCHEMA = "cuore.finance.backup/1";

/**
 * Tabelle del motore Finance portabile: escluse le interne di SQLite (`sqlite_*`, compresa `sqlite_sequence`),
 * quelle del DO (`_cf_*`) e le tabelle operative del Durable Object (`cloud_*`, vedi sopra).
 */
export const PROJECT_TABLES_SQL = `SELECT name FROM sqlite_master WHERE type = 'table' AND substr(name, 1, 7) <> 'sqlite_' AND substr(name, 1, 4) <> '_cf_' AND substr(name, 1, 6) <> 'cloud_' AND name <> 'schema_migrations' AND ${NOT_DERIVED} ORDER BY name`;

export interface TableExport {
  readonly table: string;
  readonly columns: readonly string[];
  readonly rows: readonly unknown[][];
}

/** Legge tutte le tabelle del progetto, righe ordinate per `rowid` (come `logicalChecksum` di Node). */
export async function readExportableTables(db: Db): Promise<TableExport[]> {
  const tables = await db.all<{ name: string }>(PROJECT_TABLES_SQL);
  const out: TableExport[] = [];
  for (const { name } of tables) {
    const rows = await db.all<Record<string, unknown>>(`SELECT * FROM "${name}" ORDER BY rowid`);
    const columns = rows.length > 0 ? Object.keys(rows[0] as Record<string, unknown>) : [];
    out.push({ table: name, columns, rows: rows.map((r) => columns.map((c) => r[c] ?? null)) });
  }
  return out;
}

/** `sqlite_sequence` (contatori `AUTOINCREMENT`), esportata a parte: non e' una tabella del progetto. */
export async function readSqliteSequence(db: Db): Promise<[string, number][]> {
  const has = await db.first<{ name: string }>(
    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'",
  );
  if (has === null) return [];
  const rows = await db.all<{ name: string; seq: number }>(
    "SELECT name, seq FROM sqlite_sequence ORDER BY name",
  );
  return rows.map((r) => [r.name, r.seq]);
}

/** Stessa formula di `logicalChecksum` (Node, `node:crypto`), ma con Web Crypto: identica in workerd e in Node. */
export async function logicalChecksumPortable(db: Db): Promise<string> {
  const tables = await readExportableTables(db);
  const parts = tables.map((t) => `${t.table}:${JSON.stringify(t.rows.map((r) => rowObject(t.columns, r)))}`);
  return sha256Hex(parts.join("\n"));
}

function rowObject(columns: readonly string[], row: readonly unknown[]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  columns.forEach((c, i) => {
    out[c] = row[i];
  });
  return out;
}

export interface Recipient {
  readonly kid: string;
  readonly publicKey: webcrypto.CryptoKey; // RSA-OAEP, SHA-256
}

export interface RecipientPrivate {
  readonly kid: string;
  readonly privateKey: webcrypto.CryptoKey; // RSA-OAEP, SHA-256
}

export async function recipientKid(publicKey: webcrypto.CryptoKey): Promise<string> {
  const spki = await crypto.subtle.exportKey("spki", publicKey);
  return sha256Hex(new Uint8Array(spki));
}

export interface BackupKeys {
  readonly signingKey: webcrypto.CryptoKey; // ECDSA P-256, privata
  readonly recipients: readonly Recipient[]; // Mac + recupero offline (C-09 decisione 3)
}

/**
 * Chiavi del Durable Object da variabili d'ambiente (JWK, SOLO chiavi di prova in locale: mai un segreto reale,
 * mai in produzione — esclusioni del mandato L1). Assenti o non valide = `null`: la rotta di esportazione resta
 * chiusa per difetto, come `VAPID_PRIVATE_JWK` per il push (push.ts).
 */
export async function parseBackupKeys(env: {
  readonly BACKUP_SIGNING_PRIVATE_JWK?: string;
  readonly BACKUP_MAC_PUBLIC_JWK?: string;
  readonly BACKUP_RECOVERY_PUBLIC_JWK?: string;
}): Promise<BackupKeys | null> {
  try {
    const signingKey = await crypto.subtle.importKey(
      "jwk",
      JSON.parse(env.BACKUP_SIGNING_PRIVATE_JWK ?? ""),
      { name: "ECDSA", namedCurve: "P-256" },
      false,
      ["sign"],
    );
    const importPublic = async (text: string | undefined): Promise<Recipient> => {
      const publicKey = await crypto.subtle.importKey(
        "jwk",
        JSON.parse(text ?? ""),
        { name: "RSA-OAEP", hash: "SHA-256" },
        true,
        ["encrypt"],
      );
      return { kid: await recipientKid(publicKey), publicKey };
    };
    const recipients = await Promise.all([
      importPublic(env.BACKUP_MAC_PUBLIC_JWK),
      importPublic(env.BACKUP_RECOVERY_PUBLIC_JWK),
    ]);
    return { signingKey, recipients };
  } catch {
    return null;
  }
}

export interface BackupHeader {
  readonly schema: typeof BACKUP_SCHEMA;
  readonly creato: string;
  readonly versione_schema: number;
  readonly write_seq: number;
  readonly origine: "durable-object" | "sintetico";
  readonly cifrario: "A256GCM";
  readonly destinatari: readonly { kid: string; alg: "RSA-OAEP-256"; chiave_dati_cifrata: string }[];
  readonly blocchi: number;
}

interface Manifest {
  readonly tabelle: { table: string; columns: string[]; righe: number }[];
  readonly sqlite_sequence: [string, number][];
  readonly impronta_logica: string;
  readonly impronta_blocchi: string[]; // sha256 del testo cifrato di ogni blocco dati (indici 1..N)
}

export interface BackupBlock {
  readonly iv: string; // base64, 96 bit
  readonly cifrato: string; // base64
}

export interface BackupFile {
  readonly header: BackupHeader;
  readonly firma: string; // base64, ECDSA P-256 su header canonico + impronte dei blocchi
  readonly blocchi: readonly BackupBlock[]; // [0] = manifesto cifrato, [1..] = blocchi dati
}

const b64 = (b: ArrayBuffer | Uint8Array) =>
  Buffer.from(b instanceof Uint8Array ? b : new Uint8Array(b)).toString("base64");
const unb64 = (s: string) => new Uint8Array(Buffer.from(s, "base64"));

function u32be(n: number): Uint8Array {
  const b = new Uint8Array(4);
  new DataView(b.buffer).setUint32(0, n, false);
  return b;
}

function concatBytes(...parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let o = 0;
  for (const p of parts) {
    out.set(p, o);
    o += p.length;
  }
  return out;
}

function headerBytes(header: BackupHeader): Uint8Array {
  return new TextEncoder().encode(canonicalJson(header as unknown as Json));
}

/** AAD = intestazione canonica + indice del blocco (4 byte) + numero totale (4 byte): lega ogni blocco alla sua posizione. */
function aad(header: BackupHeader, index: number, total: number): Uint8Array {
  return concatBytes(headerBytes(header), u32be(index), u32be(total));
}

async function encryptBlock(
  key: webcrypto.CryptoKey,
  header: BackupHeader,
  index: number,
  total: number,
  plaintext: Uint8Array,
): Promise<BackupBlock> {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const cifrato = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv, additionalData: aad(header, index, total) },
    key,
    plaintext,
  );
  return { iv: b64(iv), cifrato: b64(cifrato) };
}

/** Lancia se il blocco e' alterato, fuori posto o cifrato con un'altra chiave/intestazione (AEAD + AAD). */
async function decryptBlock(
  key: webcrypto.CryptoKey,
  header: BackupHeader,
  index: number,
  total: number,
  block: BackupBlock,
): Promise<Uint8Array> {
  const plaintext = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: unb64(block.iv), additionalData: aad(header, index, total) },
    key,
    unb64(block.cifrato),
  );
  return new Uint8Array(plaintext);
}

function chunk<T>(items: readonly T[], size: number): T[][] {
  if (items.length === 0) return [];
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

export interface CreateBackupInput {
  readonly tables: readonly TableExport[];
  readonly sqliteSequence: readonly [string, number][];
  readonly versioneSchema: number;
  readonly writeSeq: number;
  readonly origine: "durable-object" | "sintetico";
  readonly recipients: readonly Recipient[]; // esattamente i destinatari a cui cifrare la chiave dati
  readonly signingKey: webcrypto.CryptoKey; // ECDSA P-256, privata
  readonly rowsPerBlock?: number; // predefinito 500
  readonly now?: () => string;
}

/** Costruisce il file di backup: manifesto (blocco 0) + blocchi dati, cifrati per i destinatari e firmati. */
export async function createBackup(input: CreateBackupInput): Promise<BackupFile> {
  const rowsPerBlock = input.rowsPerBlock ?? 500;
  const dataChunks: { table: string; columns: readonly string[]; rows: readonly unknown[][] }[] = [];
  for (const t of input.tables)
    for (const rows of chunk(t.rows, rowsPerBlock))
      dataChunks.push({ table: t.table, columns: t.columns, rows });

  const dataKey = await crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, true, [
    "encrypt",
    "decrypt",
  ]);
  const rawKey = new Uint8Array(await crypto.subtle.exportKey("raw", dataKey));
  const destinatari = await Promise.all(
    input.recipients.map(async (r) => ({
      kid: r.kid,
      alg: "RSA-OAEP-256" as const,
      chiave_dati_cifrata: b64(await crypto.subtle.encrypt({ name: "RSA-OAEP" }, r.publicKey, rawKey)),
    })),
  );

  const header: BackupHeader = {
    schema: BACKUP_SCHEMA,
    creato: (input.now ?? (() => new Date().toISOString()))(),
    versione_schema: input.versioneSchema,
    write_seq: input.writeSeq,
    origine: input.origine,
    cifrario: "A256GCM",
    destinatari,
    blocchi: dataChunks.length + 1,
  };
  const total = header.blocchi;

  const blocchiDati: BackupBlock[] = [];
  const impronteBlocchi: string[] = [];
  for (let i = 0; i < dataChunks.length; i++) {
    const c = dataChunks[i] as { table: string; columns: readonly string[]; rows: readonly unknown[][] };
    const plaintext = new TextEncoder().encode(
      JSON.stringify({ tabella: c.table, colonne: c.columns, righe: c.rows }),
    );
    const block = await encryptBlock(dataKey, header, i + 1, total, plaintext);
    blocchiDati.push(block);
    impronteBlocchi.push(await sha256Hex(block.cifrato));
  }

  const manifest: Manifest = {
    tabelle: input.tables.map((t) => ({ table: t.table, columns: [...t.columns], righe: t.rows.length })),
    sqlite_sequence: input.sqliteSequence.map(([n, s]) => [n, s]),
    impronta_logica: await logicalChecksumFromTables(input.tables),
    impronta_blocchi: impronteBlocchi,
  };
  const manifestBlock = await encryptBlock(
    dataKey,
    header,
    0,
    total,
    new TextEncoder().encode(JSON.stringify(manifest)),
  );

  const firma = b64(
    await crypto.subtle.sign(
      { name: "ECDSA", hash: "SHA-256" },
      input.signingKey,
      concatBytes(headerBytes(header), new TextEncoder().encode(impronteBlocchi.join("\n"))),
    ),
  );

  return { header, firma, blocchi: [manifestBlock, ...blocchiDati] };
}

/** Impronta logica calcolata da tabelle gia' lette (stessa formula di `logicalChecksumPortable`). */
export async function logicalChecksumFromTables(tables: readonly TableExport[]): Promise<string> {
  const parts = tables.map((t) => `${t.table}:${JSON.stringify(t.rows.map((r) => rowObject(t.columns, r)))}`);
  return sha256Hex(parts.join("\n"));
}

export class BackupVerificationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "BackupVerificationError";
  }
}

export interface OpenBackupInput {
  readonly recipient: RecipientPrivate;
  readonly verifyingKey: webcrypto.CryptoKey; // ECDSA P-256, pubblica
  readonly expectedVersioneSchema: number;
  /** ultimo `write_seq` gia' accettato: un valore minore o uguale e' un backup piu' vecchio (rifiutato). */
  readonly minWriteSeq?: number;
}

export interface OpenedBackup {
  readonly header: BackupHeader;
  readonly tables: TableExport[];
  readonly sqliteSequence: [string, number][];
  readonly impronta_logica: string;
}

/** Verifica firma, intestazione, destinatario e ogni blocco; ricompone le tabelle. Mai testo in chiaro su disco. */
export async function openBackup(file: BackupFile, opts: OpenBackupInput): Promise<OpenedBackup> {
  const { header, firma, blocchi } = file;
  if (header.schema !== BACKUP_SCHEMA) throw new BackupVerificationError("schema di backup non riconosciuto");
  if (header.versione_schema !== opts.expectedVersioneSchema)
    throw new BackupVerificationError("versione dello schema diversa da quella del motore");
  if (opts.minWriteSeq !== undefined && header.write_seq <= opts.minWriteSeq)
    throw new BackupVerificationError("backup piu' vecchio dell'ultimo accettato");
  if (blocchi.length !== header.blocchi)
    throw new BackupVerificationError("numero di blocchi diverso da quello dichiarato");

  const dest = header.destinatari.find((d) => d.kid === opts.recipient.kid);
  if (dest === undefined)
    throw new BackupVerificationError("nessun destinatario corrisponde alla chiave data");
  let rawKey: Uint8Array;
  try {
    rawKey = new Uint8Array(
      await crypto.subtle.decrypt(
        { name: "RSA-OAEP" },
        opts.recipient.privateKey,
        unb64(dest.chiave_dati_cifrata),
      ),
    );
  } catch {
    throw new BackupVerificationError("chiave privata sbagliata o chiave dati cifrata alterata");
  }
  const dataKey = await crypto.subtle.importKey("raw", rawKey, { name: "AES-GCM" }, false, ["decrypt"]);

  const impronteBlocchi = await Promise.all(blocchi.slice(1).map((b) => sha256Hex(b.cifrato)));
  const firmaOk = await crypto.subtle.verify(
    { name: "ECDSA", hash: "SHA-256" },
    opts.verifyingKey,
    unb64(firma),
    concatBytes(headerBytes(header), new TextEncoder().encode(impronteBlocchi.join("\n"))),
  );
  if (!firmaOk) throw new BackupVerificationError("firma assente o non valida");

  const total = header.blocchi;
  let manifestBytes: Uint8Array;
  try {
    manifestBytes = await decryptBlock(dataKey, header, 0, total, blocchi[0] as BackupBlock);
  } catch {
    throw new BackupVerificationError("manifesto alterato o fuori posto");
  }
  const manifest = JSON.parse(new TextDecoder().decode(manifestBytes)) as Manifest;
  if (manifest.impronta_blocchi.length !== blocchi.length - 1)
    throw new BackupVerificationError("numero di blocchi dati diverso da quello del manifesto");
  manifest.impronta_blocchi.forEach((imp, i) => {
    if (imp !== impronteBlocchi[i])
      throw new BackupVerificationError(`blocco ${i + 1} tolto, duplicato o scambiato`);
  });

  const byTable = new Map<string, { columns: string[]; rows: unknown[][] }>();
  for (let i = 1; i < blocchi.length; i++) {
    let plaintext: Uint8Array;
    try {
      plaintext = await decryptBlock(dataKey, header, i, total, blocchi[i] as BackupBlock);
    } catch {
      throw new BackupVerificationError(`blocco ${i} alterato`);
    }
    const parsed = JSON.parse(new TextDecoder().decode(plaintext)) as {
      tabella: string;
      colonne: string[];
      righe: unknown[][];
    };
    const existing = byTable.get(parsed.tabella);
    if (existing === undefined)
      byTable.set(parsed.tabella, { columns: parsed.colonne, rows: [...parsed.righe] });
    else existing.rows.push(...parsed.righe);
  }
  const tables: TableExport[] = manifest.tabelle.map((t) => {
    const found = byTable.get(t.table) ?? { columns: t.columns, rows: [] };
    if (found.rows.length !== t.righe)
      throw new BackupVerificationError(`conteggio di "${t.table}" diverso dal manifesto`);
    return { table: t.table, columns: found.columns, rows: found.rows };
  });

  const impronta = await logicalChecksumFromTables(tables);
  if (impronta !== manifest.impronta_logica)
    throw new BackupVerificationError("impronta logica diversa dal manifesto");

  return { header, tables, sqliteSequence: manifest.sqlite_sequence, impronta_logica: impronta };
}
