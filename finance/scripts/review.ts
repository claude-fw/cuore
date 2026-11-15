#!/usr/bin/env node
/**
 * REVISIONE ASSISTITA (nessuna scrittura nel database).
 *
 *   node scripts/review.ts export --db <database.sqlite3> --out <cartella>       # file di decisioni + scheda leggibile
 *   node scripts/review.ts check  --db <database.sqlite3> --decisions <file.json> # validazione + simulazione su copia
 *
 * `export` elenca, dal database in sola lettura:
 *   - i movimenti contabilizzati ancora "da classificare" (uno per uno: data, importo, conto, descrizione originale,
 *     evidenze, proposta motivata SOLO quando una regola l'ha formulata a bassa confidenza);
 *   - le proposte di trasferimento non confermate e i casi ambigui, separatamente, senza confermarli.
 * Ogni decisione nel file parte vuota (`null` = resta da classificare). Il file contiene dati reali: va scritto SOLO
 * nella cartella protetta (permessi 700/600), mai nel repository.
 *
 * `check` valida un file di decisioni compilato e ne simula l'effetto su una COPIA temporanea del database
 * (classificazioni dell'utente, conferma o rifiuto delle proposte): mostra cosa cambierebbe nel riepilogo. Il database
 * indicato non viene mai modificato; l'applicazione reale e' un passo separato con backup.
 */
import { createHash } from "node:crypto";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { parseArgs } from "node:util";
import { createCtx } from "../src/context.ts";
import { NodeSqliteDb } from "../src/db/node-sqlite.ts";
import { KINDS, type RuleKind } from "../src/domain/rules.ts";
import { normalizeText } from "../src/domain/text.ts";
import { runQuery } from "../src/query/operations.ts";
import { setClassification } from "../src/services/classification.ts";
import { EFFECTIVE_COLUMNS, EFFECTIVE_FROM } from "../src/services/effective.ts";
import { decideLink } from "../src/services/links.ts";

export const REVIEW_SCHEMA = "cloud-finance-review/1";

interface Txn {
  id: string;
  account_id: string;
  booking_date: string;
  transaction_date: string;
  amount_minor: number;
  currency: string;
  description: string;
  counterparty: string | null;
}

export interface MovementItem {
  id: string;
  conto: string;
  data: string;
  dataValuta: string;
  importoMinor: number;
  valuta: string;
  descrizioneOriginale: string;
  controparte: string | null;
  evidenze: string[];
  proposta: { tipo: string; categoria: string | null; motivo: string } | null;
  /** abbinamento o caso ambiguo che coinvolge questo movimento (da decidere nella sezione dedicata) */
  vediAnche: string | null;
  /** null = resta da classificare; altrimenti { tipo, categoria, nota } */
  decisione: { tipo: RuleKind; categoria: string | null; nota?: string } | null;
}

export interface LinkItem {
  id: string;
  criterio: string;
  da: { id: string; conto: string; data: string; importoMinor: number; descrizioneOriginale: string };
  a: { id: string; conto: string; data: string; importoMinor: number; descrizioneOriginale: string };
  evidenze: string[];
  /** null = resta proposta; "conferma" | "rifiuta" */
  decisione: "conferma" | "rifiuta" | null;
  nota?: string;
}

export interface AmbiguousItem {
  movimento: { id: string; conto: string; data: string; importoMinor: number; descrizioneOriginale: string };
  candidati: {
    id: string;
    conto: string;
    data: string;
    importoMinor: number;
    descrizioneOriginale: string;
  }[];
  evidenze: string[];
  /** null = resta ambiguo; { scegli: <id candidato> } | "nessuno" (applicazione: servizio dedicato, passo successivo) */
  decisione: { scegli: string } | "nessuno" | null;
  nota?: string;
}

export interface ReviewFile {
  schema: typeof REVIEW_SCHEMA;
  generatoIl: string;
  database: { improntaFile: string; aggiornatoAl: string | null };
  istruzioni: string[];
  categorie: { id: string; nome: string }[];
  movimenti: MovementItem[];
  abbinamentiProposti: LinkItem[];
  casiAmbigui: AmbiguousItem[];
}

/** Evidenze dal testo originale: SOLO fatti leggibili nella descrizione, nessuna deduzione sullo scopo. */
function textEvidence(description: string): string[] {
  const t = normalizeText(description);
  const e: string[] = [];
  if (t.includes("caus: 048")) e.push("bonifico SEPA disposto (causale bancaria 048)");
  if (t.includes("caus: 034") || t.includes("giroconto da/su conto dello stesso nominativo"))
    e.push("giroconto (causale 034) verso un conto che la banca indica come 'stesso nominativo'");
  if (t.includes("saldo debiti") || t.includes("rimborso quote"))
    e.push("la causale indica il saldo di debiti: possibile credito/debito da registrare (non deciso)");
  if (t.startsWith("pagamento a favore di"))
    e.push("pagamento verso una persona: spesa, regalo, rimborso o debito non determinabili dall'estratto");
  if (t.startsWith("ricarica di")) e.push("ricarica da una carta esterna non identificata nell'estratto");
  if (t.startsWith("prelievo di contanti"))
    e.push("prelievo di contanti: denaro spostato in contanti, spesa non dimostrata");
  if (t.startsWith("pagamento da parte di")) e.push("entrata da una persona indicata nella descrizione");
  return e;
}

const fileSha = (path: string) => createHash("sha256").update(readFileSync(path)).digest("hex");

export async function buildReview(dbPath: string): Promise<ReviewFile> {
  const db = new NodeSqliteDb(dbPath, { readOnly: true });
  try {
    const rows = await db.all<
      Txn & {
        effective_kind: string;
        excluded: number;
        rule_id: string | null;
        reason: string | null;
        rule_set_version: number | null;
      }
    >(
      `SELECT t.id, t.account_id, t.booking_date, t.transaction_date, t.amount_minor, t.currency, t.description,
              t.counterparty, c.rule_id, c.reason, c.rule_set_version, ${EFFECTIVE_COLUMNS}
       FROM ${EFFECTIVE_FROM} WHERE t.status = 'booked' ORDER BY effective_date, t.account_id, t.id`,
    );
    const byId = new Map(rows.map((r) => [r.id, r]));
    const rules = await db.all<{
      rule_set_version: number;
      rule_id: string;
      description: string;
      kind: string;
      category_id: string | null;
    }>("SELECT rule_set_version, rule_id, description, kind, category_id FROM rules");
    const links = await db.all<{ id: string; from_txn_id: string; to_txn_id: string }>(
      "SELECT id, from_txn_id, to_txn_id FROM transaction_links WHERE type = 'transfer' AND status = 'proposed' ORDER BY created_at, id",
    );
    const ambiguous = await db.all<{ entity_id: string; detail_json: string }>(
      "SELECT entity_id, detail_json FROM review_items WHERE kind = 'transfer_ambiguous' AND status = 'open' ORDER BY created_at, id",
    );
    const categories = await db.all<{ id: string; name: string }>(
      "SELECT id, name FROM categories ORDER BY id",
    );
    const last = await db.first<{ d: string | null }>("SELECT MAX(booking_date) AS d FROM transactions");
    const brief = (id: string) => {
      const r = byId.get(id);
      return {
        id,
        conto: r?.account_id ?? "?",
        data: r?.booking_date ?? "?",
        importoMinor: r?.amount_minor ?? 0,
        descrizioneOriginale: r?.description ?? "?",
      };
    };
    const seeAlso = new Map<string, string>();
    const linkItems: LinkItem[] = links.map((l, n) => {
      const ref = `abbinamento ${n + 1}`;
      seeAlso.set(l.from_txn_id, ref);
      seeAlso.set(l.to_txn_id, ref);
      const from = byId.get(l.from_txn_id);
      const to = byId.get(l.to_txn_id);
      const days =
        from && to
          ? Math.round((Date.parse(to.booking_date) - Date.parse(from.booking_date)) / 86_400_000)
          : null;
      return {
        id: l.id,
        criterio: "solo importo e data (nessun identificativo condiviso)",
        da: brief(l.from_txn_id),
        a: brief(l.to_txn_id),
        evidenze: [
          `stesso importo assoluto, ${days === null ? "?" : Math.abs(days)} giorni di distanza tra le date contabili, conti di istituti diversi`,
          ...textEvidence(from?.description ?? ""),
          "l'uguaglianza di importo e la vicinanza temporale da sole non dimostrano un trasferimento tra conti propri",
        ],
        decisione: null,
      };
    });
    const ambiguousItems: AmbiguousItem[] = ambiguous.map((a, n) => {
      const ref = `caso ambiguo ${n + 1}`;
      const candidates = (JSON.parse(a.detail_json) as { candidate?: string[] }).candidate ?? [];
      seeAlso.set(a.entity_id, ref);
      for (const c of candidates) if (!seeAlso.has(c)) seeAlso.set(c, ref);
      return {
        movimento: brief(a.entity_id),
        candidati: candidates.map(brief),
        evidenze: [
          `${candidates.length} movimenti compatibili per importo e data: abbinamento non univoco`,
          ...textEvidence(byId.get(a.entity_id)?.description ?? ""),
        ],
        decisione: null,
      };
    });
    const movements: MovementItem[] = rows
      .filter((r) => r.effective_kind === "unknown" && !r.excluded)
      .map((r) => {
        const rule = rules.find((x) => x.rule_id === r.rule_id && x.rule_set_version === r.rule_set_version);
        const evidence = [...textEvidence(r.description)];
        if (rule) evidence.unshift(`regola "${rule.rule_id}": ${rule.description}`);
        return {
          id: r.id,
          conto: r.account_id,
          data: r.booking_date,
          dataValuta: r.transaction_date,
          importoMinor: r.amount_minor,
          valuta: r.currency,
          descrizioneOriginale: r.description,
          controparte: r.counterparty,
          evidenze: evidence.length > 0 ? evidence : ["nessuna evidenza oltre alla descrizione"],
          proposta: rule
            ? {
                tipo: rule.kind,
                categoria: rule.category_id,
                motivo: r.reason ?? "regola a bassa confidenza",
              }
            : null,
          vediAnche: seeAlso.get(r.id) ?? null,
          decisione: null,
        };
      });
    return {
      schema: REVIEW_SCHEMA,
      generatoIl: new Date().toISOString(),
      database: { improntaFile: fileSha(dbPath), aggiornatoAl: last?.d ?? null },
      istruzioni: [
        "Compilare SOLO i campi 'decisione'. null = resta da classificare / proposta / ambiguo.",
        `movimenti: { "tipo": ${KINDS.map((k) => `"${k}"`).join(" | ")}, "categoria": <id da 'categorie'> | null, "nota": "..." }`,
        'abbinamentiProposti: "conferma" | "rifiuta" (un rifiuto non viene riproposto).',
        'casiAmbigui: { "scegli": <id di un candidato> } | "nessuno".',
        "Prima di applicare: node scripts/review.ts check --db <database> --decisions <questo file> (simulazione).",
      ],
      categorie: categories.map((c) => ({ id: c.id, nome: c.name })),
      movimenti: movements,
      abbinamentiProposti: linkItems,
      casiAmbigui: ambiguousItems,
    };
  } finally {
    db.close();
  }
}

const eur = (minor: number) => (minor / 100).toFixed(2).replace(".", ",");

export function renderMarkdown(r: ReviewFile): string {
  const lines = [
    "# Revisione Cloud Finance",
    "",
    `Generato il ${r.generatoIl}; dati aggiornati al ${r.database.aggiornatoAl ?? "?"}. Le decisioni si compilano nel file JSON.`,
    "",
    `## Movimenti da classificare (${r.movimenti.length})`,
  ];
  r.movimenti.forEach((m, n) => {
    lines.push(
      "",
      `### ${n + 1}. ${m.data} — ${eur(m.importoMinor)} ${m.valuta} — ${m.conto}`,
      `- Descrizione originale: ${m.descrizioneOriginale}`,
      ...(m.controparte ? [`- Controparte: ${m.controparte}`] : []),
      ...(m.dataValuta !== m.data ? [`- Data valuta: ${m.dataValuta}`] : []),
      `- Evidenze: ${m.evidenze.join("; ")}`,
      `- Proposta: ${m.proposta ? `${m.proposta.tipo}${m.proposta.categoria ? ` / ${m.proposta.categoria}` : ""} (${m.proposta.motivo})` : "nessuna proposta: evidenza insufficiente"}`,
      ...(m.vediAnche ? [`- Vedi anche: ${m.vediAnche}`] : []),
    );
  });
  lines.push("", `## Abbinamenti proposti, non confermati (${r.abbinamentiProposti.length})`);
  r.abbinamentiProposti.forEach((l, n) => {
    lines.push(
      "",
      `### Abbinamento ${n + 1} (${l.criterio})`,
      `- Da: ${l.da.data} ${eur(l.da.importoMinor)} ${l.da.conto} — ${l.da.descrizioneOriginale}`,
      `- A: ${l.a.data} ${eur(l.a.importoMinor)} ${l.a.conto} — ${l.a.descrizioneOriginale}`,
      `- Evidenze: ${l.evidenze.join("; ")}`,
    );
  });
  lines.push("", `## Casi ambigui (${r.casiAmbigui.length})`);
  r.casiAmbigui.forEach((a, n) => {
    lines.push(
      "",
      `### Caso ambiguo ${n + 1}: ${a.movimento.data} ${eur(a.movimento.importoMinor)} ${a.movimento.conto}`,
      `- Movimento: ${a.movimento.descrizioneOriginale}`,
      ...a.candidati.map(
        (c) => `- Candidato: ${c.data} ${eur(c.importoMinor)} ${c.conto} — ${c.descrizioneOriginale}`,
      ),
      `- Evidenze: ${a.evidenze.join("; ")}`,
    );
  });
  return `${lines.join("\n")}\n`;
}

/** Controlla un file di decisioni compilato (senza database). */
export function validateDecisions(r: ReviewFile, txnSign: (id: string) => number | null): string[] {
  const p: string[] = [];
  if (r.schema !== REVIEW_SCHEMA) p.push(`schema atteso ${REVIEW_SCHEMA}`);
  const cats = new Set(r.categorie.map((c) => c.id));
  for (const m of r.movimenti) {
    const d = m.decisione;
    if (d === null) continue;
    const sign = txnSign(m.id);
    if (sign === null) p.push(`${m.id}: movimento inesistente`);
    if (!KINDS.includes(d.tipo)) p.push(`${m.id}: tipo non ammesso ${String(d.tipo)}`);
    if (d.categoria !== null && !cats.has(d.categoria))
      p.push(`${m.id}: categoria sconosciuta ${d.categoria}`);
    if (sign !== null && (d.tipo === "income" || d.tipo === "refund") && sign < 0)
      p.push(`${m.id}: ${d.tipo} su un'uscita`);
    if (sign !== null && (d.tipo === "expense" || d.tipo === "fee") && sign > 0)
      p.push(`${m.id}: ${d.tipo} su un'entrata`);
  }
  for (const l of r.abbinamentiProposti)
    if (l.decisione !== null && l.decisione !== "conferma" && l.decisione !== "rifiuta")
      p.push(`${l.id}: decisione non ammessa`);
  for (const a of r.casiAmbigui) {
    const d = a.decisione;
    if (d === null || d === "nessuno") continue;
    if (!a.candidati.some((c) => c.id === d.scegli))
      p.push(`${a.movimento.id}: candidato scelto non tra quelli elencati`);
  }
  return p;
}

async function check(dbPath: string, decisionsPath: string): Promise<Record<string, unknown>> {
  const r = JSON.parse(readFileSync(decisionsPath, "utf8")) as ReviewFile;
  const src = new NodeSqliteDb(dbPath, { readOnly: true });
  const signs = new Map<string, number>();
  try {
    for (const t of await src.all<{ id: string; amount_minor: number }>(
      "SELECT id, amount_minor FROM transactions",
    ))
      signs.set(t.id, Math.sign(t.amount_minor));
  } finally {
    src.close();
  }
  const problems = validateDecisions(r, (id) => signs.get(id) ?? null);
  const stale = fileSha(dbPath) !== r.database.improntaFile;
  const counts = {
    movimentiDecisi: r.movimenti.filter((m) => m.decisione !== null).length,
    abbinamentiDecisi: r.abbinamentiProposti.filter((l) => l.decisione !== null).length,
    ambiguiDecisi: r.casiAmbigui.filter((a) => a.decisione !== null).length,
  };
  if (problems.length > 0) return { esito: "NON VALIDO", problemi: problems, ...counts };
  // simulazione su copia temporanea: il database indicato non viene toccato
  const dir = mkdtempSync(join(tmpdir(), "cloud-finance-revisione-"));
  try {
    const copy = join(dir, "copia.sqlite3");
    const ro = new NodeSqliteDb(dbPath, { readOnly: true });
    try {
      await ro.all(`VACUUM INTO '${copy.replaceAll("'", "''")}'`);
    } finally {
      ro.close();
    }
    const db = new NodeSqliteDb(copy);
    const ctx = createCtx(db);
    try {
      const range = { from: "1900-01-01", to: "2999-12-31" };
      const before = await runQuery(db, "overview", range);
      for (const m of r.movimenti)
        if (m.decisione !== null)
          await setClassification(
            ctx,
            m.id,
            m.decisione.tipo,
            m.decisione.categoria,
            m.decisione.nota ?? "decisione di revisione",
          );
      for (const l of r.abbinamentiProposti)
        if (l.decisione !== null)
          await decideLink(ctx, l.id, l.decisione === "conferma" ? "confirmed" : "rejected");
      const after = await runQuery(db, "overview", range);
      const pick = (o: typeof before) =>
        o.ok ? (o.data as { currencies: Record<string, unknown>[] }).currencies : o;
      return {
        esito: "VALIDO (simulato su copia, database invariato)",
        databaseCambiatoDallEsportazione: stale,
        ...counts,
        ambiguiNonSimulati:
          counts.ambiguiDecisi > 0
            ? "la scelta di un candidato richiede il servizio dedicato (passo successivo)"
            : 0,
        riepilogoPrima: pick(before),
        riepilogoDopo: pick(after),
      };
    } finally {
      db.close();
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

async function main(argv: string[]): Promise<number> {
  const { values, positionals } = parseArgs({
    args: argv,
    options: { db: { type: "string" }, out: { type: "string" }, decisions: { type: "string" } },
    allowPositionals: true,
  });
  const cmd = positionals[0];
  if (values.db === undefined || !existsSync(values.db)) {
    process.stderr.write(
      "uso: review.ts export --db <db> --out <cartella> | check --db <db> --decisions <file>\n",
    );
    return 64;
  }
  if (cmd === "export") {
    if (values.out === undefined) return 64;
    const dir = resolve(values.out);
    const previous = process.umask(0o077);
    try {
      mkdirSync(dir, { recursive: true, mode: 0o700 });
      if ((statSync(dir).mode & 0o077) !== 0) chmodSync(dir, 0o700);
      const review = await buildReview(values.db);
      const stamp = review.generatoIl.replace(/[:.]/g, "-");
      const json = join(dir, `revisione-${stamp}.json`);
      const md = join(dir, `revisione-${stamp}.md`);
      writeFileSync(json, `${JSON.stringify(review, null, 2)}\n`, { mode: 0o600 });
      writeFileSync(md, renderMarkdown(review), { mode: 0o600 });
      process.stdout.write(
        `${JSON.stringify({ decisioni: json, scheda: md, movimenti: review.movimenti.length, abbinamenti: review.abbinamentiProposti.length, ambigui: review.casiAmbigui.length })}\n`,
      );
      return 0;
    } finally {
      process.umask(previous);
    }
  }
  if (cmd === "check") {
    if (values.decisions === undefined) return 64;
    const res = await check(values.db, values.decisions);
    process.stdout.write(`${JSON.stringify(res, null, 2)}\n`);
    return res.esito === "NON VALIDO" ? 1 : 0;
  }
  process.stderr.write(`comando sconosciuto: ${cmd ?? "(nessuno)"}\n`);
  return 64;
}

if (import.meta.main) {
  main(process.argv.slice(2)).then(
    (code) => {
      process.exitCode = code;
    },
    (err: unknown) => {
      process.stderr.write(`errore: ${err instanceof Error ? err.message : String(err)}\n`);
      process.exitCode = 2;
    },
  );
}
