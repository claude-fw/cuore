#!/usr/bin/env node
/**
 * Analisi LOCALE della struttura di un export bancario (sola lettura, nessuna rete, nessun file scritto).
 *
 *   node scripts/probe-format.ts --file <export>                         # 1. forme delle righe (nessun testo)
 *   node scripts/probe-format.ts --file <export> --header-row <N>        # 2. nomi delle colonne + forme per colonna
 *   node scripts/probe-format.ts --file <export> --header-row <N> --values-of "<colonna>" [--values-of ...]
 *                                                                        # 3. valori distinti (max 15) di colonne
 *                                                                        #    come "stato" o "tipo"
 * Opzioni: --sheet <nome> (XLSX), --delimiter "," | ";" | "tab" (CSV).
 * L'output va riletto prima di condividerlo. Il file di export non va mai copiato nel repository o in iCloud.
 */
import { readFileSync } from "node:fs";
import { parseArgs } from "node:util";
import type { CsvDelimiter } from "../src/domain/csv.ts";
import { probeFile } from "../src/import/probe.ts";

async function main(argv: string[]): Promise<number> {
  const { values } = parseArgs({
    args: argv,
    options: {
      file: { type: "string" },
      "header-row": { type: "string" },
      "values-of": { type: "string", multiple: true },
      sheet: { type: "string" },
      delimiter: { type: "string" },
    },
  });
  if (values.file === undefined) {
    process.stderr.write(
      "uso: probe-format.ts --file <export> [--header-row N] [--values-of <colonna>]...\n",
    );
    return 64;
  }
  const headerRow = values["header-row"] === undefined ? undefined : Number(values["header-row"]);
  if (headerRow !== undefined && !(Number.isInteger(headerRow) && headerRow >= 1)) {
    throw new Error("--header-row deve essere un numero di riga >= 1");
  }
  const d = values.delimiter;
  const delimiter: CsvDelimiter | undefined =
    d === undefined ? undefined : d === "tab" ? "\t" : (d as CsvDelimiter);
  if (delimiter !== undefined && ![",", ";", "\t"].includes(delimiter))
    throw new Error('--delimiter: "," ";" o "tab"');
  const report = await probeFile(new Uint8Array(readFileSync(values.file)), {
    ...(headerRow === undefined ? {} : { headerRow }),
    ...(values["values-of"] === undefined ? {} : { valuesOf: values["values-of"] }),
    ...(values.sheet === undefined ? {} : { sheet: values.sheet }),
    ...(delimiter === undefined ? {} : { delimiter }),
  });
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  process.stderr.write(
    "Ricontrolla l'output prima di condividerlo: non deve contenere nomi, IBAN o importi reali.\n",
  );
  return 0;
}

if (import.meta.main) {
  main(process.argv.slice(2)).then(
    (code) => {
      process.exitCode = code;
    },
    (err: unknown) => {
      process.stderr.write(`errore: ${err instanceof Error ? err.message : String(err)}\n`);
      process.exitCode = 2;
    },
  );
}
