/**
 * Automazioni (C3) in workerd (Miniflare), dati sintetici, senza rete: Cron idempotente, coda con l'esecutore B3
 * persistente, notifiche spente per difetto, push VAPID senza contenuto verso un finto servizio push, riprove.
 */
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { before, describe, it } from "node:test";
import { AUD, bundle, EMAIL, startWorker, TEAM, type TestKey, testKey, token } from "./helpers.ts";

const URL0 = "https://cuore.esempio.workers.dev";
const PUSH = "https://web.push.apple.com/QGxpcHJvdmEtY3VvcmU";
const MONTH = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Rome" }).format(new Date()).slice(0, 7);

let key: TestKey;
let script = "";
let vapidJwk = "";
let vapidPub: webcrypto.CryptoKey;
let pushStatus = 201;
const pushes: Request[] = [];
const outbound = (r: Request) => {
  if (r.url === `https://${TEAM}/cdn-cgi/access/certs`) return Response.json({ keys: [key.jwk] });
  if (r.url.startsWith("https://web.push.apple.com/")) {
    pushes.push(r);
    return new Response(null, { status: r.url.endsWith("-scaduta") ? 410 : pushStatus });
  }
  return new Response("rete non disponibile nei test", { status: 599 });
};

before(async () => {
  key = await testKey("chiave-team");
  script = await bundle("index");
  const kp = await webcrypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, [
    "sign",
    "verify",
  ]);
  vapidJwk = JSON.stringify(await webcrypto.subtle.exportKey("jwk", kp.privateKey));
  vapidPub = kp.publicKey;
});

type Mf = Awaited<ReturnType<typeof startWorker>>;
// biome-ignore lint/suspicious/noExplicitAny: risposte JSON lette nei test
type Doc = Record<string, any>;

const start = () =>
  startWorker(
    script,
    {
      ACCESS_TEAM_DOMAIN: TEAM,
      ACCESS_AUD: AUD,
      ACCESS_ALLOWED_EMAILS: EMAIL,
      CUORE_STATO: "attivo",
      CUORE_SCRITTURE: "attive",
      VAPID_PRIVATE_JWK: vapidJwk,
      VAPID_SUBJECT: "mailto:titolare@prova.invalid",
    },
    outbound,
  );

let n = 0;
async function op(mf: Mf, name: string, input: Doc = {}): Promise<Doc> {
  const r = await mf.dispatchFetch(`${URL0}/api/finance/esegui`, {
    method: "POST",
    headers: {
      "Cf-Access-Jwt-Assertion": await token(key),
      Origin: URL0,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ op: name, input, requestId: `prova-${String(++n).padStart(6, "0")}` }),
  });
  const doc = (await r.json()) as Doc;
  assert.equal(r.status, 200, `${name}: ${JSON.stringify(doc)}`);
  return doc.risultato;
}

const cron = async (mf: Mf) => {
  const w = await (
    mf as unknown as { getWorker(): Promise<{ scheduled(o: unknown): Promise<unknown> }> }
  ).getWorker();
  await w.scheduled({ scheduledTime: new Date(), cron: "0 5,11,17,21 * * *" });
};

/** Budget totale di 1 € nel mese corrente superato da una spesa classificata in contanti. */
async function overBudget(mf: Mf) {
  const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Rome" }).format(new Date());
  const add = await op(mf, "aggiungi_manuale", {
    accountId: "contanti-demo",
    transactionDate: today,
    amountMinor: -450,
    currency: "EUR",
    description: "Pizza",
  });
  await op(mf, "classifica", { id: add.id, tipo: "expense", categoria: "bar-ristoranti" });
  await op(mf, "imposta_budget", {
    scope: "total",
    target: null,
    validFrom: MONTH,
    currency: "EUR",
    amountMinor: 100,
  });
}

describe("automazioni (C3, sintetico)", () => {
  it("Cron due volte nella stessa ora = un lavoro per tipo; notifica registrata una volta; spente: nessun push", async () => {
    pushes.length = 0;
    const mf = await start();
    await overBudget(mf);
    await op(mf, "iscrivi_push", { endpoint: PUSH });
    await cron(mf);
    await cron(mf);
    const st = await op(mf, "stato_automazioni");
    assert.equal(st.notifiche, "spente");
    assert.deepEqual(st.coda, []);
    const byType = (t: string) => st.lavori.filter((l: Doc) => l.chiave.startsWith(`${t}:`));
    assert.equal(byType("abbinamenti").length, 1);
    assert.equal(byType("budget").length, 1);
    assert.equal(byType("budget")[0].stato, "succeeded");
    assert.equal(byType("push")[0].esito.motivo, "notifiche_spente");
    const list = await op(mf, "notifiche");
    assert.equal(list.length, 1);
    assert.match(list[0].testo, /4,50 € su 1,00 €/);
    assert.equal(pushes.length, 0);
  });

  it("notifiche attive: push VAPID firmato e senza contenuto; 410 toglie l'iscrizione; errore 500 = riprova in coda", async () => {
    pushes.length = 0;
    pushStatus = 201;
    const mf = await start();
    await op(mf, "imposta_notifiche", { attive: true });
    await op(mf, "iscrivi_push", { endpoint: PUSH });
    await op(mf, "iscrivi_push", { endpoint: `${PUSH}-scaduta` });
    await overBudget(mf);
    await cron(mf);
    assert.equal(pushes.length, 2);
    const p = pushes[0] as Request;
    assert.equal(p.method, "POST");
    assert.equal((await p.arrayBuffer()).byteLength, 0, "nessun contenuto");
    const m = /^vapid t=([^.]+)\.([^.]+)\.([^,]+), k=(.+)$/.exec(p.headers.get("Authorization") ?? "");
    assert.ok(m, p.headers.get("Authorization") ?? "");
    const [, h, c, s] = m;
    const claims = JSON.parse(Buffer.from(c as string, "base64url").toString());
    assert.equal(claims.aud, "https://web.push.apple.com");
    assert.equal(claims.sub, "mailto:titolare@prova.invalid");
    const ok = await webcrypto.subtle.verify(
      { name: "ECDSA", hash: "SHA-256" },
      vapidPub,
      Buffer.from(s as string, "base64url"),
      new TextEncoder().encode(`${h}.${c}`),
    );
    assert.ok(ok, "firma VAPID valida");
    let st = await op(mf, "stato_automazioni");
    assert.equal(st.iscrizioni_push, 1, "iscrizione scaduta (410) rimossa");

    pushStatus = 500;
    const mf2 = await start();
    await op(mf2, "imposta_notifiche", { attive: true });
    await op(mf2, "iscrivi_push", { endpoint: PUSH });
    await overBudget(mf2);
    await cron(mf2);
    pushStatus = 201;
    st = await op(mf2, "stato_automazioni");
    const retry = st.lavori.filter((l: Doc) => l.stato === "retry_wait");
    assert.equal(retry.length, 1, JSON.stringify(st.lavori));
    assert.match(retry[0].errore, /servizio push: 500/);
    assert.equal(st.coda.length, 1);
    assert.ok(st.coda[0].dopo > Date.now(), "riprova pianificata con attesa");
  });

  it("iscrizioni solo verso servizi push noti in https; impostazione validata", async () => {
    const mf = await start();
    for (const endpoint of [
      "http://web.push.apple.com/x",
      "https://web.push.apple.com:8443/x",
      "https://evil.invalid/x",
      "https://127.0.0.1/x",
      "https://utente:pw@fcm.googleapis.com/x",
    ]) {
      const r = await mf.dispatchFetch(`${URL0}/api/finance/esegui`, {
        method: "POST",
        headers: {
          "Cf-Access-Jwt-Assertion": await token(key),
          Origin: URL0,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ op: "iscrivi_push", requestId: "iscr-0001-prova", input: { endpoint } }),
      });
      assert.equal(r.status, 422, endpoint);
    }
    assert.equal((await op(mf, "stato_automazioni")).iscrizioni_push, 0);
  });
});
