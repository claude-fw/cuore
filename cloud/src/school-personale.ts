/**
 * Dati personali di School nel cloud (E4 online): compiti fatti, note, impegni, obiettivi di media scritti dal telefono.
 * Stesse regole e stesso formato dell'archivio del Mac (`app/bridge/school_personale.py`, `cuore.school.personale/1`):
 * soli eventi aggiunti, mai riscritti o cancellati; note e impegni si tolgono con un evento `annulla`; `richiesta` (il
 * `requestId` della pagina) rende idempotente ogni scrittura. Vive nel Durable Object «School» (SQLite suo, separato da
 * Finance), in una tabella diversa da quella del feed: il feed del Mac la sostituisce, questa non la tocca mai.
 * Gli eventi scritti qui NON tornano da soli sul Mac (e viceversa): vedi il libro di bordo, sezione «dati veri online».
 */
import type { DurableSqlStorage } from "./durable-db.ts";

export const PERSONALE_SCHEMA = "cuore.school.personale/1";
export const PERSONALE_TABLE = `CREATE TABLE IF NOT EXISTS school_personale (
  n INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, richiesta TEXT UNIQUE, evento TEXT NOT NULL
) STRICT`;
const MAX_EVENTS = 20_000;
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const KEY = /^[a-z0-9][a-z0-9_:.-]{0,80}$/;

type Ev = Record<string, unknown>;
class NonValido extends Error {}

const text = (v: unknown, n: number, name: string): string => {
  if (typeof v !== "string" || !v.trim() || v.length > n)
    throw new NonValido(`${name}: testo da 1 a ${n} caratteri`);
  return v.trim();
};
const key = (v: unknown, name: string, optional = false): string | null => {
  if ((v === null || v === undefined) && optional) return null;
  if (typeof v !== "string" || !KEY.test(v)) throw new NonValido(`${name} non valido`);
  return v;
};
const realDate = (d: string) => new Date(`${d}T00:00:00Z`).toISOString().slice(0, 10) === d;

/** Evento pulito (solo i campi ammessi), come `valida` del Mac. */
function valida(ev: unknown): Ev {
  if (typeof ev !== "object" || ev === null || Array.isArray(ev)) throw new NonValido("evento non valido");
  const e = ev as Ev;
  switch (e.tipo) {
    case "compito":
      if (typeof e.fatto !== "boolean") throw new NonValido("fatto: vero o falso");
      return { tipo: "compito", uid: key(e.uid, "uid"), fatto: e.fatto };
    case "nota":
      return {
        tipo: "nota",
        testo: text(e.testo, 500, "nota"),
        materia: key(e.materia, "materia", true),
        uid: key(e.uid, "uid", true),
      };
    case "impegno":
      if (typeof e.data !== "string" || !DATE.test(e.data) || !realDate(e.data))
        throw new NonValido("data: AAAA-MM-GG");
      return {
        tipo: "impegno",
        testo: text(e.testo, 200, "impegno"),
        data: e.data,
        materia: key(e.materia, "materia", true),
      };
    case "obiettivo":
      if (e.media != null && (typeof e.media !== "number" || !(e.media >= 1 && e.media <= 10)))
        throw new NonValido("media: da 1 a 10, o null per toglierla");
      return { tipo: "obiettivo", materia: key(e.materia, "materia"), media: e.media ?? null };
    case "annulla":
      return { tipo: "annulla", id: key(e.id, "id"), motivo: text(e.motivo || "annullato", 200, "motivo") };
    default:
      throw new NonValido("tipo sconosciuto");
  }
}

const eventi = (sql: DurableSqlStorage): Ev[] =>
  sql.sql
    .exec("SELECT evento FROM school_personale ORDER BY n")
    .toArray()
    .map((r) => JSON.parse(r.evento as string) as Ev);

function aggiungi(sql: DurableSqlStorage, input: unknown, richiesta: string, now: Date): Ev {
  const ev = valida(input);
  if (richiesta.length < 1 || richiesta.length > 100) throw new NonValido("requestId non valido");
  const evs = eventi(sql);
  const prev = evs.find((e) => e.richiesta === richiesta);
  if (prev !== undefined) {
    if (Object.keys(ev).some((k) => JSON.stringify(prev[k] ?? null) !== JSON.stringify(ev[k])))
      throw new NonValido("richiesta gia' usata con un contenuto diverso");
    return prev;
  }
  if (evs.length >= MAX_EVENTS) throw new NonValido("archivio pieno");
  if (
    ev.tipo === "annulla" &&
    !evs.some((e) => e.id === ev.id && (e.tipo === "nota" || e.tipo === "impegno"))
  )
    throw new NonValido("nota o impegno inesistente");
  const at = `${now.toISOString().slice(0, 19)}+00:00`;
  // come il Mac: in un `annulla` il campo `id` dell'evento (la nota o l'impegno tolti) prende il posto dell'id nuovo
  const rec: Ev = { id: crypto.randomUUID().replaceAll("-", ""), at, ...ev, richiesta };
  sql.sql.exec(
    "INSERT INTO school_personale (id, richiesta, evento) VALUES (?, ?, ?)",
    rec.id as string,
    richiesta,
    JSON.stringify(rec),
  );
  return rec;
}

/** Stato corrente, come `stato` del Mac: l'ultimo evento vince per compiti e obiettivi; annullati esclusi. */
function stato(sql: DurableSqlStorage): Ev {
  const evs = eventi(sql);
  const off = new Set(evs.filter((e) => e.tipo === "annulla").map((e) => e.id));
  const fatti: Record<string, unknown> = {};
  const obiettivi: Record<string, unknown> = {};
  for (const e of evs) {
    if (e.tipo === "compito") fatti[e.uid as string] = e.fatto ? e.at : null;
    else if (e.tipo === "obiettivo") obiettivi[e.materia as string] = e.media;
  }
  const keep = (t: string) =>
    evs.filter((e) => e.tipo === t && !off.has(e.id)).map(({ tipo: _t, richiesta: _r, ...rest }) => rest);
  return {
    schema: PERSONALE_SCHEMA,
    fatti: Object.fromEntries(Object.entries(fatti).filter(([, v]) => v)),
    obiettivi: Object.fromEntries(Object.entries(obiettivi).filter(([, v]) => v !== null)),
    note: keep("nota"),
    impegni: keep("impegno").sort((a, b) =>
      String(a.data) < String(b.data) ? -1 : String(a.data) > String(b.data) ? 1 : 0,
    ),
    eventi: evs.length,
  };
}

/** `POST /personale` del Durable Object School: `{op: "school_stato" | "school_scrivi", input, richiesta}`. */
export function personale(
  sql: DurableSqlStorage,
  body: unknown,
  now = new Date(),
): { status: number; body: Ev } {
  const { op, input, richiesta } = (typeof body === "object" && body !== null ? body : {}) as Ev;
  try {
    if (op === "school_stato") return { status: 200, body: { ok: true, risultato: stato(sql) } };
    if (op === "school_scrivi" && typeof richiesta === "string")
      return { status: 200, body: { ok: true, risultato: aggiungi(sql, input, richiesta, now) } };
    return { status: 400, body: { ok: false, errore: "operazione_sconosciuta" } };
  } catch (err) {
    if (err instanceof NonValido)
      return { status: 422, body: { ok: false, errore: "non_valido", messaggio: err.message } };
    return { status: 500, body: { ok: false, errore: "errore" } };
  }
}
