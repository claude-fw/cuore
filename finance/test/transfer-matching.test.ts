/**
 * Prova di equivalenza del rilevamento dei trasferimenti (findTransferPairs): il nuovo algoritmo indicizzato deve dare
 * ESATTAMENTE l'uscita di quello di prima (confronto tutti-contro-tutti, copiato qui sotto come riferimento): stesse
 * proposte nello stesso ordine, stesse ambigue con le stesse candidate. Solo dati SINTETICI generati in modo
 * deterministico. Il caso piu' pesante (5000 movimenti con una ricarica al giorno: ~20 s col riferimento) gira solo con
 * CUORE_PROVA_LUNGA=1.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  findTransferPairs,
  type MatchResult,
  type MatchTxn,
  TRANSFER_WINDOW_DAYS,
} from "../src/domain/matching.ts";
import { daysBetween } from "../src/domain/text.ts";

/** Riferimento: findTransferPairs com'era fino al 05/11/2026 (crescita cubica), invariato. */
function referenceTransferPairs(
  txns: readonly MatchTxn[],
  excluded: ReadonlySet<string>,
  rejectedPairs: ReadonlySet<string>,
): MatchResult {
  const proposals: { fromId: string; toId: string }[] = [];
  const paired = new Set<string>();
  const valid = txns.filter((t) => !excluded.has(t.id) && t.amount_minor !== 0);
  const bySourceId = new Map<string, MatchTxn[]>();
  for (const t of valid) {
    if (t.source_txn_id == null) continue;
    bySourceId.set(t.source_txn_id, [...(bySourceId.get(t.source_txn_id) ?? []), t]);
  }
  for (const legs of bySourceId.values()) {
    const [a, b] = legs;
    if (legs.length !== 2 || a === undefined || b === undefined) continue;
    const [o, i] = a.amount_minor < 0 ? [a, b] : [b, a];
    if (
      o.account_id !== i.account_id &&
      o.currency === i.currency &&
      o.amount_minor === -i.amount_minor &&
      Math.abs(daysBetween(o.date, i.date)) <= TRANSFER_WINDOW_DAYS &&
      !rejectedPairs.has(`${o.id}>${i.id}`)
    ) {
      proposals.push({ fromId: o.id, toId: i.id });
      paired.add(o.id);
      paired.add(i.id);
    }
  }
  const usable = valid.filter((t) => !paired.has(t.id));
  const outs = usable.filter((t) => t.amount_minor < 0);
  const ins = usable.filter((t) => t.amount_minor > 0);
  const candidatesOf = (o: MatchTxn) =>
    ins.filter(
      (i) =>
        i.account_id !== o.account_id &&
        i.currency === o.currency &&
        i.amount_minor === -o.amount_minor &&
        Math.abs(daysBetween(o.date, i.date)) <= TRANSFER_WINDOW_DAYS &&
        !rejectedPairs.has(`${o.id}>${i.id}`),
    );
  const ambiguous: { id: string; candidates: string[] }[] = [];
  for (const o of outs) {
    const cands = candidatesOf(o);
    if (cands.length === 0) continue;
    const only = cands[0];
    if (cands.length === 1 && only !== undefined) {
      const reverse = outs.filter((o2) => candidatesOf(o2).some((c) => c.id === only.id));
      if (reverse.length === 1) {
        proposals.push({ fromId: o.id, toId: only.id });
        continue;
      }
    }
    ambiguous.push({ id: o.id, candidates: cands.map((c) => c.id) });
  }
  return { proposals, ambiguous };
}

/** Generatore pseudo-casuale deterministico (mulberry32). */
function rng(seed: number) {
  let s = seed >>> 0;
  const next = () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const int = (n: number) => Math.floor(next() * n);
  const pick = <T>(xs: readonly T[]) => xs[int(xs.length)] as T;
  return { next, int, pick };
}

const day = (base: string, n: number) =>
  new Date(Date.parse(`${base}T00:00:00Z`) + n * 86_400_000).toISOString().slice(0, 10);
const txn = (
  id: string,
  account_id: string,
  amount_minor: number,
  date: string,
  source_txn_id: string | null = null,
  currency = "EUR",
): MatchTxn => ({
  id,
  account_id,
  amount_minor,
  currency,
  date,
  description: "Movimento DEMO",
  counterparty_norm: null,
  source_txn_id,
});

interface Case {
  name: string;
  txns: MatchTxn[];
  excluded: Set<string>;
  rejected: Set<string>;
}

/** Insiemi piccoli e pieni di collisioni: pochi conti, importi, giorni; id della fonte ripetuti; date non valide. */
function smallCase(seed: number): Case {
  const r = rng(seed);
  const n = r.int(60);
  const txns: MatchTxn[] = [];
  for (let k = 0; k < n; k++) {
    const sign = r.next() < 0.5 ? -1 : 1;
    const amount = r.next() < 0.05 ? 0 : sign * r.pick([100, 200, 300, 1250]);
    const date = r.next() < 0.03 ? r.pick(["2026-02-30x", "", "non-data"]) : day("2026-01-01", r.int(14));
    const sid = r.next() < 0.3 ? `g${r.int(8)}` : null;
    txns.push(
      txn(
        `t${seed}-${k}`,
        r.pick(["conto", "carta", "deposito"]),
        amount,
        date,
        sid,
        r.pick(["EUR", "EUR", "USD"]),
      ),
    );
  }
  const ids = txns.map((t) => t.id);
  const excluded = new Set(ids.filter(() => r.next() < 0.05));
  const rejected = new Set<string>();
  for (let k = 0; k < n / 4; k++) rejected.add(`${r.pick(ids)}>${r.pick(ids)}`);
  return { name: `piccolo ${seed} (${n} movimenti)`, txns, excluded, rejected };
}

/**
 * Come il banco di misura (cloud/src/misure.ts): 8 movimenti al giorno su conto e carta, ricarica conto -> carta ogni
 * `ricaricaOgni` giorni; in piu' casi ambigui (stessa ricarica anche sul deposito, o due uscite uguali), importi
 * ripetuti (50,00 frequenti), giroconti con id della fonte, rifiuti ed esclusioni, ordine facoltativamente mescolato.
 */
function benchCase(n: number, ricaricaOgni: number, shuffle = false): Case {
  const r = rng(n * 31 + ricaricaOgni);
  const txns: MatchTxn[] = [];
  for (let i = 0; i < n; i++) {
    const g = Math.floor(i / 8);
    const d = day("2025-01-01", g);
    const c = 100 + ((i * 7919) % 9000);
    const ric = g % ricaricaOgni === 0 ? 2000 + ((g * 37) % 5000) : null;
    const k = i % 8;
    const id = `B-${i}`;
    if (k === 4 && ric !== null) txns.push(txn(id, "conto", -ric, d));
    else if (k === 5 && ric !== null) {
      txns.push(txn(id, "carta", ric, day("2025-01-01", g + r.int(3))));
      if (g % 5 === 0) txns.push(txn(`${id}-dep`, "deposito", ric, d)); // due entrate possibili: ambigua
      if (g % 11 === 0) txns.push(txn(`${id}-bis`, "risparmio", -ric, d)); // due uscite verso la stessa entrata
    } else if (k === 6 && r.next() < 0.3)
      txns.push(txn(id, r.pick(["conto", "carta"]), r.pick([-5000, 5000]), d));
    else if (k === 7 && g % 4 === 0) txns.push(txn(id, "conto", c * 20, d));
    else if (k === 3 && g % 9 === 0) {
      txns.push(txn(id, "conto", -c, d, `giro-${g}`));
      txns.push(txn(`${id}-in`, "deposito", c, d, `giro-${g}`));
    } else txns.push(txn(id, r.pick(["conto", "carta"]), -c, d));
  }
  if (shuffle)
    for (let k = txns.length - 1; k > 0; k--) {
      const j = r.int(k + 1);
      [txns[k], txns[j]] = [txns[j] as MatchTxn, txns[k] as MatchTxn];
    }
  const outs = txns.filter((t) => t.amount_minor < 0);
  const ins = txns.filter((t) => t.amount_minor > 0);
  const rejected = new Set<string>();
  for (let k = 0; k < n / 200; k++) rejected.add(`${r.pick(outs).id}>${r.pick(ins).id}`);
  const excluded = new Set(txns.filter(() => r.next() < 0.01).map((t) => t.id));
  return {
    name: `banco ${n} movimenti, ricarica ogni ${ricaricaOgni} g${shuffle ? ", ordine mescolato" : ""}`,
    txns,
    excluded,
    rejected,
  };
}

function assertSame(c: Case): MatchResult {
  const expected = referenceTransferPairs(c.txns, c.excluded, c.rejected);
  const actual = findTransferPairs(c.txns, c.excluded, c.rejected);
  assert.deepStrictEqual(actual, expected, c.name);
  return actual;
}

describe("trasferimenti: nuovo algoritmo identico al riferimento", () => {
  it("300 insiemi piccoli con collisioni, id della fonte ripetuti, date non valide, rifiuti ed esclusioni", () => {
    let proposals = 0;
    let ambiguous = 0;
    for (let seed = 1; seed <= 300; seed++) {
      const out = assertSame(smallCase(seed));
      proposals += out.proposals.length;
      ambiguous += out.ambiguous.length;
    }
    // gli insiemi esercitano davvero entrambi gli esiti
    assert.ok(proposals > 100 && ambiguous > 100, `${proposals} proposte, ${ambiguous} ambigue`);
  });

  it("finestra di date ai bordi (3 giorni si', 4 no, in entrambe le direzioni) e valute diverse", () => {
    const txns = [
      txn("o1", "conto", -700, "2026-03-10"),
      txn("i1", "carta", 700, "2026-03-13"),
      txn("o2", "conto", -800, "2026-03-10"),
      txn("i2", "carta", 800, "2026-03-14"),
      txn("o3", "conto", -900, "2026-03-10"),
      txn("i3", "carta", 900, "2026-03-07"),
      txn("o4", "conto", -950, "2026-03-10"),
      txn("i4", "carta", 950, "2026-03-06"),
      txn("o5", "conto", -990, "2026-03-10"),
      txn("i5", "carta", 990, "2026-03-10", null, "USD"),
    ];
    const out = assertSame({ name: "bordi", txns, excluded: new Set(), rejected: new Set() });
    assert.deepEqual(out.proposals, [
      { fromId: "o1", toId: "i1" },
      { fromId: "o3", toId: "i3" },
    ]);
  });

  for (const [n, ogni, shuffle] of [
    [1300, 7, false],
    [1300, 1, false],
    [1300, 1, true],
    [5000, 7, false],
    [5000, 7, true],
  ] as const)
    it(`banco: ${n} movimenti, ricarica ogni ${ogni} giorni${shuffle ? ", ordine mescolato" : ""}`, () => {
      const out = assertSame(benchCase(n, ogni, shuffle));
      assert.ok(out.proposals.length > 0 && out.ambiguous.length > 0);
    });

  it("banco: 5000 movimenti, ricarica ogni giorno (solo con CUORE_PROVA_LUNGA=1)", {
    skip: process.env.CUORE_PROVA_LUNGA === "1" ? false : "riferimento lento (~20 s): CUORE_PROVA_LUNGA=1",
  }, () => {
    assertSame(benchCase(5000, 1));
    assertSame(benchCase(5000, 1, true));
  });
});

describe("trasferimenti: tempi indicativi del nuovo algoritmo", () => {
  it("5000 movimenti con una ricarica al giorno ben sotto 300 ms; 50000 stampati", () => {
    const time = (c: Case) => {
      const t0 = performance.now();
      findTransferPairs(c.txns, c.excluded, c.rejected);
      return performance.now() - t0;
    };
    const c5 = benchCase(5000, 1);
    time(c5); // riscaldamento
    const ms5 = time(c5);
    const ms50 = time(benchCase(50_000, 1));
    console.log(
      `findTransferPairs: 5000 movimenti ${ms5.toFixed(1)} ms; 50000 movimenti ${ms50.toFixed(1)} ms`,
    );
    assert.ok(ms5 < 300, `${ms5} ms`);
  });
});
