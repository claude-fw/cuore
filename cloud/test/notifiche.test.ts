/**
 * Notifiche push e promemoria ONLINE (solo dati sintetici, nessuna rete): operazioni del ponte nel DO Finance
 * (promemoria_stato/imposta, appunti_stato/scrivi), dispositivi Web Push, Cron ai momenti 07:15/15:00/19:00, push
 * VAPID senza contenuto verso un finto servizio push, anti-ripetizione, 410, Access obbligatorio. Piu' le funzioni
 * pure (orari UTC dei Cron, coerenza delle chiavi VAPID, raggruppamenti come il Mac).
 */
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { before, describe, it } from "node:test";
import { generaVapid } from "../scripts/vapid.ts";
import { promemoriaMoment } from "../src/automazioni.ts";
import { addDays, group, halfWeek, normalize, romeAt } from "../src/promemoria.ts";
import { parseVapid, publicKeyOf, pushInfo } from "../src/push.ts";
import { AUD, bundle, EMAIL, startWorker, TEAM, type TestKey, testKey, token } from "./helpers.ts";

// biome-ignore lint/suspicious/noExplicitAny: documenti JSON letti nei test
type Doc = Record<string, any>;
type Mf = Awaited<ReturnType<typeof startWorker>>;
const URL0 = "https://cuore.esempio.workers.dev";
const PUSH = "https://web.push.apple.com/QGRpc3Bvc2l0aXZv";
const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Rome" }).format(new Date());

let key: TestKey;
let script = "";
let vapid = { jwk: "", pub: "" };
const pushes: Request[] = [];
const outbound = (r: Request) => {
  if (r.url === `https://${TEAM}/cdn-cgi/access/certs`) return Response.json({ keys: [key.jwk] });
  if (r.url.startsWith("https://web.push.apple.com/")) {
    pushes.push(r);
    return new Response(null, { status: r.url.endsWith("-scaduta") ? 410 : 201 });
  }
  return new Response("rete non disponibile nei test", { status: 599 });
};

before(async () => {
  key = await testKey("chiave-team-notifiche");
  script = await bundle("index");
  const k = await generaVapid();
  vapid = { jwk: JSON.stringify(k.privata), pub: k.pubblica };
});

// Vetrina sintetica con le rotte online: origine «mac» non serve per i promemoria, ma le rotte /api/notifiche e le
// operazioni del ponte esistono solo online; il DO in attesa dei dati salta il Cron, quindi qui il DO e' sintetico
// (CUORE_FINANCE_ORIGINE vuota) per il Cron e online per le API.
const start = (online: boolean, extra: Record<string, string> = {}) =>
  startWorker(
    script,
    {
      ACCESS_TEAM_DOMAIN: TEAM,
      ACCESS_AUD: AUD,
      ACCESS_ALLOWED_EMAILS: EMAIL,
      CUORE_STATO: "attivo",
      CUORE_SCRITTURE: "attive",
      ...(online ? { CUORE_FINANCE_ORIGINE: "mac" } : {}),
      VAPID_PRIVATE_JWK: vapid.jwk,
      VAPID_SUBJECT: "https://cuore.esempio.workers.dev",
      VAPID_PUBLIC_KEY: vapid.pub,
      ...extra,
    },
    outbound,
  );

async function call(mf: Mf, path: string, body?: Doc, auth = true): Promise<{ status: number; doc: Doc }> {
  const r = await mf.dispatchFetch(`${URL0}${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      ...(auth ? { "Cf-Access-Jwt-Assertion": await token(key) } : {}),
      Origin: URL0,
      "Content-Type": "application/json",
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const t = await r.text();
  return { status: r.status, doc: t.startsWith("{") ? JSON.parse(t) : {} };
}
const esegui = (mf: Mf, body: Doc) => call(mf, "/api/scritture/esegui", body);

describe("promemoria e notifiche online", () => {
  it("Access obbligatorio su API, service worker e push.js", async () => {
    const mf = await start(true);
    for (const p of ["/api/notifiche", "/api/notifiche/recenti", "/api/promemoria", "/sw.js", "/push.js"])
      assert.equal((await call(mf, p, undefined, false)).status, 403, p);
    const r = await call(
      mf,
      "/api/notifiche/iscrizione",
      { dispositivo: "dispositivo01", nome: "x", endpoint: PUSH },
      false,
    );
    assert.equal(r.status, 403);
    const sw = await mf.dispatchFetch(`${URL0}/sw.js`, {
      headers: { "Cf-Access-Jwt-Assertion": await token(key) },
    });
    assert.match(await sw.text(), /addEventListener\("push"/);
    assert.match(sw.headers.get("Content-Type") ?? "", /javascript/);
    const page = await mf.dispatchFetch(`${URL0}/`, {
      headers: { "Cf-Access-Jwt-Assertion": await token(key) },
    });
    assert.match(page.headers.get("Content-Security-Policy") ?? "", /script-src 'self'.*worker-src 'self'/);
  });

  it("operazioni del ponte: stessi formati, requestId, idempotenza, 422", async () => {
    const mf = await start(true);
    const st = (await esegui(mf, { op: "promemoria_stato", input: {} })).doc.risultato;
    assert.equal(st.schema, "cuore.promemoria.impostazioni/1");
    assert.equal(st.file, "assente");
    assert.equal(st.attivo, false);
    assert.equal(st.promemoria.length, 14);
    assert.ok(st.promemoria.every((r: Doc) => r.acceso === false && r.prossimo_controllo === null));
    assert.equal(
      (await esegui(mf, { op: "promemoria_imposta", input: { attivo: true } })).doc.errore,
      "requestId_mancante",
    );
    const req = {
      op: "promemoria_imposta",
      requestId: "imposta-0001",
      input: { attivo: true, promemoria: { fin_contanti: true } },
    };
    const a = await esegui(mf, req);
    assert.equal(a.status, 200);
    assert.equal(a.doc.risultato.file, "presente");
    const row = a.doc.risultato.promemoria.find((r: Doc) => r.chiave === "fin_contanti");
    assert.deepEqual([row.acceso, row.momenti], [true, ["sera"]]);
    assert.match(row.prossimo_controllo, /T19:00:00\+0[12]:00$/);
    assert.equal((await esegui(mf, req)).doc.ripetuta, true);
    assert.equal((await esegui(mf, { ...req, input: { attivo: false } })).status, 409);
    const bad = await esegui(mf, {
      op: "promemoria_imposta",
      requestId: "imposta-0002",
      input: { promemoria: { fin_contanti: { momenti: ["mattina"] } } },
    });
    assert.equal(bad.status, 422);
    assert.equal(bad.doc.messaggio, "fin_contanti.momenti: elenco non vuoto fra sera");
    // appunti
    const c = await esegui(mf, {
      op: "appunti_scrivi",
      requestId: "appunto-0001",
      input: { tipo: "crea", testo: " Comprare quaderni ", quando: today },
    });
    assert.equal(c.status, 200);
    assert.equal(c.doc.risultato.testo, "Comprare quaderni");
    assert.equal(c.doc.risultato.promemoria, true);
    assert.equal(c.doc.risultato.richiesta, "appunto-0001");
    const e422 = await esegui(mf, {
      op: "appunti_scrivi",
      requestId: "appunto-0002",
      input: { tipo: "crea", testo: "x", promemoria: true },
    });
    assert.equal(e422.doc.messaggio, "promemoria acceso senza data: indicare quando");
    const id = c.doc.risultato.id;
    await esegui(mf, { op: "appunti_scrivi", requestId: "appunto-0003", input: { tipo: "completa", id } });
    const again = await esegui(mf, {
      op: "appunti_scrivi",
      requestId: "appunto-0004",
      input: { tipo: "completa", id },
    });
    assert.equal(again.doc.messaggio, "appunto gia' completato: non si modifica");
    const s = (await esegui(mf, { op: "appunti_stato", input: {} })).doc.risultato;
    assert.deepEqual(
      [s.schema, s.aperti, s.eventi, s.appunti[0].stato],
      ["cuore.finance.appunti/1", 0, 2, "completato"],
    );
    assert.equal(s.appunti[0].invio_previsto, null);
    const cap = (await call(mf, "/api/scritture")).doc;
    assert.ok(cap.operazioni.includes("appunti_scrivi"));
    assert.deepEqual(cap.solo_sul_mac, []);
  });

  it("dispositivi: uno per dispositivo con nome, chiave pubblica servita, annullo", async () => {
    const mf = await start(true);
    const st = (await call(mf, "/api/notifiche")).doc;
    assert.deepEqual([st.configurate, st.chiave_pubblica, st.attive], [true, vapid.pub, false]);
    const iscr = (d: string, nome: string, endpoint: string) =>
      call(mf, "/api/notifiche/iscrizione", { dispositivo: d, nome, endpoint });
    assert.equal((await iscr("iphone0001", "iPhone", PUSH)).status, 200);
    assert.equal((await iscr("iphone0001", "iPhone di prova", `${PUSH}-nuovo`)).status, 200);
    assert.equal((await iscr("ipad000001", "iPad", "https://esempio.invalid/x")).status, 422);
    assert.equal((await iscr("ipad000001", "iPad", `${PUSH}-ipad`)).status, 200);
    let d = (await call(mf, "/api/notifiche")).doc.dispositivi;
    assert.deepEqual(
      d.map((x: Doc) => x.nome),
      ["iPhone di prova", "iPad"],
    );
    assert.equal((await call(mf, "/api/notifiche/annulla", { dispositivo: "ipad000001" })).status, 200);
    d = (await call(mf, "/api/notifiche")).doc.dispositivi;
    assert.equal(d.length, 1);
    assert.equal((await call(mf, "/api/notifiche/prova", {})).status, 409, "push spento per difetto");
    // chiavi non coerenti: nessuna iscrizione possibile
    const other = await generaVapid();
    const mf2 = await start(true, { VAPID_PUBLIC_KEY: other.pubblica });
    const st2 = (await call(mf2, "/api/notifiche")).doc;
    assert.deepEqual([st2.configurate, st2.motivo], [false, "chiavi_vapid_non_coerenti"]);
    assert.equal(
      (
        await call(mf2, "/api/notifiche/iscrizione", {
          dispositivo: "iphone0001",
          nome: "iPhone",
          endpoint: PUSH,
        })
      ).status,
      503,
    );
  });

  it("Cron delle 19:00: promemoria una volta, push VAPID senza contenuto, 410 rimuove, testi riservati", async () => {
    pushes.length = 0;
    const mf = await start(true);
    await esegui(mf, {
      op: "promemoria_imposta",
      requestId: "imposta-cron1",
      input: { attivo: true, promemoria: { fin_contanti: true } },
    });
    await call(mf, "/api/notifiche/iscrizione", {
      dispositivo: "iphone0001",
      nome: "iPhone",
      endpoint: PUSH,
    });
    await call(mf, "/api/notifiche/iscrizione", {
      dispositivo: "vecchio001",
      nome: "Vecchio",
      endpoint: `${PUSH}-scaduta`,
    });
    await call(mf, "/api/notifiche/imposta", { attive: true });
    // il DO online senza i dati del Mac esegue comunque i promemoria (mai il motore Finance)
    const w = await (
      mf as unknown as { getWorker(): Promise<{ scheduled(o: unknown): Promise<unknown> }> }
    ).getWorker();
    const sera = romeAt(today, "19:00");
    await w.scheduled({ scheduledTime: new Date(sera), cron: "0 13,14,18 * * *" });
    assert.equal(pushes.length, 2, "un segnale per dispositivo");
    const p = pushes[0] as Request;
    assert.equal(await p.text(), "", "nessun contenuto presso il servizio push");
    assert.equal(p.headers.get("Authorization")?.split("k=")[1], vapid.pub);
    const d = (await call(mf, "/api/notifiche")).doc.dispositivi;
    assert.deepEqual(
      d.map((x: Doc) => x.nome),
      ["iPhone"],
      "iscrizione scaduta (410) rimossa",
    );
    const rec = (await call(mf, "/api/notifiche/recenti")).doc.notifiche;
    assert.equal(rec.length, 1);
    assert.deepEqual(
      [rec[0].titolo, rec[0].testo],
      ["CUORE · Finance", "Segna le spese in contanti di oggi"],
    );
    // stesso momento ripetuto e giorno dopo con la stessa chiave: nessuna ripetizione
    await w.scheduled({ scheduledTime: new Date(sera), cron: "0 13,14,18 * * *" });
    await w.scheduled({ scheduledTime: new Date(sera + 86_400_000), cron: "0 13,14,18 * * *" });
    assert.equal(pushes.length, 2, "anti-ripetizione");
    // mattina: fin_contanti e' solo di sera, niente
    await w.scheduled({ scheduledTime: new Date(romeAt(today, "07:15")), cron: "15 5,6 * * *" });
    assert.equal(pushes.length, 2);
    // prova manuale: un segnale al dispositivo rimasto
    assert.equal((await call(mf, "/api/notifiche/prova", {})).status, 200);
    assert.equal(pushes.length, 3);
    // vetrina sintetica: niente rotte online
    const vetrina = await start(false);
    assert.equal((await call(vetrina, "/api/notifiche")).status, 404);
  });
});

describe("Cron, ora italiana e VAPID (funzioni pure)", () => {
  it("momenti dai Cron UTC: ora solare e legale, la stagione sbagliata non fa nulla", () => {
    const utc = (iso: string) => Date.parse(iso);
    // inverno (UTC+1)
    assert.equal(promemoriaMoment(utc("2026-12-01T06:15:00Z")), "mattina");
    assert.equal(promemoriaMoment(utc("2026-12-01T14:00:00Z")), "pomeriggio");
    assert.equal(promemoriaMoment(utc("2026-12-01T18:00:00Z")), "sera");
    for (const t of ["05:15", "13:00", "17:00", "05:00", "11:00", "21:00"])
      assert.equal(promemoriaMoment(utc(`2026-12-01T${t}:00Z`)), null, t);
    // estate (UTC+2)
    assert.equal(promemoriaMoment(utc("2026-07-01T05:15:00Z")), "mattina");
    assert.equal(promemoriaMoment(utc("2026-07-01T13:00:00Z")), "pomeriggio");
    assert.equal(promemoriaMoment(utc("2026-07-01T17:00:00Z")), "sera");
    for (const t of ["06:15", "14:00", "18:00"])
      assert.equal(promemoriaMoment(utc(`2026-07-01T${t}:00Z`)), null, t);
  });

  it("date come il Mac: meta' settimana, giorni, istante locale", () => {
    assert.equal(halfWeek("2026-11-02"), "2026-W45-lun");
    assert.equal(halfWeek("2026-11-05"), "2026-W45-gio");
    assert.equal(halfWeek("2027-01-01"), "2026-W53-gio");
    assert.equal(addDays("2026-12-31", 1), "2027-01-01");
    assert.equal(new Date(romeAt("2026-03-29", "07:15")).toISOString(), "2026-03-29T05:15:00.000Z");
  });

  it("impostazioni e raggruppamenti come promemoria.py", () => {
    const n = normalize({ attivo: true, promemoria: { sch_impegni: true } });
    assert.deepEqual(n.promemoria.sch_impegni, { attivo: true, momenti: ["sera"] });
    assert.equal(n.riservato, true);
    assert.throws(
      () => normalize({ soglie: { budget_percento: 100 } }),
      /budget_percento: intero da 50 a 99/,
    );
    const r = (k: string, d: string) => ({
      chiave: k,
      tipo: "sch_verifica",
      gruppo: "verifica:1",
      testo: `Verifica di ${d} domani`,
      riservato: "Domani c'è una verifica",
      plurale: "Domani ci sono {n} verifiche",
      dettaglio: d,
    });
    assert.deepEqual(group([r("a", "Fisica")]), [
      { gruppo: "verifica:1", titolo: "CUORE · School", testo: "Domani c'è una verifica", chiavi: ["a"] },
    ]);
    assert.equal(
      group([r("a", "Fisica"), r("b", "Storia")], false)[0]?.testo,
      "Domani ci sono 2 verifiche: Fisica, Storia",
    );
  });

  it("chiave pubblica ricavata dalla privata; firma ES256 verificabile", async () => {
    const k = await generaVapid();
    const v = parseVapid(JSON.stringify(k.privata), "mailto:prova@prova.invalid");
    assert.ok(v);
    assert.equal(publicKeyOf(v), k.pubblica);
    assert.equal(pushInfo(v, k.pubblica).chiavePubblica, k.pubblica);
    assert.equal(pushInfo(v, "").motivo, "chiave_pubblica_mancante");
    assert.equal(pushInfo(null, k.pubblica).motivo, "chiave_privata_o_subject_mancanti");
    const pub = await webcrypto.subtle.importKey(
      "raw",
      Buffer.from(k.pubblica, "base64url"),
      { name: "ECDSA", namedCurve: "P-256" },
      false,
      ["verify"],
    );
    assert.ok(pub);
  });
});
