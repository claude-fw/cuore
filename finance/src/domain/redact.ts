/**
 * Riduzione dei dati personali nei testi mostrati all'esterno (consultazione, futuri strumenti MCP e dashboard).
 * Funzione PURA, prudente: maschera identificativi bancari che le banche a volte scrivono nelle descrizioni.
 *   - IBAN (2 lettere, 2 cifre, 11-30 caratteri alfanumerici, anche a gruppi separati da spazi) -> "[IBAN]";
 *   - sequenze di 8 o piu' cifre (numeri di carta, conto, riferimenti), anche separate da spazi o trattini ->
 *     "[…1234]" (restano solo le ultime 4 cifre, utili per riconoscere il movimento).
 * Il testo originale resta intatto nel database: si maschera solo l'uscita.
 */
const IBAN = /\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b/gu;
const LONG_NUMBER = /\d(?:[ -]?\d){7,}/gu;

export function redactIdentifiers(text: string): string {
  return text.replace(IBAN, "[IBAN]").replace(LONG_NUMBER, (m) => `[…${m.replace(/\D/gu, "").slice(-4)}]`);
}
