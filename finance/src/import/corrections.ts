/**
 * CORREZIONI DICHIARATE della data contabile, applicate in importazione (funzione PURA).
 *
 * Servono quando una fonte derivata (es. staging da PDF) riporta una data contabile sbagliata e la data giusta e'
 * verificabile su un documento originale. Il file sorgente NON viene modificato ne' copiato: i dati grezzi restano
 * quelli del file, la correzione (con motivo e prova) finisce nell'audit dell'importazione.
 *
 * Regole: ogni correzione individua UNA sola riga (valori grezzi esatti), contabilizzata; due correzioni sulla stessa
 * riga, una correzione che non cambia nulla (correzione superata) o senza motivo/prova -> file rifiutato.
 * Si corregge solo la data contabile: identita' (impronta sulla data operazione) e importi restano invariati.
 */
import { isIsoDate } from "../domain/text.ts";
import type { ParsedTransaction, ParseIssue } from "./types.ts";

export interface BookingDateCorrection {
  /** colonne grezze -> valore esatto, che insieme individuano una sola riga del file */
  readonly match: Readonly<Record<string, string>>;
  /** data contabile corretta (YYYY-MM-DD) */
  readonly bookingDate: string;
  readonly reason: string;
  /** dove si verifica la data corretta (documento originale, pagina, colonna) */
  readonly evidence: string;
}

export interface AppliedCorrection {
  line: number;
  from: string | null;
  to: string;
  reason: string;
  evidence: string;
}

export function applyBookingDateCorrections(
  transactions: readonly ParsedTransaction[],
  corrections: readonly BookingDateCorrection[],
): { transactions: ParsedTransaction[]; issues: ParseIssue[]; applied: AppliedCorrection[] } {
  const issues: ParseIssue[] = [];
  const bad = (code: string, message: string, line: number | null = null) =>
    issues.push({ line, field: "correction", code, message });
  const out = [...transactions];
  const applied: AppliedCorrection[] = [];
  const touched = new Set<number>();
  for (const [n, c] of corrections.entries()) {
    const label = `correzione ${n + 1}`;
    const keys = Object.keys(c.match);
    if (keys.length === 0) {
      bad("correction_invalid", `${label}: nessun campo per individuare la riga`);
      continue;
    }
    if (!isIsoDate(c.bookingDate)) {
      bad("correction_invalid", `${label}: data non valida (${c.bookingDate})`);
      continue;
    }
    if (c.reason.trim() === "" || c.evidence.trim() === "") {
      bad("correction_invalid", `${label}: motivo e prova obbligatori`);
      continue;
    }
    const hits = out
      .map((t, i) => ({ t, i }))
      .filter(({ t }) => keys.every((k) => Object.hasOwn(t.raw, k) && t.raw[k] === c.match[k]));
    if (hits.length === 0) {
      bad("correction_not_matched", `${label}: nessuna riga corrisponde`);
      continue;
    }
    if (hits.length > 1) {
      bad("correction_ambiguous", `${label}: ${hits.length} righe corrispondono (campi insufficienti)`);
      continue;
    }
    const hit = hits[0];
    if (hit === undefined) continue;
    const { t, i } = hit;
    if (t.status !== "booked") {
      bad("correction_invalid", `${label}: riga non contabilizzata`, t.line);
      continue;
    }
    if (touched.has(i)) {
      bad("correction_conflict", `${label}: riga gia' corretta da un'altra correzione`, t.line);
      continue;
    }
    if (t.bookingDate === c.bookingDate) {
      bad("correction_noop", `${label}: la data e' gia' ${c.bookingDate} (correzione superata?)`, t.line);
      continue;
    }
    touched.add(i);
    out[i] = { ...t, bookingDate: c.bookingDate };
    applied.push({
      line: t.line,
      from: t.bookingDate,
      to: c.bookingDate,
      reason: c.reason,
      evidence: c.evidence,
    });
  }
  return { transactions: out, issues, applied };
}
