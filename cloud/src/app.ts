/**
 * Porta d'ingresso della vetrina CUORE, comune all'ingresso di produzione (index.ts) e a quello locale (locale.ts).
 * Ordine dei controlli, tutti chiusi per difetto: interruttore → metodo → configurazione → richieste da altri siti →
 * identita' (token di Cloudflare Access verificato qui, non solo dalla rete) → rotta.
 * Rotte: `/` (la pagina di app/index.html con il segnale «vetrina»), `GET /api/vetrina/stato` (Durable Object Finance
 * con dati sintetici), `POST /api/finance/esegui` (API di scrittura C2 sugli stessi dati sintetici, chiusa per
 * difetto: si apre solo con `CUORE_SCRITTURE` = "attive"; in piu' Origin uguale al sito, JSON, al massimo 64 KiB) e
 * `POST /cattura` (C3, cattura Apple Pay dal Comando Rapido: NON passa da Cloudflare Access, che un Comando Rapido non
 * puo' soddisfare; verifica solo il token applicativo Bearer contro `CATTURA_TOKEN_SHA256`, schema chiuso, al massimo
 * 2 KiB, limite di frequenza esatto nel Durable Object). School (E3): `POST /api/school` riceve dal Mac l'ultimo feed
 * School, SOLO con il token di servizio di Access del Mac (`common_name` fra `ACCESS_SERVIZIO_SCHOOL`; senza = rotta
 * chiusa, 503), JSON, al massimo 1 MiB; `GET /api/school` lo serve al titolare (Durable Object School, separato da
 * Finance). M3 (solo provata su dati sintetici): `POST /api/finance/migrazione` riceve dal Mac i dati Finance a blocchi
 * (migrazione.ts), SOLO con un token di servizio fra `ACCESS_SERVIZIO_MIGRAZIONE` e con `CUORE_FINANCE_ORIGINE` =
 * "mac"; altrimenti 503. Backup L1 (backup-cloud.ts): `GET /api/finance/backup` (la copia piu' recente, oppure
 * `?giorno=AAAA-MM-GG`) e `GET /api/finance/backup/elenco` (solo giorni e dimensioni) SOLO con il token di servizio
 * `mac-backup` (`ACCESS_SERVIZIO_BACKUP`) e con il bucket `BACKUP` collegato; altrimenti 503.
 * Dati veri online (`CUORE_FINANCE_ORIGINE` = "mac", online.ts): la pagina con «online · dati reali» e le stesse API del
 * ponte del Mac (`/api/money`, `/api/search`, `/api/scritture`, `POST /api/scritture/esegui`, ...); senza "mac" nessuna
 * di queste rotte esiste e resta la vetrina.
 */
import { type AccessConfig, type KeySource, verifyAccessJwt, verifyServiceJwt } from "./access.ts";
import { BACKUP_PATH, type BackupBucket, elencoBackup, giornoValido, nomeBackup } from "./backup-cloud.ts";
import { CATTURA_MAX_BODY, checkToken } from "./cattura.ts";
import { MIGRAZIONE_MAX_BODY, MIGRAZIONE_PATH, origineMac } from "./migrazione.ts";
import { NOTIFICHE_PATH, ONLINE_WRITE_PATH, onlineGet, onlinePage, onlineWrite } from "./online.ts";
import { SCHOOL_MAX_BODY } from "./school.ts";

export const ACCESS_HEADER = "Cf-Access-Jwt-Assertion";
export const VETRINA_META = '<meta name="cuore-vetrina" content="1">';
export const WRITE_PATH = "/api/finance/esegui";
export const CATTURA_PATH = "/cattura";
export const SCHOOL_PATH = "/api/school";
export const MAX_BODY = 64 * 1024;

/**
 * Istanza del Durable Object Finance: "vetrina" (esempi sintetici) oppure, con `CUORE_FINANCE_ORIGINE` = "mac", un
 * database SEPARATO "finance" che nasce vuoto e aspetta i dati del Mac (M3): i due non si mescolano mai.
 */
export const financeName = (origine: string | undefined) => (origineMac(origine) ? "finance" : "vetrina");

/** Interruttore delle scritture (`CUORE_SCRITTURE`): solo "attive" apre, tutto il resto chiude. */
export const writesOn = (v: string | undefined) => v?.trim().toLowerCase() === "attive";

/** Turno del Cron (C3) → Durable Object; con l'interruttore `CUORE_STATO` chiuso non fa nulla. */
export async function runCron(
  state: string | undefined,
  finance: FinanceStub,
  scheduledMs: number,
  cron?: string,
): Promise<void> {
  if (state?.trim().toLowerCase() !== "attivo") return;
  const body = JSON.stringify({ ora: scheduledMs, ...(cron === undefined ? {} : { cron }) });
  const r = await finance.fetch(new Request("https://finance.interno/cron", { method: "POST", body }));
  if (!r.ok) throw new Error(`cron: Durable Object ${r.status}`);
}

/** Come il ponte locale (app/bridge/serve.py), con le intestazioni in piu' adatte a un servizio in rete. */
export const SECURITY_HEADERS: Readonly<Record<string, string>> = {
  "Cache-Control": "no-store",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
  "Content-Security-Policy":
    "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; manifest-src 'self'; worker-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
  "X-Frame-Options": "DENY",
  "Cross-Origin-Opener-Policy": "same-origin",
  "Cross-Origin-Resource-Policy": "same-origin",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
  "Strict-Transport-Security": "max-age=31536000",
};

export interface FinanceStub {
  fetch(request: Request): Promise<Response>;
}

export interface AppDeps {
  /** null = configurazione dell'accesso mancante o non valida: tutto rifiutato */
  readonly config: AccessConfig | null;
  readonly keys: KeySource | null;
  readonly nowMs: () => number;
  readonly page: string;
  readonly finance: () => FinanceStub;
  /**
   * variabile `CUORE_STATO` (interruttore): solo "attivo" apre; "spento", un valore
   * sbagliato o la variabile mancante chiudono tutto (chiuso per difetto)
   */
  readonly state: string | undefined;
  /** API di scrittura aperta (`writesOn(CUORE_SCRITTURE)`); assente = chiusa */
  readonly writes?: boolean;
  /** impronta SHA-256 del token applicativo di `/cattura` (`CATTURA_TOKEN_SHA256`); assente = rotta chiusa */
  readonly catturaTokenHash?: string;
  /** Durable Object School (E3): ultimo feed School inviato dal Mac; assente = rotte School non disponibili */
  readonly school?: () => FinanceStub;
  /** Client ID del token di servizio del Mac ammessi su `POST /api/school` (`parseServiceIds`); vuoto = rotta chiusa */
  readonly schoolServiceIds?: readonly string[];
  /** M3: Client ID ammessi su `POST /api/finance/migrazione` (`ACCESS_SERVIZIO_MIGRAZIONE`); vuoto = rotta chiusa */
  readonly migrazioneServiceIds?: readonly string[];
  /** M3: `CUORE_FINANCE_ORIGINE` = "mac" (il Durable Object aspetta i dati del Mac); falso = rotta chiusa */
  readonly origineMac?: boolean;
  /** L1: bucket R2 dei backup (`BACKUP`); assente = ritiro chiuso */
  readonly backupBucket?: BackupBucket;
  /** L1: Client ID ammessi sul ritiro dei backup (`ACCESS_SERVIZIO_BACKUP`, token `mac-backup`); vuoto = chiuso */
  readonly backupServiceIds?: readonly string[];
  /** file statici della PWA (service worker, push.js, manifest, icone), serviti dopo Access come la pagina */
  readonly assets?: Readonly<Record<string, { body: string | ArrayBuffer | Uint8Array; type: string }>>;
  /** solo l'ingresso locale: segnala sulla pagina che l'accesso e' simulato */
  readonly decoratePage?: (html: string) => string;
}

function respond(
  status: number,
  body: string | null,
  type: string,
  extra: Record<string, string> = {},
): Response {
  return new Response(body, { status, headers: { ...SECURITY_HEADERS, "Content-Type": type, ...extra } });
}

const json = (status: number, data: unknown, extra?: Record<string, string>) =>
  respond(status, JSON.stringify(data), "application/json; charset=utf-8", extra);
const text = (status: number, body: string) => respond(status, body, "text/plain; charset=utf-8");

/** La pagina servita dal Worker: identica a app/index.html piu' il segnale che la tiene in modalita' vetrina. */
export function vetrinaPage(page: string): string {
  const i = page.indexOf("<head>");
  if (i < 0) throw new Error("pagina senza <head>");
  return `${page.slice(0, i + 6)}\n${VETRINA_META}${page.slice(i + 6)}`;
}

export async function handle(request: Request, deps: AppDeps): Promise<Response> {
  const head = request.method === "HEAD";
  const out = (r: Response) => (head ? new Response(null, { status: r.status, headers: r.headers }) : r);
  if (deps.state?.trim().toLowerCase() !== "attivo")
    return out(text(503, "CUORE e' spento (interruttore CUORE_STATO)."));
  if (request.method !== "GET" && request.method !== "POST" && !head)
    return out(respond(405, "metodo non ammesso", "text/plain; charset=utf-8", { Allow: "GET, HEAD, POST" }));
  const url = new URL(request.url);
  if (request.method === "POST" && url.pathname === CATTURA_PATH) return cattura(request, deps);
  const api = url.pathname.startsWith("/api/");
  if (deps.config === null || deps.keys === null)
    return out(
      json(503, {
        errore: "configurazione_mancante",
        messaggio: "accesso non configurato: nessuna pagina servita",
      }),
    );
  const site = request.headers.get("Sec-Fetch-Site");
  if (site === "cross-site" || site === "same-site")
    return out(json(403, { errore: "richiesta_da_altro_sito" }));
  // macchina (E3): identita' di servizio del Mac, mai quella del titolare; prima di ogni altra verifica
  if (request.method === "POST" && url.pathname === SCHOOL_PATH)
    return machinePush(request, deps.config, deps.keys, deps, {
      ids: deps.school === undefined ? [] : (deps.schoolServiceIds ?? []),
      spento: "invio_school_spento",
      max: SCHOOL_MAX_BODY,
      target: () => (deps.school as () => FinanceStub)(),
      url: "https://school.interno/feed",
    });
  if (request.method === "POST" && url.pathname === MIGRAZIONE_PATH)
    return machinePush(request, deps.config, deps.keys, deps, {
      ids: deps.origineMac === true ? (deps.migrazioneServiceIds ?? []) : [],
      spento: "migrazione_spenta",
      max: MIGRAZIONE_MAX_BODY,
      target: deps.finance,
      url: "https://finance.interno/migrazione",
    });

  if (request.method === "GET" && url.pathname === `${SCHOOL_PATH}/richiesta`)
    return richiestaSchool(request, deps.config, deps.keys, deps);
  if (request.method === "GET" && (url.pathname === BACKUP_PATH || url.pathname === `${BACKUP_PATH}/elenco`))
    return ritiroBackup(request, url, deps.config, deps.keys, deps);

  const verdict = await verifyAccessJwt(
    request.headers.get(ACCESS_HEADER),
    deps.config,
    deps.keys,
    Math.floor(deps.nowMs() / 1000),
  );
  if (!verdict.ok)
    return out(
      api
        ? json(403, { errore: "accesso_negato", motivo: verdict.reason })
        : text(403, `Accesso negato (${verdict.reason}).`),
    );

  const online = deps.origineMac === true;
  if (request.method === "POST") {
    if (url.pathname === WRITE_PATH)
      return write(request, url, deps, (body) =>
        deps.finance().fetch(new Request("https://finance.interno/esegui", { method: "POST", body })),
      );
    if (online && url.pathname.startsWith(NOTIFICHE_PATH))
      return write(request, url, deps, (body) =>
        deps.finance().fetch(
          new Request(`https://finance.interno/notifiche/${url.pathname.slice(NOTIFICHE_PATH.length)}`, {
            method: "POST",
            body,
          }),
        ),
      );
    if (online && url.pathname === ONLINE_WRITE_PATH)
      return write(request, url, deps, async (body) => {
        const r = await onlineWrite(body, { finance: deps.finance(), school: deps.school?.() });
        return Response.json(r.body, { status: r.status });
      });
    return respond(405, "metodo non ammesso", "text/plain; charset=utf-8", { Allow: "GET, HEAD" });
  }
  if (url.pathname === "/" || url.pathname === "/index.html") {
    const html = online ? onlinePage(deps.page, deps.writes === true) : vetrinaPage(deps.page);
    return out(respond(200, deps.decoratePage ? deps.decoratePage(html) : html, "text/html; charset=utf-8"));
  }
  const asset = deps.assets?.[url.pathname];
  if (asset !== undefined) return out(respond(200, asset.body as string, asset.type));
  if (online && api) {
    try {
      const r = await onlineGet(url, {
        finance: deps.finance(),
        school: deps.school?.(),
        writes: deps.writes === true,
      });
      if (r !== null) return out(json(r.status, r.body));
    } catch {
      return out(json(502, { errore: "durable_object_non_disponibile" }));
    }
  }
  if (url.pathname === "/api/vetrina/stato") {
    try {
      const r = await deps.finance().fetch(new Request("https://finance.interno/stato"));
      return out(respond(r.status, await r.text(), "application/json; charset=utf-8"));
    } catch {
      return out(json(502, { errore: "durable_object_non_disponibile" }));
    }
  }
  if (url.pathname === SCHOOL_PATH && deps.school !== undefined) {
    try {
      const r = await deps.school().fetch(new Request("https://school.interno/feed"));
      return out(respond(r.status, await r.text(), "application/json; charset=utf-8"));
    } catch {
      return out(json(502, { errore: "durable_object_non_disponibile" }));
    }
  }
  return out(api ? json(404, { errore: "non_trovato" }) : text(404, "Non trovato."));
}

/** Rotta di una MACCHINA (token di servizio di Access, mai il titolare): `POST /api/school` (E3, `mac-school`: il DO
 * School valida a schema chiuso e conserva solo l'ultimo feed) e `POST /api/finance/migrazione` (M3). Nessun Client ID
 * ammesso = rotta chiusa (503); poi firma, JSON e dimensione massima prima di chiamare il Durable Object. */
async function machinePush(
  request: Request,
  config: AccessConfig,
  keys: KeySource,
  deps: AppDeps,
  route: {
    ids: readonly string[];
    spento: string;
    max: number;
    target: () => FinanceStub;
    url: string;
  },
): Promise<Response> {
  if (route.ids.length === 0) return json(503, { ok: false, errore: route.spento });
  const v = await verifyServiceJwt(
    request.headers.get(ACCESS_HEADER),
    config,
    keys,
    Math.floor(deps.nowMs() / 1000),
    route.ids,
  );
  if (!v.ok) return json(403, { ok: false, errore: "accesso_negato", motivo: v.reason });
  if ((request.headers.get("Content-Type") ?? "").split(";")[0]?.trim().toLowerCase() !== "application/json")
    return json(415, { ok: false, errore: "tipo_non_ammesso" });
  const tooBig = () => json(413, { ok: false, errore: "richiesta_troppo_grande" });
  if (Number(request.headers.get("Content-Length") ?? 0) > route.max) return tooBig();
  const body = await request.arrayBuffer();
  if (body.byteLength > route.max) return tooBig();
  try {
    const r = await route.target().fetch(new Request(route.url, { method: "POST", body }));
    return respond(r.status, await r.text(), "application/json; charset=utf-8");
  } catch {
    return json(502, { ok: false, errore: "durable_object_non_disponibile" });
  }
}

/** Il Mac (`mac-school`) chiede se dal telefono e' stato chiesto un giro School: `{richiesta: istante | null}`. */
async function richiestaSchool(request: Request, config: AccessConfig, keys: KeySource, deps: AppDeps) {
  const ids = deps.school === undefined ? [] : (deps.schoolServiceIds ?? []);
  if (ids.length === 0) return json(503, { ok: false, errore: "invio_school_spento" });
  const v = await verifyServiceJwt(
    request.headers.get(ACCESS_HEADER),
    config,
    keys,
    Math.floor(deps.nowMs() / 1000),
    ids,
  );
  if (!v.ok) return json(403, { ok: false, errore: "accesso_negato", motivo: v.reason });
  try {
    const r = await (deps.school as () => FinanceStub)().fetch(
      new Request("https://school.interno/richiesta"),
    );
    return respond(r.status, await r.text(), "application/json; charset=utf-8");
  } catch {
    return json(502, { ok: false, errore: "durable_object_non_disponibile" });
  }
}

/** Ritiro dei backup L1 da parte del Mac (`mac-backup`): token di servizio verificato PRIMA di toccare il bucket; il
 * file e' gia' cifrato e firmato (backup.ts), qui passa cosi' com'e'. Nessun Client ID o nessun bucket = 503. */
async function ritiroBackup(
  request: Request,
  url: URL,
  config: AccessConfig,
  keys: KeySource,
  deps: AppDeps,
): Promise<Response> {
  const bucket = deps.backupBucket;
  const ids = bucket === undefined ? [] : (deps.backupServiceIds ?? []);
  if (bucket === undefined || ids.length === 0)
    return json(503, { ok: false, errore: "ritiro_backup_spento" });
  const v = await verifyServiceJwt(
    request.headers.get(ACCESS_HEADER),
    config,
    keys,
    Math.floor(deps.nowMs() / 1000),
    ids,
  );
  if (!v.ok) return json(403, { ok: false, errore: "accesso_negato", motivo: v.reason });
  try {
    const copie = await elencoBackup(bucket);
    if (url.pathname.endsWith("/elenco")) return json(200, { ok: true, copie });
    const chiesto = url.searchParams.get("giorno");
    if (chiesto !== null && !giornoValido(chiesto))
      return json(400, { ok: false, errore: "giorno_non_valido" });
    const giorno = chiesto ?? copie[0]?.giorno;
    const obj = giorno === undefined ? null : await bucket.get(nomeBackup(giorno));
    if (obj === null || giorno === undefined) return json(404, { ok: false, errore: "backup_non_trovato" });
    return new Response(obj.body, {
      status: 200,
      headers: {
        ...SECURITY_HEADERS,
        "Content-Type": "application/json; charset=utf-8",
        "X-Cuore-Backup-Giorno": giorno,
      },
    });
  } catch {
    return json(502, { ok: false, errore: "bucket_non_disponibile" });
  }
}

/** `/cattura` (C3): secondo strato della doppia barriera (token applicativo), nessun Access, nessun Durable Object
 * chiamato senza prima aver verificato il token (§4.3: doppia barriera, schema chiuso, al massimo 2 KiB). */
async function cattura(request: Request, deps: AppDeps): Promise<Response> {
  if ((await checkToken(request.headers.get("Authorization"), deps.catturaTokenHash)) !== true)
    return json(401, { ok: false, errore: "non_autorizzato" });
  if ((request.headers.get("Content-Type") ?? "").split(";")[0]?.trim().toLowerCase() !== "application/json")
    return json(415, { ok: false, errore: "tipo_non_ammesso" });
  const tooBig = () => json(413, { ok: false, errore: "richiesta_troppo_grande" });
  if (Number(request.headers.get("Content-Length") ?? 0) > CATTURA_MAX_BODY) return tooBig();
  const body = await request.arrayBuffer();
  if (body.byteLength > CATTURA_MAX_BODY) return tooBig();
  try {
    const r = await deps
      .finance()
      .fetch(new Request("https://finance.interno/cattura", { method: "POST", body }));
    return respond(r.status, await r.text(), "application/json; charset=utf-8");
  } catch {
    return json(502, { ok: false, errore: "durable_object_non_disponibile" });
  }
}

/** Scrittura del titolare (dopo Access): interruttore, Origin del sito, JSON, al massimo 64 KiB; poi `send`. */
async function write(
  request: Request,
  url: URL,
  deps: AppDeps,
  send: (body: ArrayBuffer) => Promise<Response>,
): Promise<Response> {
  if (deps.writes !== true) return json(403, { ok: false, errore: "scritture_spente" });
  if (request.headers.get("Origin") !== url.origin)
    return json(403, { ok: false, errore: "origine_non_ammessa" });
  if ((request.headers.get("Content-Type") ?? "").split(";")[0]?.trim().toLowerCase() !== "application/json")
    return json(415, { ok: false, errore: "tipo_non_ammesso" });
  const tooBig = () => json(413, { ok: false, errore: "richiesta_troppo_grande" });
  if (Number(request.headers.get("Content-Length") ?? 0) > MAX_BODY) return tooBig();
  const body = await request.arrayBuffer();
  if (body.byteLength > MAX_BODY) return tooBig();
  try {
    const r = await send(body);
    return respond(r.status, await r.text(), "application/json; charset=utf-8");
  } catch {
    return json(502, { ok: false, errore: "durable_object_non_disponibile" });
  }
}
