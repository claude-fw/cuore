/**
 * Scrittura con controllo ottimistico di concorrenza, compatibile con D1.
 *
 * Ogni operazione che scrive:
 *   1. legge `write_seq` (meta) prima di pianificare;
 *   2. invia un batch che inizia con una riga in `write_guard` il cui CHECK fallisce se nel frattempo
 *      un'altra scrittura ha cambiato `write_seq`; il batch e' atomico, quindi fallisce tutto;
 *   3. incrementa `write_seq`.
 * Cosi' un piano calcolato su dati vecchi non viene mai applicato (errore ConflictError, si ripete l'operazione).
 */
import type { Db, Statement } from "./types.ts";
import { stmt } from "./types.ts";

export class ConflictError extends Error {
  constructor() {
    super("conflitto di scrittura: i dati sono cambiati durante l'operazione; ripetere");
    this.name = "ConflictError";
  }
}

export async function readWriteSeq(db: Db): Promise<number> {
  const row = await db.first<{ value: string }>("SELECT value FROM meta WHERE key = 'write_seq'");
  if (row === null) throw new Error("database non inizializzato: eseguire le migrazioni");
  const seq = Number(row.value);
  if (!Number.isSafeInteger(seq)) throw new Error("write_seq non valido");
  return seq;
}

export function guardStatements(expectedSeq: number): Statement[] {
  return [
    stmt(
      "INSERT INTO write_guard (expected_seq, actual_seq) SELECT ?, CAST(value AS INTEGER) FROM meta WHERE key = 'write_seq'",
      expectedSeq,
    ),
    stmt("DELETE FROM write_guard"),
    stmt("UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'write_seq'"),
  ];
}

export async function commitGuarded(
  db: Db,
  expectedSeq: number,
  statements: readonly Statement[],
): Promise<void> {
  try {
    await db.batch([...guardStatements(expectedSeq), ...statements]);
  } catch (err) {
    if (err instanceof Error && err.message.includes("write_guard_seq")) throw new ConflictError();
    throw err;
  }
}
