/**
 * Crediti e debiti (funzioni PURE). Un'obbligazione non e' cassa: il residuo nasce dagli eventi
 * (apertura + aumenti - pagamenti - condoni +/- rettifiche) e non entra mai in entrate o spese.
 */
import { addMinor } from "./money.ts";

export type Direction = "receivable" | "payable";
export type EventKind = "open" | "increase" | "payment" | "forgive" | "adjust";

export interface ObligationEvent {
  kind: EventKind;
  amountMinor: number;
  occurredOn: string;
}

export interface ObligationState {
  initial: number;
  increased: number;
  paid: number;
  forgiven: number;
  adjusted: number;
  /** importo ancora dovuto (mai negativo: gli eventi che lo renderebbero negativo sono rifiutati prima) */
  residual: number;
  status: "open" | "closed";
  /** solo se aperta, con scadenza passata rispetto ad `asOf` */
  overdue: boolean;
  lastEventOn: string | null;
}

export function obligationState(
  events: readonly ObligationEvent[],
  dueOn: string | null,
  asOf: string,
): ObligationState {
  const s = { initial: 0, increased: 0, paid: 0, forgiven: 0, adjusted: 0 };
  let opens = 0;
  let last: string | null = null;
  for (const e of events) {
    if (last === null || e.occurredOn > last) last = e.occurredOn;
    switch (e.kind) {
      case "open":
        opens += 1;
        s.initial = addMinor(s.initial, e.amountMinor);
        break;
      case "increase":
        s.increased = addMinor(s.increased, e.amountMinor);
        break;
      case "payment":
        s.paid = addMinor(s.paid, e.amountMinor);
        break;
      case "forgive":
        s.forgiven = addMinor(s.forgiven, e.amountMinor);
        break;
      case "adjust":
        s.adjusted = addMinor(s.adjusted, e.amountMinor);
        break;
    }
  }
  if (opens !== 1) throw new Error(`obbligazione con ${opens} eventi di apertura (atteso 1)`);
  const residual = addMinor(
    addMinor(addMinor(s.initial, s.increased), s.adjusted),
    -addMinor(s.paid, s.forgiven),
  );
  if (residual < 0) throw new Error("residuo negativo: storia degli eventi incoerente");
  const status = residual === 0 ? "closed" : "open";
  return {
    ...s,
    residual,
    status,
    overdue: status === "open" && dueOn !== null && dueOn < asOf,
    lastEventOn: last,
  };
}

/** Residuo dopo un evento proposto; `null` se l'evento renderebbe il residuo negativo (va rifiutato). */
export function residualAfter(current: number, kind: EventKind, amountMinor: number): number | null {
  const delta = kind === "payment" || kind === "forgive" ? -amountMinor : amountMinor;
  const next = addMinor(current, delta);
  return next < 0 ? null : next;
}

/**
 * Segno che deve avere un movimento bancario collegato a un evento:
 *   - credito (mi devono): apertura/aumento = ho dato denaro (uscita), pagamento = mi restituiscono (entrata);
 *   - debito (devo): apertura/aumento = ho ricevuto denaro (entrata), pagamento = restituisco (uscita).
 */
export function expectedTransactionSign(direction: Direction, kind: "open" | "increase" | "payment"): 1 | -1 {
  const outgoingForReceivable = kind === "open" || kind === "increase";
  if (direction === "receivable") return outgoingForReceivable ? -1 : 1;
  return outgoingForReceivable ? 1 : -1;
}

export interface PositionLine {
  currency: string;
  receivable: number;
  payable: number;
  /** crediti - debiti */
  net: number;
  openCount: number;
  overdueCount: number;
}

/** Posizione per valuta (mai somme tra valute diverse). */
export function obligationsPosition(
  items: readonly { direction: Direction; currency: string; state: ObligationState }[],
): PositionLine[] {
  const by = new Map<string, PositionLine>();
  for (const it of items) {
    let p = by.get(it.currency);
    if (p === undefined) {
      p = { currency: it.currency, receivable: 0, payable: 0, net: 0, openCount: 0, overdueCount: 0 };
      by.set(it.currency, p);
    }
    if (it.direction === "receivable") p.receivable = addMinor(p.receivable, it.state.residual);
    else p.payable = addMinor(p.payable, it.state.residual);
    if (it.state.status === "open") p.openCount += 1;
    if (it.state.overdue) p.overdueCount += 1;
  }
  for (const p of by.values()) p.net = addMinor(p.receivable, -p.payable);
  return [...by.values()].sort((a, b) => a.currency.localeCompare(b.currency));
}
