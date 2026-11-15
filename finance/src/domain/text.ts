/** Normalizzazione testuale per confronti (mai per la visualizzazione: il testo originale resta intatto). */
export function normalizeText(value: string): string {
  return value.normalize("NFKC").toLocaleLowerCase("it-IT").replace(/\s+/gu, " ").trim();
}

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

/** Data di calendario valida in formato YYYY-MM-DD (rifiuta 2026-02-30). */
export function isIsoDate(value: string): boolean {
  const m = ISO_DATE.exec(value);
  if (m === null) return false;
  const [, y, mo, d] = m;
  const date = new Date(Date.UTC(Number(y), Number(mo) - 1, Number(d)));
  return date.toISOString().slice(0, 10) === value;
}

/** Differenza in giorni tra due date ISO (b - a). */
export function daysBetween(a: string, b: string): number {
  return Math.round((Date.parse(`${b}T00:00:00Z`) - Date.parse(`${a}T00:00:00Z`)) / 86_400_000);
}

export function addDays(date: string, days: number): string {
  return new Date(Date.parse(`${date}T00:00:00Z`) + days * 86_400_000).toISOString().slice(0, 10);
}
