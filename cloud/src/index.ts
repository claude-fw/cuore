/**
 * Ingresso di PRODUZIONE della vetrina CUORE (wrangler.jsonc). Nessuna modalita' dimostrativa dell'accesso qui: il
 * token di Cloudflare Access si verifica sempre con le chiavi pubbliche del team. Senza ACCESS_TEAM_DOMAIN, ACCESS_AUD e
 * ACCESS_ALLOWED_EMAILS validi il Worker rifiuta ogni richiesta (503). Non pubblicato: il deploy richiede un'autorizzazione.
 */
import page from "../../app/index.html";
import { type KeySource, parseAccessConfig, parseServiceIds, teamKeySource } from "./access.ts";
import { financeName, handle, runCron, writesOn } from "./app.ts";
import { ASSETS } from "./assets.ts";
import type { BackupBucket } from "./backup-cloud.ts";

import { origineMac } from "./migrazione.ts";

export { FinanceVetrina } from "./finance-do.ts";
export { SchoolFeed } from "./school-do.ts";

interface Env {
  FINANCE: DurableObjectNamespace;
  SCHOOL?: DurableObjectNamespace;
  /** Client ID del token di servizio `mac-school` (non segreto); assente = invio del feed School chiuso */
  ACCESS_SERVIZIO_SCHOOL?: string;
  /** M3: Client ID del token di servizio della migrazione (non segreto); assente = migrazione chiusa (503) */
  ACCESS_SERVIZIO_MIGRAZIONE?: string;
  /** L1: Client ID del token di servizio `mac-backup` (non segreto); assente = ritiro dei backup chiuso (503) */
  ACCESS_SERVIZIO_BACKUP?: string;
  /** L1: bucket R2 dei backup conservati (7 copie); commentato in wrangler.jsonc finche' non autorizzato */
  BACKUP?: BackupBucket;
  /** M3: "mac" = Durable Object "finance" che aspetta i dati del Mac; assente = vetrina sintetica */
  CUORE_FINANCE_ORIGINE?: string;
  ACCESS_TEAM_DOMAIN?: string;
  ACCESS_AUD?: string;
  ACCESS_ALLOWED_EMAILS?: string;
  CUORE_STATO?: string;
  CUORE_SCRITTURE?: string;
  CATTURA_TOKEN_SHA256?: string;
}

const keySources = new Map<string, KeySource>();
const schoolStub = (ns: DurableObjectNamespace) => () => ns.get(ns.idFromName("school"));

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const { config } = parseAccessConfig(env);
    let keys: KeySource | null = null;
    if (config !== null) {
      keys = keySources.get(config.teamDomain) ?? teamKeySource(config.teamDomain, (url) => fetch(url));
      keySources.set(config.teamDomain, keys);
    }
    return handle(request, {
      config,
      keys,
      nowMs: Date.now,
      page,
      assets: ASSETS,
      finance: () => env.FINANCE.get(env.FINANCE.idFromName(financeName(env.CUORE_FINANCE_ORIGINE))),
      state: env.CUORE_STATO,
      writes: writesOn(env.CUORE_SCRITTURE),
      catturaTokenHash: env.CATTURA_TOKEN_SHA256,
      ...(env.SCHOOL === undefined ? {} : { school: schoolStub(env.SCHOOL) }),
      schoolServiceIds: parseServiceIds(env.ACCESS_SERVIZIO_SCHOOL),
      migrazioneServiceIds: parseServiceIds(env.ACCESS_SERVIZIO_MIGRAZIONE),
      origineMac: origineMac(env.CUORE_FINANCE_ORIGINE),
      ...(env.BACKUP === undefined ? {} : { backupBucket: env.BACKUP }),
      backupServiceIds: parseServiceIds(env.ACCESS_SERVIZIO_BACKUP),
    });
  },
  async scheduled(controller: { scheduledTime: number; cron?: string }, env: Env): Promise<void> {
    await runCron(
      env.CUORE_STATO,
      env.FINANCE.get(env.FINANCE.idFromName(financeName(env.CUORE_FINANCE_ORIGINE))),
      controller.scheduledTime,
      controller.cron,
    );
  },
};
