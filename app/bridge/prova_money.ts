/**
 * Feed Money della COPIA SINTETICA per il ponte di prova (D3b, decisione C-10). Mai usato dal ponte reale.
 *
 *   node app/bridge/prova_money.ts --dati <cartella della sessione> [--today YYYY-MM-DD]
 *
 * Stessa lettura di `cuore_money.ts` (`readMoney`: istantanea immutabile, contratto minimo), ma solo dopo la verifica
 * di cartella, collegamenti, permessi e sessione (`locateSynthetic`, che non apre il database: nessun -wal/-shm, cosi'
 * il controllo «file cambiato durante la lettura» di `cuore_money.py` resta valido) e con lo schema della copia
 * sintetica, che `runQuery` ammette solo su un database marcato `sintetico` (marcatore ricontrollato a ogni
 * operazione). Il ponte di prova verifica anche il marcatore e l'id della sessione prima di lanciarlo.
 * Uscita: `cuore.money.query/1`.
 */
import { pathToFileURL } from "node:url";
import { SYNTHETIC_QUERY_SCHEMA_VERSION } from "../../finance/src/query/operations.ts";
import { locateSynthetic } from "../../finance/src/synthetic/guard.ts";
import { readMoney } from "./cuore_money.ts";

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  const dir = arg("--dati");
  const today = arg("--today") ?? new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Rome" }).format(new Date());
  if (!dir || !/^\d{4}-\d{2}-\d{2}$/.test(today)) {
    process.stderr.write("uso: prova_money.ts --dati <cartella> [--today YYYY-MM-DD]\n");
    process.exitCode = 64;
  } else {
    Promise.resolve()
      .then(() => locateSynthetic(dir))
      .then((v) => readMoney(v.path, today, { expectedSchemaVersion: SYNTHETIC_QUERY_SCHEMA_VERSION }))
      .then(
        (out) => process.stdout.write(`${JSON.stringify(out)}\n`),
        (err: unknown) => {
          process.stderr.write(`errore: ${err instanceof Error ? err.message : String(err)}\n`);
          process.exitCode = 1;
        },
      );
  }
}
