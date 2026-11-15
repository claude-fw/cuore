#!/usr/bin/env node
/**
 * Controllo della configurazione di pubblicazione (`wrangler.jsonc`): dice in italiano semplice se e' tutto pronto o
 * cosa manca, e per ogni cosa CHI la fa («serve l'account» o «basta il titolare») e QUANDO (prima della pubblicazione
 * della vetrina, dopo e facoltativa, oppure solo prima dei dati veri). Nessuna rete, nessun account: legge solo i
 * file del repository. Dei segreti conosce e stampa solo i NOMI; i valori non passano mai da qui.
 *
 *   npm run configurazione
 *   npm run configurazione -- --segreti-creati ACCESS_ALLOWED_EMAILS   (dopo `wrangler secret put`, per segnarli)
 *   npm run configurazione -- --json
 * Controlla anche il banco di misura (`wrangler.misure.jsonc`): Worker separato, mai la vetrina.
 * Codice d'uscita: 0 = nessuna incoerenza nel file (anche se mancano passi); 1 = incoerenze da correggere nel codice.
 */
import { readFileSync } from "node:fs";
import { parseArgs } from "node:util";
import { parseServiceIds } from "../src/access.ts";

export type Chi = "serve l'account" | "basta il titolare";
export type Quando = "prima della pubblicazione" | "dopo, facoltativo" | "prima dei dati veri";

export interface Voce {
  readonly cosa: string;
  readonly chi: Chi;
  readonly quando: Quando;
}

export interface Esito {
  readonly pronto: boolean;
  /** incoerenze nel file o nel codice: vanno corrette prima di tutto */
  readonly errori: readonly string[];
  readonly daFare: readonly Voce[];
  readonly aPosto: readonly string[];
  /** nomi dei segreti da creare con `wrangler secret put` (mai i valori) */
  readonly segreti: readonly { nome: string; quando: Quando; creato: boolean }[];
}

/** JSON con commenti `//` e `/* *\/` e virgole finali (come lo legge wrangler); le stringhe restano intatte. */
export function parseJsonc(text: string): unknown {
  let out = "";
  let i = 0;
  while (i < text.length) {
    const c = text[i] as string;
    if (c === '"') {
      let j = i + 1;
      while (j < text.length && text[j] !== '"') j += text[j] === "\\" ? 2 : 1;
      out += text.slice(i, j + 1);
      i = j + 1;
    } else if (c === "/" && text[i + 1] === "/") {
      while (i < text.length && text[i] !== "\n") i++;
    } else if (c === "/" && text[i + 1] === "*") {
      const end = text.indexOf("*/", i + 2);
      i = end < 0 ? text.length : end + 2;
    } else {
      out += c;
      i++;
    }
  }
  return JSON.parse(out.replace(/,(\s*[}\]])/g, "$1"));
}

/** Classi esportate dall'ingresso di produzione (`export { X } from ...` o `export class X`). */
export function classiEsportate(sorgente: string): string[] {
  const out = new Set<string>();
  for (const m of sorgente.matchAll(/export\s*\{([^}]*)\}\s*from/g))
    for (const nome of (m[1] ?? "").split(","))
      out.add(
        nome
          .trim()
          .split(/\s+as\s+/)
          .pop() ?? "",
      );
  for (const m of sorgente.matchAll(/export\s+class\s+([A-Za-z0-9_]+)/g)) out.add(m[1] ?? "");
  out.delete("");
  return [...out];
}

// biome-ignore lint/suspicious/noExplicitAny: configurazione letta da JSONC
type Cfg = Record<string, any>;

/** Classi attese e loro migrazione: v1 Finance (C1), v2 School (E3). Solo backend SQLite (l'unico sul Free). */
const DO_ATTESI = [
  { binding: "FINANCE", classe: "FinanceVetrina", tag: "v1" },
  { binding: "SCHOOL", classe: "SchoolFeed", tag: "v2" },
] as const;

const TEAM = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com$/;
const AUD = /^[A-Za-z0-9]{16,128}$/;
const ACCOUNT = /^[0-9a-f]{32}$/;
const SEMBRA_SEGRETO = /PRIVATE|SECRET|PASSWORD|ALLOWED_EMAILS|^EB_PRIVATE|TOKEN(?!_SHA256)/;
const CRON = /^(\S+\s+){4}\S+$/;

export interface Ingresso {
  readonly config: Cfg;
  /** configurazione locale, per confrontare le migrazioni (stesse classi, stessi tag) */
  readonly locale?: Cfg;
  readonly classi: readonly string[];
  /** configurazione del banco di misura (`wrangler.misure.jsonc`), se presente */
  readonly misure?: Cfg;
  readonly oggi: string; // AAAA-MM-GG
  readonly segretiCreati?: readonly string[];
}

export function controllaConfigurazione(inp: Ingresso): Esito {
  const c = inp.config;
  const errori: string[] = [];
  const daFare: Voce[] = [];
  const aPosto: string[] = [];
  const vars: Record<string, unknown> = typeof c.vars === "object" && c.vars !== null ? c.vars : {};
  const v = (k: string) => (typeof vars[k] === "string" ? (vars[k] as string).trim() : "");
  const creati = new Set(inp.segretiCreati ?? []);
  const fai = (cosa: string, chi: Chi, quando: Quando) => daFare.push({ cosa, chi, quando });

  // --- Worker
  if (c.name !== "cuore") errori.push(`nome del Worker «${c.name}»: atteso «cuore»`);
  if (c.main !== "src/index.ts")
    errori.push(`main = «${c.main}»: in produzione solo src/index.ts (mai l'ingresso locale)`);
  else aPosto.push("Ingresso di produzione src/index.ts (l'accesso simulato non c'e')");
  if (c.workers_dev !== true)
    errori.push("workers_dev deve essere true (indirizzo gratuito cuore.<sottodominio>.workers.dev)");
  if (c.preview_urls !== false)
    errori.push("preview_urls deve essere false (nessun indirizzo di anteprima in piu')");
  if (c.observability?.enabled !== false)
    errori.push("observability deve restare spenta (nessun registro con dati, §4.7)");
  if (c.route !== undefined || c.routes !== undefined)
    errori.push("route/routes presenti: un dominio proprio costa ed e' escluso (decisione §7.4)");
  if (typeof c.compatibility_date !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(c.compatibility_date))
    errori.push("compatibility_date mancante");
  else if (c.compatibility_date > inp.oggi)
    errori.push(`compatibility_date ${c.compatibility_date} nel futuro: la pubblicazione verrebbe rifiutata`);
  else aPosto.push(`compatibility_date ${c.compatibility_date}`);
  if (Array.isArray(c.compatibility_flags) && c.compatibility_flags.length > 0)
    errori.push(`compatibility_flags non previsti: ${c.compatibility_flags.join(", ")}`);

  // --- Account (del genitore, C-02)
  if (c.account_id === undefined || c.account_id === "") {
    fai(
      "Aprire l'account Cloudflare a suo nome (piano Free), attivare la verifica in due passaggi e invitare il titolare come «Administrator»",
      "serve l'account",
      "prima della pubblicazione",
    );
    fai(
      "Copiare l'ID dell'account (pannello Cloudflare, colonna di destra; non e' un segreto) in `account_id` di wrangler.jsonc",
      "basta il titolare",
      "prima della pubblicazione",
    );
  } else if (typeof c.account_id !== "string" || !ACCOUNT.test(c.account_id))
    errori.push("account_id non valido (32 caratteri esadecimali)");
  else aPosto.push("ID dell'account presente");

  // --- Access (Zero Trust)
  const team = v("ACCESS_TEAM_DOMAIN");
  if (team === "") {
    fai(
      "Attivare Zero Trust (piano Free, 0 €) e scegliere il nome del team: Cloudflare chiede un metodo di pagamento anche per il Free",
      "serve l'account",
      "prima della pubblicazione",
    );
    fai(
      "Scrivere `<team>.cloudflareaccess.com` in vars.ACCESS_TEAM_DOMAIN",
      "basta il titolare",
      "prima della pubblicazione",
    );
  } else if (!TEAM.test(team.toLowerCase()))
    errori.push("ACCESS_TEAM_DOMAIN non valido (<team>.cloudflareaccess.com)");
  else aPosto.push("Nome del team di Access presente");
  const aud = v("ACCESS_AUD");
  if (aud === "")
    fai(
      "Creare in Zero Trust il login con GitHub e l'applicazione Access del Worker (solo la tua email), poi copiare il suo AUD in vars.ACCESS_AUD",
      "basta il titolare",
      "prima della pubblicazione",
    );
  else if (!AUD.test(aud)) errori.push("ACCESS_AUD non valido");
  else aPosto.push("AUD dell'applicazione Access presente");

  // --- Variabili che sembrano segreti: mai in vars
  for (const k of Object.keys(vars))
    if (SEMBRA_SEGRETO.test(k))
      errori.push(`${k} e' in vars (testo in chiaro nel repository): va creato come segreto`);

  // --- Interruttori
  const stato = v("CUORE_STATO").toLowerCase();
  if (stato === "attivo") aPosto.push("Interruttore CUORE_STATO = attivo (spento = tutto 503)");
  else
    fai(
      'Impostare vars.CUORE_STATO = "attivo" quando si vuole aprire la vetrina',
      "basta il titolare",
      "prima della pubblicazione",
    );
  const origine = v("CUORE_FINANCE_ORIGINE").toLowerCase();
  const scritture = v("CUORE_SCRITTURE").toLowerCase() === "attive";
  if (origine === "mac")
    aPosto.push(
      "CUORE_FINANCE_ORIGINE = mac: Durable Object dei dati veri (M3), pagina «online · dati reali» e API del ponte",
    );
  else if (origine !== "")
    errori.push(`CUORE_FINANCE_ORIGINE «${origine}» sconosciuto (vuoto = vetrina, "mac" = M3)`);
  if (scritture && origine !== "mac")
    errori.push(
      "CUORE_SCRITTURE = attive senza CUORE_FINANCE_ORIGINE = mac: in produzione le modifiche si aprono solo sui dati veri, con autorizzazione",
    );
  else if (scritture) aPosto.push("Modifiche dal telefono aperte (CUORE_SCRITTURE = attive) sui dati veri");
  else if (origine === "mac")
    fai(
      'Modifiche dal telefono (spese, contanti, correzioni, categorie, budget, note School): vars.CUORE_SCRITTURE = "attive"; senza, CUORE online resta in sola lettura',
      "basta il titolare",
      "prima dei dati veri",
    );
  const migra = v("ACCESS_SERVIZIO_MIGRAZIONE");
  if (migra !== "" && parseServiceIds(migra).length === 0)
    errori.push("ACCESS_SERVIZIO_MIGRAZIONE non valido");
  if (migra !== "" && origine !== "mac")
    errori.push(
      "ACCESS_SERVIZIO_MIGRAZIONE senza CUORE_FINANCE_ORIGINE = mac: togliere (la rotta resterebbe chiusa)",
    );
  if (migra !== "" && origine === "mac")
    fai(
      "Dopo l'invio M3 riuscito: eliminare il token `mac-migrazione` in Zero Trust e togliere vars.ACCESS_SERVIZIO_MIGRAZIONE (la rotta torna chiusa)",
      "basta il titolare",
      "prima dei dati veri",
    );
  if (migra === "")
    fai(
      'M3, solo con nuova autorizzazione: token di servizio temporaneo `mac-migrazione`, il suo Client ID in vars.ACCESS_SERVIZIO_MIGRAZIONE e CUORE_FINANCE_ORIGINE = "mac"; dopo l\'invio si elimina il token',
      "basta il titolare",
      "prima dei dati veri",
    );
  const r2: unknown = c.r2_buckets;
  const backupId = v("ACCESS_SERVIZIO_BACKUP");
  if (backupId !== "" && parseServiceIds(backupId).length === 0)
    errori.push("ACCESS_SERVIZIO_BACKUP non valido");
  if (backupId !== "" && r2 === undefined)
    errori.push("ACCESS_SERVIZIO_BACKUP senza bucket R2 BACKUP: la rotta di ritiro resterebbe chiusa");
  if (backupId === "")
    fai(
      "Ritiro dei backup dal Mac (L1): token di servizio `mac-backup` in Zero Trust, ammesso solo su GET /api/finance/backup; il suo Client ID in vars.ACCESS_SERVIZIO_BACKUP, il Client Secret nel Portachiavi del Mac",
      "basta il titolare",
      "prima dei dati veri",
    );
  else if (r2 !== undefined) aPosto.push("Token di servizio del Mac per il ritiro dei backup configurato");
  const school = v("ACCESS_SERVIZIO_SCHOOL");
  if (school === "")
    fai(
      "School dal Mac (E3): token di servizio `mac-school` in Zero Trust e il suo Client ID in vars.ACCESS_SERVIZIO_SCHOOL",
      "basta il titolare",
      "dopo, facoltativo",
    );
  else if (parseServiceIds(school).length === 0) errori.push("ACCESS_SERVIZIO_SCHOOL non valido");
  else aPosto.push("Token di servizio del Mac per School configurato");

  // --- Segreti (solo nomi)
  const segreti: Esito["segreti"][number][] = [
    {
      nome: "ACCESS_ALLOWED_EMAILS",
      quando: "prima della pubblicazione",
      creato: creati.has("ACCESS_ALLOWED_EMAILS"),
    },
    { nome: "CATTURA_TOKEN_SHA256", quando: "dopo, facoltativo", creato: creati.has("CATTURA_TOKEN_SHA256") },
    { nome: "VAPID_PRIVATE_JWK", quando: "dopo, facoltativo", creato: creati.has("VAPID_PRIVATE_JWK") },
    {
      nome: "BACKUP_SIGNING_PRIVATE_JWK",
      quando: "prima dei dati veri",
      creato: creati.has("BACKUP_SIGNING_PRIVATE_JWK"),
    },
  ];
  for (const s of segreti)
    if (s.creato) aPosto.push(`Segreto ${s.nome} creato (dichiarato con --segreti-creati)`);
    else if (s.quando === "prima della pubblicazione")
      fai(
        `Creare il segreto ${s.nome} (la tua email di GitHub) con \`wrangler secret put ${s.nome}\``,
        "basta il titolare",
        s.quando,
      );
  const vpub = v("VAPID_PUBLIC_KEY");
  if (vpub !== "" && !/^B[A-Za-z0-9_-]{86}$/.test(vpub))
    errori.push("VAPID_PUBLIC_KEY non valida (chiave pubblica P-256 base64url di `npm run vapid`)");
  const vsub = v("VAPID_SUBJECT");
  if (vsub !== "" && !/^(mailto:|https:\/\/)\S+$/.test(vsub))
    errori.push("VAPID_SUBJECT: mailto: oppure https://");
  if (vpub !== "" && vsub !== "" && creati.has("VAPID_PRIVATE_JWK"))
    aPosto.push("Notifiche push configurate (VAPID_PUBLIC_KEY, VAPID_SUBJECT, segreto VAPID_PRIVATE_JWK)");
  else if (vpub !== "" || vsub !== "")
    fai(
      "Notifiche push incomplete: servono insieme vars.VAPID_PUBLIC_KEY, vars.VAPID_SUBJECT e il segreto VAPID_PRIVATE_JWK (`npm run vapid`)",
      "basta il titolare",
      "dopo, facoltativo",
    );
  if (!creati.has("VAPID_PRIVATE_JWK") && v("VAPID_SUBJECT") === "" && vpub === "")
    fai(
      "Notifiche push: `npm run vapid`, poi segreto VAPID_PRIVATE_JWK, vars.VAPID_PUBLIC_KEY e vars.VAPID_SUBJECT; senza, nessuna notifica parte",
      "basta il titolare",
      "dopo, facoltativo",
    );
  if (!creati.has("CATTURA_TOKEN_SHA256"))
    fai(
      "Cattura Apple Pay: impronta del token del Comando Rapido come segreto CATTURA_TOKEN_SHA256; senza, /cattura resta chiusa",
      "basta il titolare",
      "dopo, facoltativo",
    );

  // --- Durable Objects e migrazioni
  const bindings: Cfg[] = Array.isArray(c.durable_objects?.bindings) ? c.durable_objects.bindings : [];
  const migrazioni: Cfg[] = Array.isArray(c.migrations) ? c.migrations : [];
  const tags = migrazioni.map((m) => m.tag);
  if (new Set(tags).size !== tags.length) errori.push("tag delle migrazioni ripetuti");
  const nuove = new Map<string, string>();
  for (const m of migrazioni) {
    if (Array.isArray(m.new_classes) && m.new_classes.length > 0)
      errori.push(
        `migrazione ${m.tag}: new_classes (archivio chiave-valore) non e' disponibile sul Free: usare new_sqlite_classes`,
      );
    for (const k of ["deleted_classes", "renamed_classes", "transferred_classes"])
      if (m[k] !== undefined)
        errori.push(`migrazione ${m.tag}: ${k} cancellerebbe o sposterebbe dati: non prevista`);
    for (const cl of Array.isArray(m.new_sqlite_classes) ? m.new_sqlite_classes : []) nuove.set(cl, m.tag);
  }
  for (const a of DO_ATTESI) {
    const b = bindings.find((x) => x.name === a.binding);
    if (b === undefined) errori.push(`binding ${a.binding} mancante`);
    else if (b.class_name !== a.classe)
      errori.push(`binding ${a.binding} → ${b.class_name}: atteso ${a.classe}`);
    if (nuove.get(a.classe) !== a.tag)
      errori.push(`classe ${a.classe}: attesa nella migrazione ${a.tag} come new_sqlite_classes`);
    if (!inp.classi.includes(a.classe)) errori.push(`classe ${a.classe} non esportata da src/index.ts`);
  }
  for (const b of bindings)
    if (!DO_ATTESI.some((a) => a.binding === b.name))
      errori.push(`binding Durable Object non previsto: ${b.name}`);
  if (tags.join(",") !== DO_ATTESI.map((a) => a.tag).join(","))
    errori.push(
      `migrazioni ${tags.join(", ") || "assenti"}: attese v1 (Finance) e v2 (School), in quest'ordine`,
    );
  if (inp.locale !== undefined && JSON.stringify(inp.locale.migrations) !== JSON.stringify(c.migrations))
    errori.push(
      "migrazioni diverse fra wrangler.jsonc e wrangler.locale.jsonc: le prove locali non varrebbero",
    );
  if (!errori.some((e) => /migrazion|binding|classe/.test(e)))
    aPosto.push(
      "Durable Object coerenti: v1 FinanceVetrina, v2 SchoolFeed (SQLite), esportati da src/index.ts",
    );

  // --- Cron, code, R2, altri servizi
  const crons: unknown[] = Array.isArray(c.triggers?.crons) ? c.triggers.crons : [];
  if (crons.length === 0)
    fai(
      'Automazioni (C3): aggiungere il Cron "0 5,11,17,21 * * *" (1 dei 5 dell\'account, orari UTC); senza, coda e notifiche restano ferme',
      "basta il titolare",
      "dopo, facoltativo",
    );
  else if (crons.length > 5 || !crons.every((x) => typeof x === "string" && CRON.test(x)))
    errori.push("Cron non validi o piu' di 5 (limite dell'account)");
  else aPosto.push(`Cron: ${crons.join(", ")}`);
  if (c.queues !== undefined)
    errori.push(
      "queues presenti: il codice non usa code Cloudflare (la coda e' la tabella cloud_coda del Durable Object)",
    );
  else
    aPosto.push(
      "Nessuna coda Cloudflare da creare: la coda e' dentro il Durable Object (cloud_coda + allarmi)",
    );
  for (const k of ["kv_namespaces", "d1_databases", "services", "ai", "browser", "vectorize", "hyperdrive"])
    if (c[k] !== undefined) errori.push(`${k} presente: servizio non previsto dal disegno`);
  if (r2 === undefined)
    fai(
      "Backup conservati nel cloud (7 copie, decisione L1 del 03/11; codice pronto): attivare R2 sull'account (puo' chiedere un metodo di pagamento anche per la quota gratuita [da verificare]), creare il bucket `cuore-backup`, togliere il commento al binding BACKUP in wrangler.jsonc e aggiungere il Cron delle automazioni (senza, nessuna copia giornaliera)",
      "serve l'account",
      "prima dei dati veri",
    );
  else if (
    !Array.isArray(r2) ||
    r2.length !== 1 ||
    r2[0]?.binding !== "BACKUP" ||
    typeof r2[0]?.bucket_name !== "string" ||
    r2[0].bucket_name === ""
  )
    errori.push("r2_buckets: atteso un solo binding BACKUP con bucket_name (backup L1)");
  else if (crons.length === 0)
    errori.push("r2_buckets senza Cron: la copia giornaliera dei backup non partirebbe mai");
  else
    aPosto.push(
      `Bucket R2 dei backup collegato (BACKUP → ${r2[0].bucket_name}): una copia al giorno, ultime 7`,
    );
  for (const k of ["BACKUP_MAC_PUBLIC_JWK", "BACKUP_RECOVERY_PUBLIC_JWK"]) {
    if (v(k) === "") continue;
    let chiave: Cfg | null = null;
    try {
      chiave = JSON.parse(v(k)) as Cfg;
    } catch {}
    if (chiave === null || chiave.kty !== "RSA")
      errori.push(`${k} non e' una chiave pubblica RSA in JSON (JWK)`);
    else if ("d" in chiave || "p" in chiave)
      errori.push(`${k} contiene una chiave PRIVATA: toglierla subito dal file e rigenerare le chiavi`);
  }
  if (
    !creati.has("BACKUP_SIGNING_PRIVATE_JWK") ||
    v("BACKUP_MAC_PUBLIC_JWK") === "" ||
    v("BACKUP_RECOVERY_PUBLIC_JWK") === ""
  )
    fai(
      "Chiavi dei backup: `npm run chiavi-mac -- --recupero-su <chiavetta>` (privata del Mac nel Portachiavi), le due pubbliche stampate in vars.BACKUP_MAC_PUBLIC_JWK e vars.BACKUP_RECOVERY_PUBLIC_JWK, la firma come segreto BACKUP_SIGNING_PRIVATE_JWK dal Portachiavi; senza, nessuna copia viene scritta",
      "basta il titolare",
      "prima dei dati veri",
    );

  // --- Banco di misura (Worker separato, facoltativo)
  const m = inp.misure;
  if (m !== undefined) {
    const mv: Record<string, unknown> = typeof m.vars === "object" && m.vars !== null ? m.vars : {};
    const mb: Cfg[] = Array.isArray(m.durable_objects?.bindings) ? m.durable_objects.bindings : [];
    if (m.name === c.name || m.name === "cuore")
      errori.push("banco di misura: stesso nome della vetrina, la sostituirebbe: serve un Worker separato");
    if (m.main !== "src/misure.ts") errori.push("banco di misura: main deve essere src/misure.ts");
    if (mb.length !== 1 || mb[0]?.name !== "BANCO" || mb[0]?.class_name !== "BancoMisure")
      errori.push("banco di misura: solo il Durable Object BANCO (BancoMisure), mai quelli della vetrina");
    if (typeof mv.MISURE_TOKEN_SHA256 === "string" && mv.MISURE_TOKEN_SHA256.trim() !== "")
      errori.push("banco di misura: MISURE_TOKEN_SHA256 nel file; si passa solo al deploy con --var");
    if (m.observability?.enabled !== false) errori.push("banco di misura: observability deve restare spenta");
    for (const k of ["triggers", "r2_buckets", "routes", "route"])
      if (m[k] !== undefined) errori.push(`banco di misura: ${k} non previsto`);
    if (m.account_id !== c.account_id)
      errori.push("banco di misura: account_id diverso da quello della vetrina");
    fai(
      "Banco di misura da 5000 movimenti sintetici (C-01): `wrangler deploy -c wrangler.misure.jsonc --var MISURE_TOKEN_SHA256:<impronta>`, poi `npm run misure -- --url https://cuore-misure-sintetiche.<sottodominio>.workers.dev --pitr` e `wrangler delete -c wrangler.misure.jsonc`",
      "basta il titolare",
      "dopo, facoltativo",
    );
  }

  fai(
    "Solo se le misure della vetrina lo dimostrano (C-01): Workers Paid, ~5 $/mese; lo attiva il titolare dell'account dalla fatturazione, con nuova approvazione",
    "serve l'account",
    "dopo, facoltativo",
  );

  const pronto = errori.length === 0 && !daFare.some((d) => d.quando === "prima della pubblicazione");
  return { pronto, errori, daFare, aPosto, segreti };
}

export function testo(e: Esito): string {
  const blocco = (q: Quando) => e.daFare.filter((d) => d.quando === q);
  const elenco = (vv: Voce[]) => vv.map((d, i) => `  ${i + 1}. [${d.chi}] ${d.cosa}`);
  const prima = blocco("prima della pubblicazione");
  return [
    "Controllo della configurazione di pubblicazione (wrangler.jsonc) — nessuna rete, nessun valore segreto letto",
    e.errori.length > 0
      ? `Stato: DA CORREGGERE — ${e.errori.length} incoerenze nel file`
      : e.pronto
        ? "Stato: PRONTO per la prima pubblicazione della vetrina (dati inventati)"
        : `Stato: NON ANCORA PRONTO — ${prima.length} cose da fare prima della pubblicazione`,
    ...(e.errori.length > 0
      ? ["", "Incoerenze (da correggere nel codice):", ...e.errori.map((x) => `  - ${x}`)]
      : []),
    "",
    "Gia' a posto:",
    ...e.aPosto.map((x) => `  - ${x}`),
    ...(prima.length > 0 ? ["", "Da fare prima della prima pubblicazione:", ...elenco(prima)] : []),
    "",
    "Dopo, facoltativo:",
    ...elenco(blocco("dopo, facoltativo")),
    "",
    "Solo prima dei dati veri (M3, con nuova autorizzazione):",
    ...elenco(blocco("prima dei dati veri")),
    "",
    `Segreti da creare (solo i nomi): ${e.segreti.map((s) => `${s.nome}${s.creato ? " (fatto)" : ` (${s.quando})`}`).join(", ")}`,
  ].join("\n");
}

if (import.meta.main) {
  const { values } = parseArgs({
    options: { json: { type: "boolean", default: false }, "segreti-creati": { type: "string" } },
  });
  const leggi = (f: string) => readFileSync(new URL(`../${f}`, import.meta.url), "utf8");
  const esito = controllaConfigurazione({
    config: parseJsonc(leggi("wrangler.jsonc")) as Cfg,
    locale: parseJsonc(leggi("wrangler.locale.jsonc")) as Cfg,
    misure: parseJsonc(leggi("wrangler.misure.jsonc")) as Cfg,
    classi: classiEsportate(leggi("src/index.ts")),
    oggi: new Date().toISOString().slice(0, 10),
    segretiCreati: (values["segreti-creati"] ?? "")
      .split(",")
      .map((s) => s.trim())
      .filter((s) => s !== ""),
  });
  console.log(values.json ? JSON.stringify(esito, null, 2) : testo(esito));
  process.exitCode = esito.errori.length === 0 ? 0 : 1;
}
