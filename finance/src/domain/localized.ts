/**
 * Conversioni RIGOROSE da formati "umani" (esportazioni bancarie, fogli di calcolo) ai formati interni.
 * Ogni formato e' dichiarato dalla specifica: nessuna autodeduzione (es. 01/02/2026 non si indovina).
 */
import { currencyExponent, MoneyError, parseAmountToMinor } from "./money.ts";
import { isIsoDate } from "./text.ts";

export interface NumberFormat {
  /** separatore decimale */
  readonly decimal: "." | ",";
  /** separatore delle migliaia ammesso (null = nessuno); i gruppi devono essere esattamente di 3 cifre */
  readonly thousands: "." | "," | " " | "'" | null;
}

/**
 * "1.234,56" (decimal ",", thousands ".") -> 123456 per EUR. Ammessi solo: segno "-" iniziale facoltativo, cifre,
 * separatore delle migliaia dichiarato (gruppi di 3), separatore decimale dichiarato. Qualsiasi altro carattere
 * (simboli di valuta, "+", spazi interni non dichiarati, parentesi) e' un errore.
 */
export function parseLocalizedAmount(text: string, currency: string, fmt: NumberFormat): number {
  if (fmt.thousands !== null && fmt.thousands === fmt.decimal) {
    throw new MoneyError("invalid_number_format", "separatore decimale e delle migliaia coincidono");
  }
  const t = text.trim();
  const sign = t.startsWith("-") ? "-" : "";
  const body = sign ? t.slice(1) : t;
  const decIdx = body.lastIndexOf(fmt.decimal);
  const intPart = decIdx === -1 ? body : body.slice(0, decIdx);
  const fracPart = decIdx === -1 ? null : body.slice(decIdx + 1);
  if (fracPart !== null && !/^\d+$/.test(fracPart)) {
    throw new MoneyError("invalid_amount", `importo non valido: "${text}"`);
  }
  let digits: string;
  if (/^\d+$/.test(intPart)) digits = intPart;
  else if (fmt.thousands !== null) {
    const groups = intPart.split(fmt.thousands);
    const [head, ...rest] = groups;
    if (
      head === undefined ||
      !/^\d{1,3}$/.test(head) ||
      rest.length === 0 ||
      !rest.every((g) => /^\d{3}$/.test(g))
    ) {
      throw new MoneyError("invalid_amount", `importo non valido (migliaia): "${text}"`);
    }
    digits = groups.join("");
  } else {
    throw new MoneyError("invalid_amount", `importo non valido: "${text}"`);
  }
  return parseAmountToMinor(`${sign}${digits}${fracPart === null ? "" : `.${fracPart}`}`, currency);
}

/**
 * Numero memorizzato come numero binario in un foglio XLSX (es. "-45.200000000000003"): si arrotonda alle unita'
 * minime SOLO se la differenza e' rumore della virgola mobile (< 1e-6 dell'unita' minima); altrimenti errore.
 */
export function parseSpreadsheetNumber(text: string, currency: string): number {
  if (!/^-?\d+(\.\d+)?(E[+-]?\d+)?$/i.test(text.trim())) {
    throw new MoneyError("invalid_amount", `numero non valido nel foglio: "${text}"`);
  }
  const exp = currencyExponent(currency);
  const scaled = Number(text) * 10 ** exp;
  const rounded = Math.round(scaled);
  if (!Number.isSafeInteger(rounded))
    throw new MoneyError("amount_out_of_range", `importo fuori intervallo: "${text}"`);
  if (Math.abs(scaled - rounded) > 1e-6) {
    throw new MoneyError("too_many_decimals", `troppi decimali per ${currency}: "${text}"`);
  }
  return rounded === 0 ? 0 : rounded;
}

export type DateFormat = "YYYY-MM-DD" | "DD/MM/YYYY" | "DD-MM-YYYY" | "DD.MM.YYYY";
export type DateTimeFormat =
  | "YYYY-MM-DD HH:mm:ss"
  | "YYYY-MM-DDTHH:mm:ss"
  | "DD/MM/YYYY HH:mm:ss"
  | "DD/MM/YYYY HH:mm";

const DATE_RX: Record<DateFormat, RegExp> = {
  "YYYY-MM-DD": /^(\d{4})-(\d{2})-(\d{2})$/,
  "DD/MM/YYYY": /^(\d{2})\/(\d{2})\/(\d{4})$/,
  "DD-MM-YYYY": /^(\d{2})-(\d{2})-(\d{4})$/,
  "DD.MM.YYYY": /^(\d{2})\.(\d{2})\.(\d{4})$/,
};

/** Data nel formato dichiarato -> YYYY-MM-DD, oppure null se non valida (anche 31/02). */
export function parseDate(text: string, format: DateFormat): string | null {
  const m = DATE_RX[format].exec(text.trim());
  if (m === null) return null;
  const [, a = "", b = "", c = ""] = m;
  const iso = format === "YYYY-MM-DD" ? `${a}-${b}-${c}` : `${c}-${b}-${a}`;
  return isIsoDate(iso) ? iso : null;
}

const DATETIME_RX: Record<DateTimeFormat, RegExp> = {
  "YYYY-MM-DD HH:mm:ss": /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/,
  "YYYY-MM-DDTHH:mm:ss": /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})$/,
  "DD/MM/YYYY HH:mm:ss": /^(\d{2})\/(\d{2})\/(\d{4}) (\d{2}):(\d{2}):(\d{2})$/,
  "DD/MM/YYYY HH:mm": /^(\d{2})\/(\d{2})\/(\d{4}) (\d{2}):(\d{2})()$/,
};

export interface LocalDateTime {
  date: string;
  hour: number;
  minute: number;
  second: number;
}

export function parseDateTime(text: string, format: DateTimeFormat): LocalDateTime | null {
  const m = DATETIME_RX[format].exec(text.trim());
  if (m === null) return null;
  const [, a = "", b = "", c = "", hh = "", mm = "", ss = ""] = m;
  const date = format.startsWith("YYYY") ? `${a}-${b}-${c}` : `${c}-${b}-${a}`;
  const hour = Number(hh);
  const minute = Number(mm);
  const second = ss === "" ? 0 : Number(ss);
  if (!isIsoDate(date) || hour > 23 || minute > 59 || second > 59) return null;
  return { date, hour, minute, second };
}

/**
 * Data di calendario nel fuso `reportingTimeZone` di un istante espresso nel fuso `sourceTimeZone`.
 * - sourceTimeZone === reportingTimeZone: la data e' quella scritta (nessuna conversione);
 * - sourceTimeZone "UTC": conversione con il database dei fusi del runtime (Intl, disponibile in Node e Workers).
 * Altri fusi di origine non sono ammessi (servirebbe risolvere l'ambiguita' dei cambi d'ora).
 */
export function calendarDateIn(dt: LocalDateTime, sourceTimeZone: string, reportingTimeZone: string): string {
  if (sourceTimeZone === reportingTimeZone) return dt.date;
  if (sourceTimeZone !== "UTC") {
    throw new Error(
      `fuso di origine non supportato: ${sourceTimeZone} (ammessi: UTC o ${reportingTimeZone})`,
    );
  }
  const [y, mo, d] = dt.date.split("-").map(Number) as [number, number, number];
  const instant = new Date(Date.UTC(y, mo - 1, d, dt.hour, dt.minute, dt.second));
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: reportingTimeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(instant);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")}`;
}

/**
 * Numero di serie di data di un foglio di calcolo -> YYYY-MM-DD. Accetta solo numeri INTERI (una parte oraria
 * renderebbe la data dipendente da un fuso non dichiarato). `date1904` dal workbook.
 */
export function spreadsheetSerialToDate(text: string, date1904: boolean): string | null {
  if (!/^\d+$/.test(text.trim())) return null;
  const serial = Number(text);
  // sistema 1900: il giorno 60 e' il 29/02/1900 inesistente (errore storico di Lotus/Excel); dal 61 in poi si
  // scala di un giorno. I seriali < 61 non sono plausibili per movimenti bancari: rifiutati.
  if (!date1904 && serial < 61) return null;
  const epoch = date1904 ? Date.UTC(1904, 0, 1) : Date.UTC(1899, 11, 30);
  const iso = new Date(epoch + serial * 86_400_000).toISOString().slice(0, 10);
  return isIsoDate(iso) ? iso : null;
}
