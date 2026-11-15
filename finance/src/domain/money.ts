/**
 * Importi in UNITA' MINIME intere (es. centesimi): mai numeri in virgola mobile per il denaro.
 * Valute ammesse: elenco esplicito con l'esponente ISO 4217. Una valuta sconosciuta e' un errore, non un default.
 */

export const CURRENCY_EXPONENTS: Readonly<Record<string, number>> = {
  EUR: 2,
  USD: 2,
  GBP: 2,
  CHF: 2,
  PLN: 2,
  CZK: 2,
  HUF: 2,
  RON: 2,
  SEK: 2,
  NOK: 2,
  DKK: 2,
  CAD: 2,
  AUD: 2,
  JPY: 0,
};

export class MoneyError extends Error {
  readonly code: string;
  constructor(code: string, message: string) {
    super(message);
    this.name = "MoneyError";
    this.code = code;
  }
}

export function currencyExponent(currency: string): number {
  const exp = CURRENCY_EXPONENTS[currency];
  if (exp === undefined) throw new MoneyError("unsupported_currency", `valuta non supportata: ${currency}`);
  return exp;
}

export function isSupportedCurrency(currency: string): boolean {
  return Object.hasOwn(CURRENCY_EXPONENTS, currency);
}

const DECIMAL = /^(-)?(\d+)(?:\.(\d+))?$/;

/**
 * "−12.30" -> -1230 (EUR). Formato rigoroso: punto decimale, nessun separatore delle migliaia, nessun "+",
 * al massimo tanti decimali quanti ne prevede la valuta (niente arrotondamenti silenziosi).
 */
export function parseAmountToMinor(text: string, currency: string): number {
  const exp = currencyExponent(currency);
  const m = DECIMAL.exec(text);
  if (m === null) throw new MoneyError("invalid_amount", `importo non valido: "${text}"`);
  const [, sign, intPart = "", frac = ""] = m;
  if (frac.length > exp) {
    throw new MoneyError("too_many_decimals", `troppi decimali per ${currency} (max ${exp}): "${text}"`);
  }
  const digits = (intPart + frac.padEnd(exp, "0")).replace(/^0+(?=\d)/, "");
  const value = Number(digits);
  if (!Number.isSafeInteger(value))
    throw new MoneyError("amount_out_of_range", `importo fuori intervallo: "${text}"`);
  return sign === "-" && value !== 0 ? -value : value;
}

/** -1230 (EUR) -> "-12.30". */
export function formatMinor(minor: number, currency: string): string {
  assertMinor(minor);
  const exp = currencyExponent(currency);
  const abs = Math.abs(minor)
    .toString()
    .padStart(exp + 1, "0");
  const intPart = exp === 0 ? abs : abs.slice(0, -exp);
  const frac = exp === 0 ? "" : `.${abs.slice(-exp)}`;
  return `${minor < 0 ? "-" : ""}${intPart}${frac}`;
}

export function assertMinor(value: number): void {
  if (!Number.isSafeInteger(value))
    throw new MoneyError("amount_out_of_range", `importo non intero o fuori intervallo`);
}

/** Somma con controllo di overflow (Number.MAX_SAFE_INTEGER). */
export function addMinor(a: number, b: number): number {
  const s = a + b;
  assertMinor(s);
  return s;
}

const RATE = /^(\d+)(?:\.(\d+))?$/;

/**
 * Conversione con un tasso decimale testuale ("1.0845" = 1 unita' di `from` vale 1.0845 `to`).
 * Aritmetica intera (BigInt); arrotondamento al centesimo "metà lontano da zero".
 * Il risultato e' sempre una STIMA: chi lo usa deve marcarlo come tale.
 */
export function convertMinor(amountMinor: number, from: string, to: string, rate: string): number {
  assertMinor(amountMinor);
  const m = RATE.exec(rate);
  if (m === null) throw new MoneyError("invalid_rate", `tasso non valido: "${rate}"`);
  const [, intPart = "", frac = ""] = m;
  const rateNum = BigInt(intPart + frac);
  if (rateNum === 0n) throw new MoneyError("invalid_rate", "tasso nullo");
  const rateScale = 10n ** BigInt(frac.length);
  const expFrom = BigInt(currencyExponent(from));
  const expTo = BigInt(currencyExponent(to));
  const numerator = BigInt(amountMinor) * rateNum * 10n ** expTo;
  const denominator = rateScale * 10n ** expFrom;
  const result = divRoundHalfAwayFromZero(numerator, denominator);
  const n = Number(result);
  assertMinor(n);
  return n;
}

function divRoundHalfAwayFromZero(n: bigint, d: bigint): bigint {
  const negative = n < 0n !== d < 0n;
  const an = n < 0n ? -n : n;
  const ad = d < 0n ? -d : d;
  const q = (an * 2n + ad) / (ad * 2n);
  return negative ? -q : q;
}
