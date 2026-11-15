/**
 * Definizione UNICA dei campi "effettivi" di una transazione, condivisa da riepiloghi (reports.ts) e consultazione
 * (src/query): cosi' elenco dei movimenti e riepilogo non possono divergere (niente doppi conteggi).
 *
 *   - data effettiva: contabilizzate per data di contabilizzazione, pending per data operazione;
 *   - tipo effettivo, nell'ordine: collegato a crediti/debiti (`obligation`), trasferimento confermato,
 *     classificazione corrente SOLO se `auto` o `confirmed`; altrimenti `unknown` (da classificare);
 *   - categoria e gruppo di budget solo da una classificazione decisa;
 *   - `excluded`: doppione confermato (non revocato) o pending sostituita, o manuale annullato (fuori da riepiloghi e
 *     riconciliazioni).
 */
import type { BudgetGroup, SummaryKind } from "../domain/summary.ts";

/**
 * Id delle transazioni escluse da riepiloghi e riconciliazioni: doppioni confermati (un abbinamento manuale ↔ banca
 * confermato per errore e poi revocato in `link_reversals` NON esclude piu' il manuale), pending sostituite, e
 * movimenti manuali annullati (`manual_voids`).
 */
export const EXCLUDED_IDS_SQL = `SELECT to_txn_id AS id FROM transaction_links WHERE type = 'possible_duplicate' AND status = 'confirmed'
       AND transaction_links.id NOT IN (SELECT link_id FROM link_reversals)
     UNION SELECT from_txn_id AS id FROM transaction_links WHERE type = 'pending_booked' AND status = 'confirmed'
     UNION SELECT transaction_id AS id FROM manual_voids`;

/** FROM con alias `t` (transazione), `c` (classificazione corrente), `cat` (categoria). */
// Classificazione corrente = la piu' recente della transazione, presa con l'indice (transaction_id, id): la vista
// `current_classifications` rilegge TUTTO lo storico a ogni consultazione (righe lette = quota del piano Free).
export const EFFECTIVE_FROM = `transactions t
     LEFT JOIN classifications c
       ON c.id = (SELECT MAX(c2.id) FROM classifications c2 WHERE c2.transaction_id = t.id)
     LEFT JOIN categories cat ON cat.id = c.category_id`;

export const EFFECTIVE_DATE_SQL =
  "CASE WHEN t.status = 'booked' THEN t.booking_date ELSE t.transaction_date END";

/** Colonne calcolate (da usare con EFFECTIVE_FROM). Crediti/debiti e trasferimenti con `IN (lista)`, letta una volta
 * sola: l'EXISTS con `from OR to` rileggeva tutti i collegamenti per ogni movimento (14/11: 3,5 milioni di passi
 * contro 90 mila sui dati veri, stesso risultato). */
export const EFFECTIVE_COLUMNS = `${EFFECTIVE_DATE_SQL} AS effective_date,
     CASE
       WHEN t.id IN (SELECT transaction_id FROM obligation_events) THEN 'obligation'
       WHEN t.id IN (SELECT from_txn_id FROM transaction_links WHERE type = 'transfer' AND status = 'confirmed'
                     UNION ALL SELECT to_txn_id FROM transaction_links WHERE type = 'transfer' AND status = 'confirmed')
         THEN 'transfer'
       WHEN c.status IN ('auto', 'confirmed') THEN c.kind
       ELSE 'unknown'
     END AS effective_kind,
     CASE WHEN c.status IN ('auto', 'confirmed') THEN c.category_id END AS effective_category_id,
     CASE WHEN c.status IN ('auto', 'confirmed') THEN cat.budget_group END AS effective_budget_group,
     c.status AS classification_status,
     t.id IN (${EXCLUDED_IDS_SQL}) AS excluded`;

export interface EffectiveColumns {
  effective_date: string;
  effective_kind: SummaryKind;
  effective_category_id: string | null;
  effective_budget_group: BudgetGroup | null;
  classification_status: "auto" | "confirmed" | "needs_review" | null;
  excluded: number;
}
