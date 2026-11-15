#!/usr/bin/env node
/**
 * L1 — chiavi VERE dei backup cifrati, generate sul Mac e LANCIATE SOLO DAL TITOLARE (mai da Claude):
 *
 *   npm run chiavi-mac -- --recupero-su <file su chiavetta, fuori da iCloud e dal repository>
 *   npm run chiavi-mac -- --recupero-stampa        (la privata di recupero in chiaro sul Terminale, da stampare)
 *   npm run chiavi-mac -- --controlla              (solo: le voci ci sono? stampa i kid, nessuna chiave privata)
 *
 * Crea tre coppie con Web Crypto (le stesse di `src/backup.ts`):
 *   - Mac (RSA-OAEP 3072, SHA-256): privata nel Portachiavi `cuore.backup.mac`; pubblica → `BACKUP_MAC_PUBLIC_JWK`;
 *   - recupero offline (RSA-OAEP 3072): pubblica → `BACKUP_RECOVERY_PUBLIC_JWK`; privata SOLO fuori dal Mac (chiavetta
 *     o carta, a scelta: senza una delle due opzioni lo script si ferma), mai nel Portachiavi;
 *   - firma (ECDSA P-256): privata nel Portachiavi `cuore.backup.firma-privata` solo per passarla al segreto del Worker
 *     senza vederla (`security find-generic-password -a cuore -s cuore.backup.firma-privata -w | npm run -s chiavi-mac --
 *     --esporta-firma | wrangler secret put BACKUP_SIGNING_PRIVATE_JWK`), poi si puo' togliere; pubblica nel Portachiavi
 *     `cuore.backup.firma` (serve al ritiro per verificare la firma).
 * Stampa solo cio' che NON e' segreto: i `kid` e le due chiavi pubbliche da mettere in `wrangler.jsonc` (`vars`).
 * Nel Portachiavi i valori sono JSON in base64url, scritti con `security -i` dallo standard input (mai sulla riga di
 * comando, dove `ps` li vedrebbe). Voci gia' presenti = fermo: una chiave esistente non si sovrascrive mai.
 */
import { execFileSync, spawnSync } from "node:child_process";
import type { webcrypto } from "node:crypto";
import { existsSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { parseArgs } from "node:util";
import { recipientKid } from "../src/backup.ts";

export const ACCOUNT = "cuore";
export const SERVIZI = {
  mac: "cuore.backup.mac",
  firma: "cuore.backup.firma",
  firmaPrivata: "cuore.backup.firma-privata",
} as const;

/** Portachiavi: quello vero (`security`) o uno finto nei test. `leggi` = null se la voce non c'e'. */
export interface Portachiavi {
  leggi(servizio: string): string | null;
  aggiungi(servizio: string, valore: string): void;
}

const b64 = (s: string) => Buffer.from(s, "utf8").toString("base64url");
const unb64 = (s: string) => Buffer.from(s.trim(), "base64url").toString("utf8");

/** Riga di `security -i` per una voce nuova: solo caratteri base64url e nomi fissi, nessuna virgoletta da sfuggire. */
export function rigaAggiungi(servizio: string, valore: string): string {
  if (!/^[a-z.-]+$/.test(servizio)) throw new Error("servizio non valido");
  return `add-generic-password -a ${ACCOUNT} -s ${servizio} -l ${servizio} -w ${b64(valore)}\n`;
}

export const portachiaviMac: Portachiavi = {
  leggi(servizio) {
    const r = spawnSync("/usr/bin/security", ["find-generic-password", "-a", ACCOUNT, "-s", servizio, "-w"], {
      encoding: "utf8",
    });
    if (r.status === 44) return null; // voce assente
    if (r.status !== 0) throw new Error(`Portachiavi: lettura di ${servizio} non riuscita (${r.status})`);
    return unb64(r.stdout);
  },
  aggiungi(servizio, valore) {
    execFileSync("/usr/bin/security", ["-i"], {
      input: rigaAggiungi(servizio, valore),
      stdio: ["pipe", "ignore", "pipe"],
    });
    if (portachiaviMac.leggi(servizio) !== valore) throw new Error(`Portachiavi: ${servizio} non scritto`);
  },
};

const RSA = {
  name: "RSA-OAEP",
  modulusLength: 3072,
  publicExponent: new Uint8Array([1, 0, 1]),
  hash: "SHA-256",
};
const jwk = async (k: webcrypto.CryptoKey) => JSON.stringify(await crypto.subtle.exportKey("jwk", k));
const coppia = (alg: object, usi: string[]) =>
  crypto.subtle.generateKey(alg as never, true, usi as never) as Promise<webcrypto.CryptoKeyPair>;

export interface Esito {
  /** NON segreti: vanno in `wrangler.jsonc` (`vars`) e nel libro di bordo */
  pubbliche: { BACKUP_MAC_PUBLIC_JWK: string; BACKUP_RECOVERY_PUBLIC_JWK: string };
  kid: { mac: string; recupero: string };
  /** privata di recupero: restituita SOLO al chiamante, che la scrive sul file o la stampa (mai nel Portachiavi) */
  recuperoPrivata: string;
}

/** Genera le tre coppie e salva le voci del Mac. Fermo se una voce esiste gia'. */
export async function generaChiavi(pc: Portachiavi): Promise<Esito> {
  for (const s of Object.values(SERVIZI))
    if (pc.leggi(s) !== null)
      throw new Error(`voce gia' presente nel Portachiavi: ${s} (nessuna chiave sovrascritta)`);
  const [mac, recupero, firma] = await Promise.all([
    coppia(RSA, ["encrypt", "decrypt"]),
    coppia(RSA, ["encrypt", "decrypt"]),
    coppia({ name: "ECDSA", namedCurve: "P-256" }, ["sign", "verify"]),
  ]);
  pc.aggiungi(SERVIZI.mac, await jwk(mac.privateKey));
  pc.aggiungi(SERVIZI.firma, await jwk(firma.publicKey));
  pc.aggiungi(SERVIZI.firmaPrivata, await jwk(firma.privateKey));
  return {
    pubbliche: {
      BACKUP_MAC_PUBLIC_JWK: await jwk(mac.publicKey),
      BACKUP_RECOVERY_PUBLIC_JWK: await jwk(recupero.publicKey),
    },
    kid: { mac: await recipientKid(mac.publicKey), recupero: await recipientKid(recupero.publicKey) },
    recuperoPrivata: await jwk(recupero.privateKey),
  };
}

/** Chiavi del ritiro lette dal Portachiavi: privata del Mac e pubblica di firma (JWK). */
export function chiaviRitiro(pc: Portachiavi): {
  chiavePrivata: webcrypto.JsonWebKey;
  chiaveFirma: webcrypto.JsonWebKey;
} {
  const mac = pc.leggi(SERVIZI.mac);
  const firma = pc.leggi(SERVIZI.firma);
  if (mac === null || firma === null)
    throw new Error(
      `chiavi assenti nel Portachiavi (${SERVIZI.mac}, ${SERVIZI.firma}): prima npm run chiavi-mac`,
    );
  return {
    chiavePrivata: JSON.parse(mac) as webcrypto.JsonWebKey,
    chiaveFirma: JSON.parse(firma) as webcrypto.JsonWebKey,
  };
}

/** Cartella del file di recupero: mai iCloud, mai il repository, mai un file esistente. */
export function controllaRecupero(file: string, repo: string): string {
  const p = resolve(file);
  if (p.includes("/Library/Mobile Documents/")) throw new Error("il file di recupero non va in iCloud");
  if (p.startsWith(`${resolve(repo)}/`)) throw new Error("il file di recupero non va nel repository");
  if (existsSync(p)) throw new Error(`gia' presente, non sovrascritto: ${p}`);
  if (!existsSync(dirname(p)))
    throw new Error(`cartella inesistente (chiavetta non collegata?): ${dirname(p)}`);
  return p;
}

if (import.meta.main) {
  const { values } = parseArgs({
    options: {
      "recupero-su": { type: "string" },
      "recupero-stampa": { type: "boolean", default: false },
      controlla: { type: "boolean", default: false },
      "esporta-firma": { type: "boolean", default: false },
    },
  });
  const repo = new URL("../..", import.meta.url).pathname;
  const fermo = (m: string) => {
    console.error(`chiavi-mac fermato: ${m}`);
    process.exitCode = 1;
  };
  if (values["esporta-firma"]) {
    // filtro: base64url (da `security ... -w`) → JWK sullo standard output, per `wrangler secret put` (mai a video)
    let dati = "";
    process.stdin.on("data", (c) => {
      dati += c;
    });
    process.stdin.on("end", () => process.stdout.write(unb64(dati)));
  } else if (values.controlla) {
    try {
      const { chiavePrivata, chiaveFirma } = chiaviRitiro(portachiaviMac);
      console.log(JSON.stringify({ mac: chiavePrivata.kty === "RSA", firma: chiaveFirma.kty === "EC" }));
    } catch (e) {
      fermo(e instanceof Error ? e.message : String(e));
    }
  } else if (!values["recupero-su"] && !values["recupero-stampa"]) {
    fermo("scegli dove va la chiave di recupero: --recupero-su <file su chiavetta> oppure --recupero-stampa");
  } else {
    try {
      const file = values["recupero-su"] ? controllaRecupero(values["recupero-su"], repo) : null;
      const e = await generaChiavi(portachiaviMac);
      if (file !== null) writeFileSync(file, e.recuperoPrivata, { mode: 0o600, flag: "wx" });
      else
        console.log(
          `\nCHIAVE DI RECUPERO (privata, da stampare e conservare offline):\n${e.recuperoPrivata}\n`,
        );
      console.log(JSON.stringify({ kid: e.kid, vars: e.pubbliche }, null, 2));
    } catch (err) {
      fermo(err instanceof Error ? err.message : String(err));
    }
  }
}
