/**
 * PIANO di importazione: funzione pura (nessun I/O) che confronta le righe in arrivo con le transazioni esistenti.
 *
 * Identita' (deduplicazione prudente):
 *   - riga con id della fonte  -> stessa transazione solo se (conto, id) coincide;
 *   - riga senza id            -> stessa transazione solo se coincidono impronta E occorrenza.
 *     impronta  = hash(conto, data operazione, importo, valuta, descrizione e controparte normalizzate, importo originale);
 *     occorrenza = posizione tra le righe IDENTICHE dello stesso file (1, 2, ...): due caffe' uguali nello stesso
 *     giorno restano due transazioni, e reimportando lo stesso periodo restano due.
 *   - nient'altro unisce due transazioni. Somiglianze (stesso importo e date vicine, descrizione diversa)
 *     producono solo un COLLEGAMENTO PROPOSTO e un elemento da verificare, mai una fusione.
 *
 * Stato:
 *   - pending esistente + riga pending/booked diversa -> aggiornamento (la pending e' provvisoria);
 *   - booked esistente  + riga diversa                -> conflitto da verificare, nessuna modifica;
 *   - booked esistente  + riga pending                -> regressione da verificare, nessuna modifica.
 */
import { addDays, daysBetween } from "../domain/text.ts";
import type { ParsedTransaction } from "./types.ts";

export interface IncomingTxn extends ParsedTransaction {
  readonly fingerprint: string;
  /** null per le righe con id della fonte. */
  readonly occurrence: number | null;
  readonly descriptionNorm: string;
  readonly counterpartyNorm: string | null;
}

export interface ExistingTxn {
  id: string;
  source_txn_id: string | null;
  fingerprint: string;
  occurrence: number | null;
  status: "pending" | "booked";
  transaction_date: string;
  booking_date: string | null;
  amount_minor: number;
  currency: string;
  original_amount_minor: number | null;
  original_currency: string | null;
  description: string;
  counterparty: string | null;
  source_type: string | null;
}

export type FieldChanges = Record<string, { from: string | number | null; to: string | number | null }>;

export type TxnAction =
  | { type: "create"; incoming: IncomingTxn; id: string }
  | { type: "unchanged"; incoming: IncomingTxn; existing: ExistingTxn }
  | { type: "update"; incoming: IncomingTxn; existing: ExistingTxn; changes: FieldChanges }
  | {
      type: "conflict";
      incoming: IncomingTxn;
      existing: ExistingTxn;
      reason: "source_changed" | "status_regression";
      changes: FieldChanges;
    };

export interface PlannedReview {
  kind: string;
  entityId: string;
  detail: Record<string, unknown>;
}

export interface PlannedLink {
  type: "pending_booked" | "possible_duplicate";
  fromTxnId: string;
  toTxnId: string;
}

export interface ImportPlan {
  actions: TxnAction[];
  reviews: PlannedReview[];
  links: PlannedLink[];
}

export const MATCH_WINDOW = {
  /** giorni entro cui una pending puo' diventare una booked con descrizione diversa */
  pendingToBookedDays: 5,
  /** giorni entro cui due righe con stesso importo sono un possibile doppione */
  possibleDuplicateDays: 3,
} as const;

/** Campi confrontati per riconoscere un cambiamento della fonte. */
const COMPARED: readonly [keyof ExistingTxn, (t: IncomingTxn) => string | number | null][] = [
  ["status", (t) => t.status],
  ["transaction_date", (t) => t.transactionDate],
  ["booking_date", (t) => t.bookingDate],
  ["amount_minor", (t) => t.amountMinor],
  ["original_amount_minor", (t) => t.originalAmountMinor],
  ["original_currency", (t) => t.originalCurrency],
  ["description", (t) => t.description],
  ["counterparty", (t) => t.counterparty],
  ["source_type", (t) => t.sourceType ?? null],
];

/** Per le righe senza id l'impronta copre gia' i campi descrittivi (normalizzati): contano solo stato e contabilizzazione. */
const COMPARED_BY_FINGERPRINT = new Set<keyof ExistingTxn>(["status", "booking_date"]);

function diff(existing: ExistingTxn, incoming: IncomingTxn, byFingerprint: boolean): FieldChanges {
  const changes: FieldChanges = {};
  for (const [field, get] of COMPARED) {
    if (byFingerprint && !COMPARED_BY_FINGERPRINT.has(field)) continue;
    const from = existing[field] as string | number | null;
    const to = get(incoming);
    if (from !== to) changes[field] = { from, to };
  }
  return changes;
}

export function planImport(
  incoming: readonly IncomingTxn[],
  existing: readonly ExistingTxn[],
  newId: () => string,
): ImportPlan {
  const bySourceId = new Map<string, ExistingTxn>();
  const byFingerprint = new Map<string, ExistingTxn>();
  for (const e of existing) {
    if (e.source_txn_id !== null) bySourceId.set(e.source_txn_id, e);
    else byFingerprint.set(`${e.fingerprint}#${e.occurrence}`, e);
  }

  const actions: TxnAction[] = [];
  const reviews: PlannedReview[] = [];
  const links: PlannedLink[] = [];
  const matched = new Set<string>();

  for (const t of incoming) {
    const byFp = t.sourceTxnId === null;
    const ex = byFp
      ? byFingerprint.get(`${t.fingerprint}#${t.occurrence}`)
      : bySourceId.get(t.sourceTxnId ?? "");
    if (ex === undefined) {
      actions.push({ type: "create", incoming: t, id: newId() });
      continue;
    }
    matched.add(ex.id);
    const changes = diff(ex, t, byFp);
    if (Object.keys(changes).length === 0) {
      actions.push({ type: "unchanged", incoming: t, existing: ex });
    } else if (ex.status === "pending") {
      actions.push({ type: "update", incoming: t, existing: ex, changes });
      if (t.status === "booked" && changes.amount_minor !== undefined) {
        reviews.push({
          kind: "amount_changed_on_booking",
          entityId: ex.id,
          detail: { da: ex.amount_minor, a: t.amountMinor, riga: t.line },
        });
      }
    } else {
      const reason = t.status === "pending" ? "status_regression" : "source_changed";
      actions.push({ type: "conflict", incoming: t, existing: ex, reason, changes });
      reviews.push({ kind: reason, entityId: ex.id, detail: { cambiamenti: changes, riga: t.line } });
    }
  }

  if (incoming.length === 0) return { actions, reviews, links };
  const dates = incoming.map((t) => t.transactionDate).sort();
  const fileFrom = dates[0] ?? "";
  const fileTo = dates[dates.length - 1] ?? "";
  const unmatched = existing.filter((e) => !matched.has(e.id));
  const linkedExisting = new Set<string>();

  for (const action of actions) {
    if (action.type !== "create") continue;
    const t = action.incoming;
    const sameMoney = (e: ExistingTxn) => e.amount_minor === t.amountMinor && e.currency === t.currency;

    const pendingCandidates =
      t.status === "booked"
        ? unmatched.filter(
            (e) =>
              e.status === "pending" &&
              sameMoney(e) &&
              e.transaction_date >= addDays(fileFrom, -MATCH_WINDOW.pendingToBookedDays) &&
              Math.abs(daysBetween(e.transaction_date, t.transactionDate)) <=
                MATCH_WINDOW.pendingToBookedDays,
          )
        : [];
    const duplicateCandidates = unmatched.filter(
      (e) =>
        sameMoney(e) &&
        e.transaction_date >= fileFrom &&
        e.transaction_date <= fileTo &&
        Math.abs(daysBetween(e.transaction_date, t.transactionDate)) <= MATCH_WINDOW.possibleDuplicateDays &&
        !pendingCandidates.includes(e),
    );

    const [candidates, type] =
      pendingCandidates.length > 0
        ? [pendingCandidates, "pending_booked" as const]
        : [duplicateCandidates, "possible_duplicate" as const];
    if (candidates.length === 0) continue;
    if (candidates.length === 1 && candidates[0] !== undefined) {
      const c = candidates[0];
      links.push({ type, fromTxnId: c.id, toTxnId: action.id });
      reviews.push({ kind: type, entityId: action.id, detail: { candidata: c.id, riga: t.line } });
      linkedExisting.add(c.id);
    } else {
      reviews.push({
        kind: `${type}_ambiguous`,
        entityId: action.id,
        detail: { candidate: candidates.map((c) => c.id), riga: t.line },
      });
      for (const c of candidates) linkedExisting.add(c.id);
    }
  }

  // Pending gia' note che cadono nel periodo coperto dal file ma non compaiono piu': da verificare (annullata?
  // contabilizzata con un'altra descrizione?). Nessuna modifica automatica.
  for (const e of unmatched) {
    if (e.status !== "pending" || linkedExisting.has(e.id)) continue;
    if (e.transaction_date >= fileFrom && e.transaction_date <= fileTo) {
      reviews.push({
        kind: "pending_not_seen",
        entityId: e.id,
        detail: { periodo_file: [fileFrom, fileTo] },
      });
    }
  }
  return { actions, reviews, links };
}
