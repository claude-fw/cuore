/**
 * BACKUP SETTIMANALE verificato del database Finance (prima della rotazione della domenica, C-08).
 *
 *   node scripts/backup-settimanale.ts --db <percorso assoluto del database>
 *
 * Stesso metodo e stesso schema dei nomi del backup prima della scrittura (`scrivi.ts`): copia coerente con
 * `VACUUM INTO` in `<db>/../../backup/<nome>-prima-di-settimanale-<istante>.sqlite3`, permessi 0600, `integrity_check`
 * ok e impronta del contenuto uguale a quella dell'originale prima e dopo la copia; altrimenti `.non-verificato`.
 * Al massimo uno al giorno (`presente` se c'e' gia'). Il database e' solo letto: nessuna migrazione, nessuna scrittura.
 * Nessun percorso predefinito: senza `--db` esce con 64. Rifiuti (percorso, iCloud, repository, verifica): 2.
 * Uscita: una riga JSON con esito e nome del file (nessun dato del database).
 */
import { parseArgs } from "node:util";
import { checkDb, Rifiuto, verifiedBackup } from "./scrivi.ts";

export async function main(argv: readonly string[]): Promise<number> {
  let db: string | undefined;
  try {
    db = parseArgs({ args: [...argv], options: { db: { type: "string" } } }).values.db;
  } catch {
    db = undefined;
  }
  if (db === undefined) {
    process.stderr.write("uso: backup-settimanale.ts --db <database>   (nessun percorso predefinito)\n");
    return 64;
  }
  try {
    const r = await verifiedBackup(checkDb(db), "settimanale");
    process.stdout.write(`${JSON.stringify({ ok: true, stato: r.stato, file: r.file })}\n`);
    return 0;
  } catch (err) {
    const motivo = err instanceof Rifiuto ? err.message : "errore imprevisto";
    process.stdout.write(`${JSON.stringify({ ok: false, errore: "rifiutato", motivo })}\n`);
    return 2;
  }
}

if (import.meta.main) process.exitCode = await main(process.argv.slice(2));
