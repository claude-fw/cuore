#!/usr/bin/env node
/**
 * Banco di misura (src/misure.ts): carica N movimenti SINTETICI a blocchi ed esegue una operazione per richiesta,
 * stampando i tempi. Nessun dato reale. Due modi:
 *
 *   npm run misure                                   in locale: Miniflare/workerd, nessuna rete, token in memoria
 *   npm run misure -- --movimenti 1300 --ripetizioni 5 --blocco 250 --ricarica-ogni 1   (caso pessimo: 1 giroconto al giorno)
 *   MISURE_TOKEN=... npm run misure -- --url https://cuore-misure-sintetiche.<sottodominio>.workers.dev [--pitr]
 *                                                    dopo la pubblicazione del banco (wrangler.misure.jsonc)
 *
 * In locale workerd NON applica i limiti di CPU: i tempi sono di parete visti da fuori (limite superiore della CPU).
 * Nel cloud gli orologi del Worker avanzano solo con l'I/O: la CPU vera si legge dal pannello (Metrics) o da
 * `wrangler tail`; qui conta l'esito (200, oppure 5xx «exceeded CPU»). `--pitr` prova il ripristino a un istante.
 */
import { createHash, randomBytes } from "node:crypto";
import { createRequire } from "node:module";
import { parseArgs } from "node:util";

const FINANCE = new URL("../../finance/package.json", import.meta.url).pathname;

interface Risposta {
  status: number;
  doc: Record<string, unknown>;
  ms: number;
}
type Chiama = (path: string, body?: unknown) => Promise<Risposta>;

export interface Riga {
  operazione: string;
  esito: string;
  ms_mediana: number;
  ms_min: number;
  ms_interni: number | null;
  volte: number;
}

const mediana = (xs: number[]) => {
  const s = [...xs].sort((a, b) => a - b);
  return s[Math.floor((s.length - 1) / 2)] ?? 0;
};
const r1 = (x: number) => Math.round(x * 10) / 10;

/** Avvia il banco in workerd (Miniflare) senza rete; restituisce chi chiama e come spegnerlo. */
export async function bancoLocale(): Promise<{ chiama: Chiama; chiudi: () => Promise<void> }> {
  const req = createRequire(FINANCE);
  const { build } = req("esbuild");
  const out = await build({
    entryPoints: [new URL("../src/misure.ts", import.meta.url).pathname],
    bundle: true,
    format: "esm",
    platform: "browser",
    target: "es2024",
    write: false,
    logLevel: "silent",
    external: ["cloudflare:workers"],
  });
  const token = randomBytes(32).toString("hex");
  const { Miniflare, convertV4MiniflareOptions } = req("miniflare");
  const mf = new Miniflare(
    convertV4MiniflareOptions({
      modules: true,
      script: out.outputFiles[0].text,
      compatibilityDate: "2026-09-01",
      bindings: { MISURE_TOKEN_SHA256: createHash("sha256").update(token).digest("hex") },
      durableObjects: { BANCO: { className: "BancoMisure", useSQLite: true } },
      outboundService: () => new Response("rete non disponibile", { status: 599 }),
      cf: false,
    }),
  );
  await mf.ready;
  return {
    chiama: chiamante(token, (u, i) => mf.dispatchFetch(`http://localhost${u}`, i)),
    chiudi: () => mf.dispose(),
  };
}

function chiamante(token: string, f: (path: string, init: RequestInit) => Promise<Response>): Chiama {
  return async (path, body = {}) => {
    const t0 = performance.now();
    const r = await f(path, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const text = await r.text();
    const ms = performance.now() - t0;
    let doc: Record<string, unknown>;
    try {
      doc = JSON.parse(text);
    } catch {
      doc = { testo: text.slice(0, 200) };
    }
    return { status: r.status, doc, ms };
  };
}

const esito = (r: Risposta) =>
  r.status === 200
    ? "ok"
    : `${r.status} ${String(r.doc.errore ?? "")} ${String(r.doc.messaggio ?? r.doc.testo ?? "")}`.trim();

/** La sequenza di misura: azzera, carica a blocchi, poi ogni operazione `ripetizioni` volte. */
export async function misura(
  chiama: Chiama,
  opts: { movimenti: number; blocco: number; ripetizioni: number; ricaricaOgni?: number },
): Promise<{ righe: Riga[]; stato: Record<string, unknown> }> {
  const righe: Riga[] = [];
  const registra = (operazione: string, rs: Risposta[]) => {
    const ok = rs.filter((r) => r.status === 200);
    const interni = ok.map((r) => r.doc.ms_interni).filter((x): x is number => typeof x === "number");
    righe.push({
      operazione,
      esito: ok.length === rs.length ? "ok" : esito(rs.find((r) => r.status !== 200) as Risposta),
      ms_mediana: r1(mediana(rs.map((r) => r.ms))),
      ms_min: r1(Math.min(...rs.map((r) => r.ms))),
      ms_interni: interni.length > 0 ? r1(mediana(interni)) : null,
      volte: rs.length,
    });
  };
  const azzera = await chiama("/misure/azzera", {
    movimenti: opts.movimenti,
    ricarica_ogni_giorni: opts.ricaricaOgni ?? 7,
  });
  registra("azzera (schema + conti + regole)", [azzera]);
  if (azzera.status !== 200) return { righe, stato: azzera.doc };

  const blocchi: Risposta[] = [];
  for (let da = 0; da < opts.movimenti; da += opts.blocco) {
    const r = await chiama("/misure/carica", { da, quanti: Math.min(opts.blocco, opts.movimenti - da) });
    blocchi.push(r);
    if (r.status !== 200) break;
  }
  registra(`carica a blocchi da ${opts.blocco} (per blocco)`, blocchi);

  const volte = async (n: number, body: Record<string, unknown>) => {
    const rs: Risposta[] = [];
    for (let i = 0; i < n; i++) rs.push(await chiama("/misure/esegui", body));
    return rs;
  };
  registra("classificazione (prima volta)", await volte(1, { operazione: "classifica" }));
  registra("classificazione (rilettura)", await volte(opts.ripetizioni, { operazione: "classifica" }));
  registra("trasferimenti (prima volta)", await volte(1, { operazione: "trasferimenti" }));
  registra("trasferimenti (rilettura)", await volte(opts.ripetizioni, { operazione: "trasferimenti" }));
  registra("feed Money (12 letture + 12 mesi)", await volte(opts.ripetizioni, { operazione: "feed" }));
  for (const nome of [
    "accounts",
    "overview",
    "summary",
    "summary_12_mesi",
    "categories",
    "planning",
    "balances",
    "data_status",
    "obligations",
    "transactions",
    "manual",
    "budgets",
    "daily",
    "search",
  ])
    registra(`consulta ${nome}`, await volte(opts.ripetizioni, { operazione: "consulta", nome }));
  await chiama("/misure/esegui", { operazione: "backup" }); // la prima volta genera le chiavi di prova
  registra("backup L1 cifrato (tutto il motore)", await volte(opts.ripetizioni, { operazione: "backup" }));
  const stato = await chiama("/misure/esegui", { operazione: "stato" });
  return { righe, stato: (stato.doc.esito as Record<string, unknown>) ?? stato.doc };
}

/** Ripristino a un istante: segnalibro → scrittura → ripristino al segnalibro → riavvio → la scrittura non c'e' piu'. */
export async function provaPitr(chiama: Chiama): Promise<Record<string, unknown>> {
  const passi: Record<string, unknown> = {};
  const s = await chiama("/misure/pitr/segnalibro");
  passi.segnalibro = esito(s);
  passi.segnalibro_di_un_minuto_fa = esito(
    await chiama("/misure/pitr/segnalibro", { istante_ms: Date.now() - 60_000 }),
  );
  const segnalibro = s.doc.segnalibro;
  if (s.status !== 200 || typeof segnalibro !== "string") return { esito: "pitr_non_disponibile", passi };
  const w = await chiama("/misure/pitr/scrivi");
  const p = await chiama("/misure/pitr/prepara", { segnalibro });
  passi.prepara = esito(p);
  if (p.status !== 200) return { esito: "pitr_non_disponibile", passi };
  passi.riavvia = esito(await chiama("/misure/pitr/riavvia"));
  const dopo = await chiama("/misure/pitr/leggi");
  const tolta = dopo.status === 200 && dopo.doc.marca !== w.doc.marca;
  // e ritorno: il segnalibro «annulla» rimette la scrittura
  await chiama("/misure/pitr/prepara", { segnalibro: p.doc.annulla });
  await chiama("/misure/pitr/riavvia");
  const ritorno = await chiama("/misure/pitr/leggi");
  return {
    esito: tolta && ritorno.doc.marca === w.doc.marca ? "pitr_funziona" : "pitr_inatteso",
    passi,
    scrittura_tolta_dal_ripristino: tolta,
    scrittura_tornata_con_annulla: ritorno.doc.marca === w.doc.marca,
  };
}

export function tabella(righe: readonly Riga[]): string {
  const w = Math.max(...righe.map((r) => r.operazione.length));
  return [
    `${"operazione".padEnd(w)}  mediana ms  min ms  interni ms  volte  esito`,
    ...righe.map(
      (r) =>
        `${r.operazione.padEnd(w)}  ${String(r.ms_mediana).padStart(10)}  ${String(r.ms_min).padStart(6)}  ${String(r.ms_interni ?? "-").padStart(10)}  ${String(r.volte).padStart(5)}  ${r.esito}`,
    ),
  ].join("\n");
}

if (import.meta.main) {
  const { values } = parseArgs({
    options: {
      url: { type: "string" },
      movimenti: { type: "string", default: "5000" },
      blocco: { type: "string", default: "500" },
      ripetizioni: { type: "string", default: "3" },
      "ricarica-ogni": { type: "string", default: "7" },
      pitr: { type: "boolean", default: false },
      json: { type: "boolean", default: false },
    },
  });
  const opts = {
    movimenti: Number(values.movimenti),
    blocco: Number(values.blocco),
    ripetizioni: Number(values.ripetizioni),
    ricaricaOgni: Number(values["ricarica-ogni"]),
  };
  let banco: { chiama: Chiama; chiudi: () => Promise<void> };
  if (values.url !== undefined) {
    const base = new URL(values.url);
    const token = process.env.MISURE_TOKEN ?? "";
    if (base.protocol !== "https:" || token === "") {
      console.error("serve --url https://... e il token del banco in MISURE_TOKEN (mai scritto a Claude)");
      process.exit(1);
    }
    banco = { chiama: chiamante(token, (p, i) => fetch(new URL(p, base), i)), chiudi: async () => {} };
  } else banco = await bancoLocale();
  try {
    const dove =
      values.url === undefined ? "locale (Miniflare/workerd, nessun limite di CPU applicato)" : values.url;
    const m = await misura(banco.chiama, opts);
    const pitr = values.pitr || values.url === undefined ? await provaPitr(banco.chiama) : undefined;
    if (values.json) console.log(JSON.stringify({ dove, ...opts, ...m, pitr }, null, 2));
    else {
      console.log(`Banco di misura — ${dove} — ${opts.movimenti} movimenti sintetici\n`);
      console.log(tabella(m.righe));
      console.log(`\nstato finale: ${JSON.stringify(m.stato)}`);
      if (pitr !== undefined) console.log(`PITR: ${JSON.stringify(pitr)}`);
    }
  } finally {
    await banco.chiudi();
  }
}
