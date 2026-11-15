/**
 * Backup L1 CONSERVATI nel cloud (decisione L1 del 03/11: 7 copie). Una volta al giorno il lavoro `backup` della coda
 * (Cron → `plan`, automazioni.ts) scrive nel bucket R2 `BACKUP` un file `cuore.finance.backup/1` (backup.ts: cifrato
 * per i destinatari configurati, firmato) e tiene solo le ultime `BACKUP_COPIE`; il Mac lo ritira con il token di
 * servizio `mac-backup` (`GET /api/finance/backup`, app.ts) e lo ripristina (scripts/ritira-backup.ts).
 * Nel bucket c'e' solo testo cifrato: l'intestazione in chiaro non porta dati finanziari (backup.ts).
 * Chiuso per difetto: senza binding `BACKUP` niente lavoro e rotta 503; senza chiavi il lavoro si salta.
 * In produzione il binding e' commentato in wrangler.jsonc finche' R2 non e' autorizzato (R2 puo' chiedere un metodo
 * di pagamento all'attivazione [da verificare]); in locale lo simula Miniflare.
 */
import type { BackupFile } from "./backup.ts";

/** La parte dell'API di R2 usata qui (stessi nomi e firme del binding `R2Bucket` di Workers). */
export interface BackupBucket {
  put(
    key: string,
    value: string,
    options?: { httpMetadata?: { contentType?: string }; customMetadata?: Record<string, string> },
  ): Promise<unknown>;
  get(key: string): Promise<{ body: ReadableStream; size: number } | null>;
  list(options: {
    prefix: string;
    cursor?: string;
  }): Promise<{ objects: { key: string; size: number }[]; truncated: boolean; cursor?: string }>;
  delete(keys: string | string[]): Promise<void>;
}

export const BACKUP_PATH = "/api/finance/backup";
export const BACKUP_COPIE = 7;
const PREFIX = "finance/";
const SUFFIX = ".cuorebak.json";
const GIORNO = /^\d{4}-\d{2}-\d{2}$/;

export const giornoValido = (s: unknown): s is string =>
  typeof s === "string" && GIORNO.test(s) && !Number.isNaN(Date.parse(`${s}T00:00:00Z`));
export const nomeBackup = (giorno: string) => `${PREFIX}${giorno}${SUFFIX}`;

/** Copie presenti, dalla piu' recente: solo giorno e dimensione (mai contenuti). */
export async function elencoBackup(bucket: BackupBucket): Promise<{ giorno: string; byte: number }[]> {
  const out: { giorno: string; byte: number }[] = [];
  let cursor: string | undefined;
  do {
    const page = await bucket.list({ prefix: PREFIX, ...(cursor ? { cursor } : {}) });
    for (const o of page.objects) {
      const giorno = o.key.slice(PREFIX.length, -SUFFIX.length);
      if (o.key.endsWith(SUFFIX) && giornoValido(giorno)) out.push({ giorno, byte: o.size });
    }
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
  return out.sort((a, b) => b.giorno.localeCompare(a.giorno));
}

/** Scrive la copia del giorno (stesso giorno = sostituita) e poi elimina le piu' vecchie oltre `copie`. */
export async function salvaBackup(
  bucket: BackupBucket,
  giorno: string,
  file: BackupFile,
  copie = BACKUP_COPIE,
): Promise<{ giorno: string; byte: number; conservati: number; eliminati: number }> {
  if (!giornoValido(giorno)) throw new Error("giorno non valido");
  const testo = JSON.stringify(file);
  await bucket.put(nomeBackup(giorno), testo, {
    httpMetadata: { contentType: "application/json" },
    customMetadata: { write_seq: String(file.header.write_seq), creato: file.header.creato },
  });
  const tutti = await elencoBackup(bucket);
  const vecchi = tutti.slice(copie).map((c) => nomeBackup(c.giorno));
  if (vecchi.length > 0) await bucket.delete(vecchi);
  return { giorno, byte: testo.length, conservati: tutti.length - vecchi.length, eliminati: vecchi.length };
}
