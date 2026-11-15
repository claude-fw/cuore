/**
 * Riconciliazione di un conto tra due saldi osservati di fine giornata (funzione pura).
 *
 *   saldo_apertura (fine giornata `from`) + somma movimenti contabilizzati con data in (from, to] = saldo_chiusura
 *
 * Esiti: `ok` (differenza 0), `difference` (differenza != 0, da verificare), `insufficient_data` (manca un saldo),
 * `conflict` (per la stessa data esistono saldi osservati diversi: nessun calcolo, da verificare).
 */
import { addMinor } from "./money.ts";

export interface ReconcileInput {
  openingObservations: readonly number[];
  closingObservations: readonly number[];
  movements: readonly number[];
}

export interface ReconcileResult {
  status: "ok" | "difference" | "insufficient_data" | "conflict";
  opening: number | null;
  closing: number | null;
  movementsSum: number;
  /** (apertura + movimenti) - chiusura; null se non calcolabile */
  difference: number | null;
}

export function reconcile(input: ReconcileInput): ReconcileResult {
  const movementsSum = input.movements.reduce(addMinor, 0);
  const opening = [...new Set(input.openingObservations)];
  const closing = [...new Set(input.closingObservations)];
  if (opening.length > 1 || closing.length > 1) {
    return { status: "conflict", opening: null, closing: null, movementsSum, difference: null };
  }
  const [o] = opening;
  const [c] = closing;
  if (o === undefined || c === undefined) {
    return {
      status: "insufficient_data",
      opening: o ?? null,
      closing: c ?? null,
      movementsSum,
      difference: null,
    };
  }
  const difference = addMinor(addMinor(o, movementsSum), -c);
  return { status: difference === 0 ? "ok" : "difference", opening: o, closing: c, movementsSum, difference };
}
