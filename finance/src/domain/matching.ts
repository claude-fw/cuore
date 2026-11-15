/**
 * Proposte di collegamento tra transazioni (funzioni pure). Producono SOLO proposte: la conferma e' dell'utente.
 */
import { daysBetween, normalizeText } from "./text.ts";

export interface MatchTxn {
  id: string;
  account_id: string;
  amount_minor: number;
  currency: string;
  date: string;
  description: string;
  counterparty_norm: string | null;
  /** identificativo della fonte (facoltativo): due gambe con lo stesso id sono la stessa operazione interna */
  source_txn_id?: string | null;
}

export interface Proposal {
  fromId: string;
  toId: string;
}

export interface MatchResult {
  proposals: Proposal[];
  /** transazione -> candidate multiple: nessuna proposta, solo da verificare */
  ambiguous: { id: string; candidates: string[] }[];
}

export const TRANSFER_WINDOW_DAYS = 3;
export const REFUND_WINDOW_DAYS = 90;
export const REFUND_WORDS = ["rimborso", "refund", "storno", "reversal", "chargeback", "reso"] as const;

/**
 * Trasferimenti tra conti propri: uscita da un conto e entrata in un ALTRO conto, stesso importo assoluto, stessa
 * valuta, date entro TRANSFER_WINDOW_DAYS. Proposta solo se l'abbinamento e' univoco da entrambi i lati.
 * Valute diverse non vengono abbinate (il cambio non e' noto con certezza).
 * Prima fase: le gambe con lo STESSO identificativo della fonte (es. giroconti interni Revolut) sono abbinate tra
 * loro per prime, se la coppia e' unica; escono poi dal confronto per importo e data (che altrimenti, con molti
 * giroconti identici nello stesso giorno, resterebbe ambiguo). Restano comunque solo proposte.
 * Seconda fase in tempo quasi lineare (n log n): le entrate sono indicizzate per valuta e importo e ordinate per data,
 * cosi' ogni uscita guarda solo le entrate della sua finestra; il controllo «univoca anche dal lato dell'entrata» usa
 * un conteggio fatto una volta sola. Stesse coppie, stesso ordine e stesse ambigue del confronto tutti-contro-tutti
 * di prima (prova di equivalenza in test/transfer-matching.test.ts).
 */
export function findTransferPairs(
  txns: readonly MatchTxn[],
  excluded: ReadonlySet<string>,
  rejectedPairs: ReadonlySet<string>,
): MatchResult {
  const proposals: Proposal[] = [];
  const paired = new Set<string>();
  const valid = txns.filter((t) => !excluded.has(t.id) && t.amount_minor !== 0);
  const bySourceId = new Map<string, MatchTxn[]>();
  for (const t of valid) {
    if (t.source_txn_id == null) continue;
    const legs = bySourceId.get(t.source_txn_id);
    if (legs === undefined) bySourceId.set(t.source_txn_id, [t]);
    else legs.push(t);
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
  // stesso calcolo di daysBetween, con le date lette una volta sola (data non valida = NaN = mai nella finestra)
  const day = (t: MatchTxn) => Date.parse(`${t.date}T00:00:00Z`);
  const offset = (from: number, to: number) => Math.round((to - from) / 86_400_000);
  type Leg = { t: MatchTxn; n: number; at: number };
  const index = new Map<string, Map<number, Leg[]>>();
  usable.forEach((t, n) => {
    const at = day(t);
    if (!(t.amount_minor > 0) || Number.isNaN(at)) return;
    const byAmount = index.get(t.currency) ?? new Map<number, Leg[]>();
    index.set(t.currency, byAmount);
    const legs = byAmount.get(t.amount_minor) ?? [];
    byAmount.set(t.amount_minor, legs);
    legs.push({ t, n, at });
  });
  for (const byAmount of index.values())
    for (const legs of byAmount.values()) legs.sort((x, y) => x.at - y.at || x.n - y.n);
  const W = TRANSFER_WINDOW_DAYS;
  const candidatesOf = (o: MatchTxn): MatchTxn[] => {
    const legs = index.get(o.currency)?.get(-o.amount_minor);
    const at = day(o);
    if (legs === undefined || Number.isNaN(at)) return [];
    let lo = 0;
    let hi = legs.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (offset(at, (legs[mid] as Leg).at) < -W) lo = mid + 1;
      else hi = mid;
    }
    const found: Leg[] = [];
    for (let k = lo; k < legs.length && offset(at, (legs[k] as Leg).at) <= W; k++) {
      const leg = legs[k] as Leg;
      if (leg.t.account_id !== o.account_id && !rejectedPairs.has(`${o.id}>${leg.t.id}`)) found.push(leg);
    }
    return found.sort((x, y) => x.n - y.n).map((l) => l.t);
  };
  const outs = usable.filter((t) => t.amount_minor < 0).map((o) => ({ o, cands: candidatesOf(o) }));
  // per ogni entrata: quante uscite la hanno tra le candidate
  const reverse = new Map<string, number>();
  for (const { cands } of outs)
    for (const id of new Set(cands.map((c) => c.id))) reverse.set(id, (reverse.get(id) ?? 0) + 1);
  const ambiguous: { id: string; candidates: string[] }[] = [];
  for (const { o, cands } of outs) {
    if (cands.length === 0) continue;
    const only = cands[0];
    if (cands.length === 1 && only !== undefined && reverse.get(only.id) === 1) {
      proposals.push({ fromId: o.id, toId: only.id });
      continue;
    }
    ambiguous.push({ id: o.id, candidates: cands.map((c) => c.id) });
  }
  return { proposals, ambiguous };
}

/**
 * Rimborsi: entrata con parola di rimborso nella descrizione e stessa controparte (normalizzata, non vuota) di una
 * spesa precedente nello stesso conto, entro REFUND_WINDOW_DAYS, con importo non superiore alla spesa.
 */
export function findRefundPairs(
  txns: readonly MatchTxn[],
  excluded: ReadonlySet<string>,
  rejectedPairs: ReadonlySet<string>,
): MatchResult {
  const usable = txns.filter((t) => !excluded.has(t.id));
  const proposals: Proposal[] = [];
  const ambiguous: { id: string; candidates: string[] }[] = [];
  for (const inflow of usable) {
    if (inflow.amount_minor <= 0 || inflow.counterparty_norm === null) continue;
    const text = normalizeText(inflow.description);
    if (!REFUND_WORDS.some((w) => text.includes(w))) continue;
    const cands = usable.filter(
      (o) =>
        o.amount_minor < 0 &&
        o.account_id === inflow.account_id &&
        o.currency === inflow.currency &&
        o.counterparty_norm === inflow.counterparty_norm &&
        -o.amount_minor >= inflow.amount_minor &&
        daysBetween(o.date, inflow.date) >= 0 &&
        daysBetween(o.date, inflow.date) <= REFUND_WINDOW_DAYS &&
        !rejectedPairs.has(`${o.id}>${inflow.id}`),
    );
    const only = cands[0];
    if (cands.length === 1 && only !== undefined) proposals.push({ fromId: only.id, toId: inflow.id });
    else if (cands.length > 1) ambiguous.push({ id: inflow.id, candidates: cands.map((c) => c.id) });
  }
  return { proposals, ambiguous };
}
