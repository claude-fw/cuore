#!/usr/bin/env node
/**
 * APPLICAZIONE della rotazione dei backup locali di Finance (C-08): politica 7 ultimi, 30 giornalieri, 12 mensili
 * (vedi `src/domain/backup-retention.ts` per il pianificatore puro).
 *
 *   node scripts/rotazione-backup.ts --cartella <backup> [--applica]
 *
 * Senza `--applica` e' una PROVA (default): stampa il piano (cosa terrebbe, cosa sposterebbe nel Cestino) senza
 * toccare alcun file. Solo con `--applica` i file scaduti vengono spostati nel Cestino con il comando di sistema
 * `trash` (mai `rm`: niente cancellazione definitiva). I nomi che non seguono lo schema di `stamp()` sono ignorati
 * e non vengono mai toccati. Il backup piu' recente di ogni database/tipo non scade mai.
 */
import { spawnSync } from "node:child_process";
import { readdirSync } from "node:fs";
import { parseArgs } from "node:util";
import { planRetention, type RetentionPolicy } from "../src/domain/backup-retention.ts";

/** Politica proposta e approvata in C-08. */
export const POLICY: RetentionPolicy = { keepLast: 7, keepDaily: 30, keepMonthly: 12 };

/** Sposta un file nel Cestino con `trash` (mai `rm`). Isolata per poterla sostituire nei test. */
export function trashFile(path: string): void {
  const r = spawnSync("trash", [path], { stdio: "pipe" });
  if (r.error) throw r.error;
  if (r.status !== 0) {
    throw new Error(`trash ha restituito ${r.status}: ${r.stderr?.toString() ?? ""}`);
  }
}

export interface RotationResult {
  cartella: string;
  politica: RetentionPolicy;
  tenuti: { name: string; reasons: string[] }[];
  scaduti: string[];
  ignorati: string[];
  applicato: boolean;
  spostatiNelCestino: string[];
}

/** Legge la cartella, pianifica, ed eventualmente sposta nel Cestino gli scaduti (mai gli ignorati o i tenuti). */
export function runRotation(
  cartella: string,
  policy: RetentionPolicy,
  options: { applica: boolean; trash?: (path: string) => void; listFiles?: (dir: string) => string[] },
): RotationResult {
  const listFiles = options.listFiles ?? ((dir: string) => readdirSync(dir));
  const trash = options.trash ?? trashFile;
  const names = listFiles(cartella);
  const plan = planRetention(names, policy);
  const spostatiNelCestino: string[] = [];
  if (options.applica) {
    for (const name of plan.expire) {
      trash(`${cartella}/${name}`);
      spostatiNelCestino.push(name);
    }
  }
  return {
    cartella,
    politica: policy,
    tenuti: plan.keep,
    scaduti: plan.expire,
    ignorati: plan.ignored,
    applicato: options.applica,
    spostatiNelCestino,
  };
}

async function main(argv: string[]): Promise<number> {
  const { values } = parseArgs({
    args: argv,
    options: {
      cartella: { type: "string" },
      applica: { type: "boolean", default: false },
    },
  });
  if (values.cartella === undefined) {
    process.stderr.write("uso: rotazione-backup.ts --cartella <backup> [--applica]\n");
    return 64;
  }
  const result = runRotation(values.cartella, POLICY, { applica: values.applica === true });
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
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
