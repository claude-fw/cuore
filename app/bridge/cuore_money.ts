/**
 * CUORE Money: lettura in SOLA LETTURA del database Cloud Finance tramite la consultazione v1 (`runQuery`), senza
 * modificare il motore Finance. Uscita: JSON `cuore.money.query/1` su stdout (nessun log, nessun file).
 *
 *   node app/bridge/cuore_money.ts --db <database.sqlite3> [--today YYYY-MM-DD]
 *
 * Sola lettura garantita da SQLite, non da Node: `readImmutable` del motore Finance (la stessa della CLI `query` e del
 * server MCP) copia un'istantanea coerente dei byte in memoria e interroga solo quella: nessuna scrittura, nessun lock,
 * nessun -wal/-shm; un -wal non vuoto o un file cambiato durante la copia sono errori. Verificato: il modello dei
 * permessi di Node 24 NON vincola `node:sqlite`. Chi chiama (cuore_money.py) ripete i controlli sui file prima/dopo.
 *
 * Contratto minimo: solo cio' che serve alle viste Money. Importi in unita' minime e valute separate (come il
 * contratto v1); descrizioni dei movimenti gia' mascherate da Finance; niente controparti (nemmeno nei crediti e
 * debiti), descrizioni dei crediti/debiti, profili di titolarita'. L'id interno del movimento (casuale, mai quello
 * della banca) c'e' dal 03/11: serve alle modifiche del titolare (categoria, correzione, annullamento). `manual`
 * (movimenti inseriti a mano, annullati compresi) e `budgets` (limiti del mese) sono aggiunte della v7 (A5). Nessuna
 * rete: il codice di consultazione non importa moduli di rete.
 *
 * Tutto passa dalla consultazione v1 (`runQuery`), comprese le operazioni additive `categories` (nomi e gruppi di
 * budget) e `planning` (obiettivi e acquisti): una sola fonte, una sola busta `{ ok, data | error }` per operazione.
 * `trend` non e' un'operazione nuova del motore: e' `summary` ripetuta per gli ultimi TREND_MONTHS mesi di calendario
 * (l'ultimo fino a oggi), ridotta a entrate, spesa netta, spesa per categoria e movimenti da classificare.
 * `daily` (04/11): serie giornaliera del motore dal primo giorno del mese precedente a oggi (spesa accumulata del mese
 * contro il mese prima, nel Riepilogo).
 */
import { pathToFileURL } from "node:url";
import { readImmutable } from "../../finance/src/db/node-sqlite.ts";
import { queryMoney } from "./cuore_money_query.mts";

/** Apertura immutabile del motore Finance (nome storico, usato dai test del ponte). */
export { ImmutableSqliteDb as ImmutableDb } from "../../finance/src/db/node-sqlite.ts";
export {
  monthStarts,
  queryMoney,
  SCHEMA,
  TRANSACTIONS_DAYS,
  TRANSACTIONS_LIMIT,
  TREND_MONTHS,
} from "./cuore_money_query.mts";

/**
 * `schema` (facoltativo) serve solo al ponte di prova (`prova_money.ts`, D3b): versione dello schema della copia
 * sintetica, che `runQuery` ammette solo su un database marcato `sintetico`. Senza, la lettura e' quella reale invariata.
 */
export function readMoney(dbPath: string, today: string, schema?: { expectedSchemaVersion: number }): Promise<Record<string, unknown>> {
  return readImmutable(dbPath, (db) => queryMoney(db, today, schema));
}

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  const db = arg("--db");
  const today = arg("--today") ?? new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Rome" }).format(new Date());
  if (!db || !/^\d{4}-\d{2}-\d{2}$/.test(today)) {
    process.stderr.write("uso: cuore_money.ts --db <file> [--today YYYY-MM-DD]\n");
    process.exitCode = 64;
  } else {
    readMoney(db, today).then(
      (out) => process.stdout.write(`${JSON.stringify(out)}\n`),
      (err: unknown) => {
        process.stderr.write(`errore: ${err instanceof Error ? err.message : String(err)}\n`);
        process.exitCode = 1;
      },
    );
  }
}
