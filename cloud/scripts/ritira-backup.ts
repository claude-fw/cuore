#!/usr/bin/env node
/**
 * L1 — il Mac RITIRA un backup conservato nel cloud (bucket R2, 7 copie: src/backup-cloud.ts) e lo ripristina in un
 * database NUOVO con `ripristina-backup.ts` (firma, blocchi, integrita', schema, impronta). Provato solo su dati
 * sintetici; con dati veri serve la chiave vera del Mac (decisione L1: da generare quando il cloud reale esiste).
 *
 *   CF_ACCESS_CLIENT_ID=... CF_ACCESS_CLIENT_SECRET=... npm run ritira-backup -- \
 *     --url https://cuore.<sottodominio>.workers.dev --cartella <cartella fuori da iCloud e dal repository> \
 *     [--giorno AAAA-MM-GG]
 *   Chiavi: per difetto dal Portachiavi (`npm run chiavi-mac`: privata del Mac `cuore.backup.mac`, pubblica di firma
 *   `cuore.backup.firma`); in alternativa file JWK con --chiave-privata <file> --chiave-firma <file> (solo prove).
 *   ... --elenco           solo i giorni disponibili (nessun file scaricato)
 *
 * Token di servizio `mac-backup` solo nell'ambiente (mai sulla riga di comando, mai stampato). Il file scaricato resta
 * cifrato; nulla viene sovrascritto (file o database gia' presenti = fermo). Uscita: solo giorni, conteggi ed esiti.
 */
import type { webcrypto } from "node:crypto";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { parseArgs } from "node:util";
import { BACKUP_PATH, giornoValido } from "../src/backup-cloud.ts";
import { chiaviRitiro, portachiaviMac } from "./chiavi-mac.ts";
import { type RestoreReport, ripristina, VERSIONE_MOTORE } from "./ripristina-backup.ts";

export interface Ritiro {
  readonly url: string;
  readonly cartella: string;
  readonly chiavePrivata: string | webcrypto.JsonWebKey;
  readonly chiaveFirma: string | webcrypto.JsonWebKey;
  readonly giorno?: string;
  readonly versioneSchema?: number;
}

type Fetch = (url: string, init: RequestInit) => Promise<Response>;

/** Base https (http solo verso il Mac stesso, per `npm run dev`); cartella mai in iCloud. */
function controlla(r: Ritiro): URL {
  const u = new URL(r.url);
  const locale = u.hostname === "127.0.0.1" || u.hostname === "localhost";
  if (u.protocol !== "https:" && !(locale && u.protocol === "http:"))
    throw new Error("solo indirizzi https (http solo per 127.0.0.1)");
  if (resolve(r.cartella).includes("/Library/Mobile Documents/"))
    throw new Error("la cartella e' in iCloud: i backup restano fuori da iCloud");
  if (r.giorno !== undefined && !giornoValido(r.giorno)) throw new Error("--giorno: AAAA-MM-GG");
  return u;
}

async function leggi(f: Fetch, url: URL, headers: Record<string, string>): Promise<Response> {
  const res = await f(url.toString(), { method: "GET", headers });
  if (!res.ok) {
    const d = (await res.json().catch(() => ({}))) as { errore?: string; motivo?: string };
    throw new Error(`il cloud risponde ${res.status} ${d.errore ?? ""} ${d.motivo ?? ""}`.trim());
  }
  return res;
}

export async function elenco(
  r: Pick<Ritiro, "url">,
  headers: Record<string, string>,
  f: Fetch,
): Promise<{ giorno: string; byte: number }[]> {
  const base = new URL(r.url);
  const res = await leggi(f, new URL(`${BACKUP_PATH}/elenco`, base), headers);
  return ((await res.json()) as { copie: { giorno: string; byte: number }[] }).copie;
}

/** Scarica (il piu' recente o quello del giorno), salva il file cifrato e ripristina in un database nuovo. */
export async function ritira(
  r: Ritiro,
  headers: Record<string, string>,
  f: Fetch,
): Promise<{ giorno: string; file: string; report: RestoreReport }> {
  const base = controlla(r);
  const url = new URL(BACKUP_PATH, base);
  if (r.giorno !== undefined) url.searchParams.set("giorno", r.giorno);
  const res = await leggi(f, url, headers);
  const giorno = res.headers.get("X-Cuore-Backup-Giorno") ?? "";
  if (!giornoValido(giorno)) throw new Error("risposta senza il giorno del backup");
  mkdirSync(r.cartella, { recursive: true });
  const file = join(r.cartella, `cuore-finance-${giorno}.cuorebak.json`);
  const destinazione = join(r.cartella, `cuore-finance-${giorno}.ripristino.sqlite`);
  for (const p of [file, destinazione])
    if (existsSync(p)) throw new Error(`gia' presente, non sovrascritto: ${p}`);
  writeFileSync(file, await res.text(), { mode: 0o600 });
  const report = await ripristina({
    file,
    destinazione,
    chiavePrivata: r.chiavePrivata,
    chiaveFirma: r.chiaveFirma,
    versioneSchema: r.versioneSchema ?? VERSIONE_MOTORE,
  });
  return { giorno, file, report };
}

if (import.meta.main) {
  const { values } = parseArgs({
    options: {
      url: { type: "string" },
      cartella: { type: "string" },
      "chiave-privata": { type: "string" },
      "chiave-firma": { type: "string" },
      giorno: { type: "string" },
      elenco: { type: "boolean", default: false },
    },
  });
  const id = process.env.CF_ACCESS_CLIENT_ID ?? "";
  const secret = process.env.CF_ACCESS_CLIENT_SECRET ?? "";
  const headers = { "CF-Access-Client-Id": id, "CF-Access-Client-Secret": secret };
  const fermo = (m: string) => {
    console.error(`ritiro fermato: ${m}`);
    process.exitCode = 1;
  };
  if (!values.url) fermo("serve --url https://<indirizzo del Worker>");
  else if (id === "" || secret === "")
    fermo("mancano CF_ACCESS_CLIENT_ID e CF_ACCESS_CLIENT_SECRET (token mac-backup)");
  else if (values.elenco)
    elenco({ url: values.url }, headers, fetch)
      .then((c) => console.log(JSON.stringify(c, null, 2)))
      .catch((e) => fermo(e instanceof Error ? e.message : String(e)));
  else if (!values.cartella) fermo("serve --cartella <cartella fuori da iCloud e dal repository>");
  else if (!values["chiave-privata"] !== !values["chiave-firma"])
    fermo("--chiave-privata e --chiave-firma vanno insieme (oppure nessuna delle due: Portachiavi)");
  else
    Promise.resolve()
      .then(() =>
        values["chiave-privata"] && values["chiave-firma"]
          ? { chiavePrivata: values["chiave-privata"], chiaveFirma: values["chiave-firma"] }
          : chiaviRitiro(portachiaviMac),
      )
      .then((chiavi) =>
        ritira(
          {
            url: values.url as string,
            cartella: values.cartella as string,
            ...chiavi,
            ...(values.giorno === undefined ? {} : { giorno: values.giorno }),
          },
          headers,
          fetch,
        ),
      )
      .then(({ giorno, report }) => {
        console.log(JSON.stringify({ giorno, ...report }, null, 2));
        if (report.integrityCheck !== "ok" || !report.improntaCoerente || !report.schemaCoerente)
          process.exitCode = 1;
      })
      .catch((e) => fermo(e instanceof Error ? e.message : String(e)));
}
