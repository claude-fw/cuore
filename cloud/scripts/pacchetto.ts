#!/usr/bin/env node
/**
 * Prova del PACCHETTO DI PRODUZIONE senza rete e senza account: `wrangler deploy --dry-run` con `wrangler.jsonc`
 * costruisce il Worker esattamente come in una pubblicazione e si ferma prima di caricarlo («--dry-run: exiting now»).
 *
 *   npm run pacchetto            (dalla cartella cloud/; uscita in una cartella temporanea FUORI dal repository)
 *   npm run pacchetto -- --json  (stesso controllo, risultato in JSON)
 *
 * Rete: metriche spente, download di `cf.json` spento, credenziali Cloudflare tolte dall'ambiente e proxy verso una
 * porta chiusa (127.0.0.1:9): qualunque tentativo di collegarsi fallirebbe invece di uscire.
 * Poi controlla i file prodotti: dimensione (anche compressa) contro i limiti dei piani, nessun modulo `node:`, nessuna
 * traccia dell'accesso simulato (src/locale.ts) ne' dei valori di prova locali, nessun segreto riconoscibile.
 */
import { spawnSync } from "node:child_process";
import { mkdtempSync, readdirSync, readFileSync, realpathSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { parseArgs } from "node:util";
import { gzipSync } from "node:zlib";

const CLOUD = new URL("..", import.meta.url).pathname;
const REPO = new URL("../..", import.meta.url).pathname;
const WRANGLER = new URL("../../finance/node_modules/.bin/wrangler", import.meta.url).pathname;

/**
 * Limiti di dimensione del Worker DOPO la compressione (pagina «Workers limits», gia' citata in
 * §2). Valori NON riletti in questa sessione (nessuna rete): da riconfermare sulla pagina prima della pubblicazione.
 * Si usano i megabyte decimali, il caso piu' prudente.
 */
export const LIMITI = {
  free: 3_000_000,
  paid: 10_000_000,
  fonte:
    "https://developers.cloudflare.com/workers/platform/limits/ (da riconfermare: non riletta senza rete)",
} as const;

/**
 * Tracce dell'ingresso SOLO LOCALE, dei valori fittizi di wrangler.locale.jsonc e del banco di misura: mai nel pacchetto
 * di produzione.
 */
export const TRACCE_LOCALI = [
  "__locale",
  "accesso-simulato",
  "cuore_accesso_simulato",
  "ACCESSO SIMULATO",
  "vetrina.invalid",
  "simulato-locale",
  "macschoolsimulato",
  "segreto-simulato",
  "cattura-locale-di-prova",
  "c9be647e9676413d7bfef55ff24d9c97cd77187e71d3bf555b7438d7f6573cf0",
  "cuore-locale-non-pubblicare",
  "macbackupsimulato",
  "cuore-backup-locale-non-pubblicare",
  // banco di misura (Worker separato, wrangler.misure.jsonc): mai dentro la vetrina
  "BancoMisure",
  "cloud_banco",
] as const;

/** Forme riconoscibili di segreti (chiavi private, token noti). Nessun valore reale e' mai stato nel repository. */
export const SEGRETI: readonly [string, RegExp][] = [
  ["chiave privata PEM", /-----BEGIN [A-Z ]*PRIVATE KEY-----/],
  ["chiave privata JWK (campo d)", /"d"\s*:\s*"[A-Za-z0-9_-]{20,}"/],
  ["token GitHub", /\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}/],
  ["chiave API stile sk-", /\bsk-[A-Za-z0-9_-]{24,}/],
  ["chiave AWS", /\bAKIA[0-9A-Z]{16}\b/],
  ["token Slack", /\bxox[abprs]-[A-Za-z0-9-]{10,}/],
  ["intestazione con segreto di Access", /CF-Access-Client-Secret["']?\s*[:=,]\s*["'][^"']{8,}/i],
];

const MODULI_NODE = /(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*)["']node:[a-z_/]+["']/;

export interface FilePacchetto {
  readonly nome: string;
  readonly byte: number;
  readonly compressi: number;
}

export interface EsitoPacchetto {
  readonly cartella: string;
  readonly file: readonly FilePacchetto[];
  readonly byte: number;
  readonly compressi: number;
  /** come lo dichiara wrangler («Total Upload: … / gzip: …»), se trovato */
  readonly wrangler: string | null;
  readonly entroFree: boolean;
  readonly entroPaid: boolean;
  readonly problemi: readonly string[];
}

/** File caricati alla pubblicazione: tutto tranne le mappe dei sorgenti (non caricate senza `upload_source_maps`) e il README di wrangler. */
function fileCaricati(cartella: string): string[] {
  return readdirSync(cartella)
    .filter((f) => !f.endsWith(".map") && f !== "README.md")
    .filter((f) => statSync(join(cartella, f)).isFile())
    .sort();
}

/** Controlla una cartella prodotta da `wrangler deploy --dry-run --outdir`. Funzione pura sui file: nessuna rete. */
export function analizzaPacchetto(cartella: string, wrangler: string | null = null): EsitoPacchetto {
  const problemi: string[] = [];
  const file: FilePacchetto[] = [];
  for (const nome of fileCaricati(cartella)) {
    const dati = readFileSync(join(cartella, nome));
    file.push({ nome, byte: dati.length, compressi: gzipSync(dati).length });
    const testo = dati.toString("utf8");
    if (/\.(m?js)$/.test(nome) && MODULI_NODE.test(testo)) problemi.push(`${nome}: importa un modulo node:`);
    for (const t of TRACCE_LOCALI)
      if (testo.includes(t))
        problemi.push(`${nome}: contiene una traccia dell'accesso simulato o dei valori locali («${t}»)`);
    for (const [cosa, re] of SEGRETI)
      if (re.test(testo)) problemi.push(`${nome}: sembra contenere un segreto (${cosa})`);
  }
  const principale = file.find((f) => f.nome === "index.js");
  if (principale === undefined) problemi.push("manca il modulo principale index.js");
  else {
    const testo = readFileSync(join(cartella, "index.js"), "utf8");
    if (!testo.includes("// src/index.ts"))
      problemi.push("index.js non viene da src/index.ts (ingresso di produzione)");
    if (testo.includes("// src/locale.ts"))
      problemi.push("index.js contiene src/locale.ts (ingresso SOLO locale)");
  }
  const byte = file.reduce((n, f) => n + f.byte, 0);
  const compressi = file.reduce((n, f) => n + f.compressi, 0);
  const entroFree = compressi <= LIMITI.free;
  if (!entroFree)
    problemi.push(`pacchetto compresso oltre il limite del piano Free (${compressi} > ${LIMITI.free} byte)`);
  return {
    cartella,
    file,
    byte,
    compressi,
    wrangler,
    entroFree,
    entroPaid: compressi <= LIMITI.paid,
    problemi,
  };
}

/** Ambiente per wrangler senza rete ne' credenziali. */
export function ambienteSenzaRete(base: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const [k, v] of Object.entries(base))
    if (!/^(CLOUDFLARE_|CF_|WRANGLER_)/.test(k) && !/proxy/i.test(k)) env[k] = v;
  return {
    ...env,
    WRANGLER_SEND_METRICS: "false",
    CLOUDFLARE_CF_FETCH_ENABLED: "false",
    HTTPS_PROXY: "http://127.0.0.1:9",
    HTTP_PROXY: "http://127.0.0.1:9",
    NO_PROXY: "",
  };
}

/** Costruisce il pacchetto di produzione in `cartella` (deve stare fuori dal repository). */
export function costruisciPacchetto(cartella: string): { wrangler: string | null } {
  const rel = relative(realpathSync(REPO), realpathSync(cartella));
  if (!rel.startsWith("..")) throw new Error("la cartella d'uscita deve stare fuori dal repository");
  const r = spawnSync(WRANGLER, ["deploy", "--dry-run", "--outdir", cartella, "-c", "wrangler.jsonc"], {
    cwd: CLOUD,
    env: ambienteSenzaRete(process.env),
    encoding: "utf8",
    timeout: 120_000,
  });
  const uscita = `${r.stdout ?? ""}\n${r.stderr ?? ""}`;
  if (r.status !== 0 || !uscita.includes("--dry-run: exiting now"))
    throw new Error(
      `wrangler non ha completato la prova a vuoto (codice ${r.status}): ${uscita.slice(-600)}`,
    );
  return { wrangler: /Total Upload:[^\n]*/.exec(uscita)?.[0]?.trim() ?? null };
}

const kib = (n: number) => `${(n / 1024).toFixed(1)} KiB`;

export function testo(e: EsitoPacchetto): string {
  return [
    `Pacchetto di produzione (wrangler.jsonc, prova a vuoto, nessuna rete)`,
    `Cartella: ${e.cartella}`,
    ...e.file.map((f) => `  ${f.nome}: ${kib(f.byte)} (compresso ${kib(f.compressi)})`),
    `Totale: ${kib(e.byte)} · compresso ${kib(e.compressi)}${e.wrangler ? ` · wrangler: ${e.wrangler}` : ""}`,
    `Limite Free ${kib(LIMITI.free)} compresso: ${e.entroFree ? "ENTRO" : "OLTRE"} (${((e.compressi / LIMITI.free) * 100).toFixed(1)}%)`,
    `Limite Paid ${kib(LIMITI.paid)} compresso: ${e.entroPaid ? "ENTRO" : "OLTRE"} (${((e.compressi / LIMITI.paid) * 100).toFixed(1)}%)`,
    `Fonte dei limiti: ${LIMITI.fonte}`,
    e.problemi.length === 0
      ? "Esito: PRONTO (nessun modulo node:, nessun accesso simulato, nessun segreto)"
      : "Esito: PROBLEMI",
    ...e.problemi.map((p) => `  - ${p}`),
  ].join("\n");
}

if (import.meta.main) {
  const { values } = parseArgs({
    options: { json: { type: "boolean", default: false }, outdir: { type: "string" } },
  });
  try {
    const cartella = values.outdir ?? mkdtempSync(join(tmpdir(), "cuore-pacchetto-"));
    const { wrangler } = costruisciPacchetto(cartella);
    const esito = analizzaPacchetto(cartella, wrangler);
    console.log(values.json ? JSON.stringify(esito, null, 2) : testo(esito));
    process.exitCode = esito.problemi.length === 0 ? 0 : 1;
  } catch (err) {
    console.error(`pacchetto non costruito: ${err instanceof Error ? err.message : String(err)}`);
    process.exitCode = 1;
  }
}
