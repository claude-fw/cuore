/**
 * `Db` in SOLA LETTURA che passa dalla CLI di Wrangler (`wrangler d1 execute --json --command`), per eseguire la
 * stessa `verifyDatabase()` su un database raggiungibile solo tramite Wrangler (locale o, in futuro, remoto).
 *
 * - Nessun parametro legato (la CLI non li supporta): le query della verifica non ne usano; altrimenti errore.
 * - Nessuna scrittura: `batch()` e' vietato; ogni query deve iniziare con SELECT e non contenere ";" (PRAGMA non
 *   ammessi: su D1 molti non sono autorizzati e la verifica non li usa).
 * - Il bersaglio e' sempre esplicito: `--local` (con cartella di stato) oppure `--remote`. Mai un valore predefinito.
 *
 * ACCOUNT CLOUDFLARE (solo `--remote`). In Wrangler 4.147.0 l'account si sceglie, nell'ordine: account temporaneo,
 * `account_id` della configurazione, variabile `CLOUDFLARE_ACCOUNT_ID`, account in cache da una selezione precedente.
 * Per non dipendere da cache o da un ordine implicito, la verifica remota richiede che:
 *   1. l'ID sia passato esplicitamente (`accountId`), con i caratteri ammessi da Wrangler;
 *   2. la configurazione contenga `account_id` UNA sola volta, con lo stesso valore;
 *   3. `CLOUDFLARE_ACCOUNT_ID` dell'ambiente sia assente o identica.
 * Solo allora `CLOUDFLARE_ACCOUNT_ID` viene passata al processo di Wrangler. In `--local` non viene mai passata.
 * Le variabili di credenziali (`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_API_KEY`, ...) non vengono MAI inoltrate: il
 * processo usa solo il login OAuth gia' presente. Ogni controllo avviene PRIMA di avviare Wrangler.
 */
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { Db, SqlValue, Statement } from "../src/db/types.ts";

export type WranglerMode =
  | { readonly kind: "local"; readonly persistTo: string }
  | { readonly kind: "remote"; readonly accountId: string };

export interface WranglerTarget {
  readonly database: string;
  readonly config: string;
  readonly mode: WranglerMode;
  /** variabili d'ambiente aggiuntive (es. HOME nei test); mai `CLOUDFLARE_*` o `CF_*` */
  readonly env?: Record<string, string>;
}

export class WranglerTargetError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "WranglerTargetError";
  }
}

/** Stessi caratteri ammessi da Wrangler 4.147.0 (`validateAccountId`), piu' una lunghezza massima prudente. */
export const ACCOUNT_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

const WRANGLER = join(new URL("..", import.meta.url).pathname, "node_modules/wrangler/bin/wrangler.js");

/** Tutti i valori di `"account_id": "..."` presenti nel testo della configurazione (JSON o JSONC). */
export function configAccountIds(configText: string): string[] {
  return [...configText.matchAll(/"account_id"\s*:\s*"([^"]*)"/g)].map((m) => m[1] ?? "");
}

export function assertReadOnlySql(sql: string, params: readonly SqlValue[] = []): void {
  if (params.length > 0) throw new WranglerTargetError("WranglerCliDb: parametri non supportati");
  if (!/^\s*SELECT\b/i.test(sql))
    throw new WranglerTargetError("WranglerCliDb: solo query SELECT (sola lettura)");
  // `--command` accetta piu' istruzioni: un ";" permetterebbe di accodare una scrittura a una SELECT
  if (sql.includes(";")) {
    throw new WranglerTargetError("WranglerCliDb: una sola istruzione, senza ';' (sola lettura)");
  }
}

export interface ResolveDeps {
  readConfig(path: string): string;
  processEnv: Readonly<Record<string, string | undefined>>;
}

const defaultDeps: ResolveDeps = {
  readConfig: (path) => readFileSync(path, "utf8"),
  processEnv: process.env,
};

/** Argomenti di bersaglio e ambiente del processo di Wrangler, dopo tutti i controlli. Non avvia nulla. */
export function resolveInvocation(
  target: WranglerTarget,
  deps: ResolveDeps = defaultDeps,
): { modeArgs: string[]; env: Record<string, string> } {
  for (const key of Object.keys(target.env ?? {})) {
    if (/^(CLOUDFLARE_|CF_)/.test(key)) {
      throw new WranglerTargetError(
        `variabile ${key} non ammessa in env: l'account si indica solo con accountId`,
      );
    }
  }
  const env: Record<string, string> = { PATH: deps.processEnv.PATH ?? "" };
  const home = deps.processEnv.HOME;
  if (home !== undefined) env.HOME = home;
  Object.assign(env, target.env ?? {}, { WRANGLER_SEND_METRICS: "false", CI: "1" });

  if (target.mode.kind === "local") {
    return { modeArgs: ["--local", "--persist-to", target.mode.persistTo], env };
  }

  const accountId = target.mode.accountId;
  if (!ACCOUNT_ID_PATTERN.test(accountId)) {
    throw new WranglerTargetError(
      "ID account mancante o con caratteri non ammessi (alfanumerici, '-' e '_')",
    );
  }
  let configText: string;
  try {
    configText = deps.readConfig(target.config);
  } catch {
    throw new WranglerTargetError(`configurazione non leggibile: ${target.config}`);
  }
  const inConfig = configAccountIds(configText);
  if (inConfig.length === 0) {
    throw new WranglerTargetError(
      "la configurazione dedicata deve contenere account_id (Wrangler lo preferisce alla variabile)",
    );
  }
  if (inConfig.length > 1) {
    throw new WranglerTargetError("account_id compare piu' volte nella configurazione: bersaglio ambiguo");
  }
  if (inConfig[0] !== accountId) {
    throw new WranglerTargetError("account_id della configurazione diverso da quello indicato");
  }
  const fromEnv = deps.processEnv.CLOUDFLARE_ACCOUNT_ID;
  if (fromEnv !== undefined && fromEnv !== "" && fromEnv !== accountId) {
    throw new WranglerTargetError("CLOUDFLARE_ACCOUNT_ID dell'ambiente diverso da quello indicato");
  }
  env.CLOUDFLARE_ACCOUNT_ID = accountId;
  return { modeArgs: ["--remote"], env };
}

export class WranglerCliDb implements Db {
  readonly #t: WranglerTarget;
  readonly #modeArgs: string[];
  readonly #env: Record<string, string>;

  /** Valida subito bersaglio e account: un errore qui avviene prima di qualsiasi processo o richiesta di rete. */
  constructor(target: WranglerTarget, deps: ResolveDeps = defaultDeps) {
    this.#t = target;
    const { modeArgs, env } = resolveInvocation(target, deps);
    this.#modeArgs = modeArgs;
    this.#env = env;
  }

  async all<T>(sql: string, params: readonly SqlValue[] = []): Promise<T[]> {
    assertReadOnlySql(sql, params);
    const r = spawnSync(
      process.execPath,
      [
        WRANGLER,
        "d1",
        "execute",
        this.#t.database,
        ...this.#modeArgs,
        "--config",
        this.#t.config,
        "--json",
        "--command",
        sql,
      ],
      { encoding: "utf8", env: this.#env, timeout: 120_000 },
    );
    if (r.status !== 0)
      throw new Error(`wrangler d1 execute fallito (${r.status}): ${`${r.stderr}${r.stdout}`.slice(-500)}`);
    const parsed = JSON.parse(r.stdout) as { results: T[]; success: boolean }[];
    const first = parsed[0];
    if (parsed.length !== 1 || first === undefined || !first.success)
      throw new Error("risposta inattesa da Wrangler");
    return first.results;
  }

  async first<T>(sql: string, params: readonly SqlValue[] = []): Promise<T | null> {
    return (await this.all<T>(sql, params))[0] ?? null;
  }

  batch(_statements: readonly Statement[]): Promise<void> {
    return Promise.reject(new Error("WranglerCliDb e' in sola lettura"));
  }
}
