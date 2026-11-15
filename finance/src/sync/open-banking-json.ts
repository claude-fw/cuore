/**
 * Formato `open-banking-json` v1: il pacchetto che la sincronizzazione (sync/runner.ts) costruisce con le risposte di un
 * fornitore di accesso ai conti (AISP, PSD2), oggi nella forma documentata da Enable Banking
 * (GET /accounts/{id}/transactions e /balances). NON verificato su risposte reali: `verifiedAgainstRealExports` e' false
 * e il parser NON e' nel registro dei formati (registry.ts): lo usa solo la sincronizzazione, passandolo esplicitamente.
 *
 * Pacchetto (UTF-8, JSON canonico):
 *   { schema: "cuore.sync.payload/1", provider, window: { from, to }, transactions: [Transaction], balances: [Balance] }
 * Transaction (campi usati): transaction_id | entry_reference, transaction_amount { amount, currency },
 *   credit_debit_indicator (CRDT | DBIT), status (BOOK | PDNG | altri), booking_date, transaction_date, value_date,
 *   creditor { name }, debtor { name }, remittance_information [string], bank_transaction_code { code, sub_code },
 *   merchant_category_code.
 * Regole:
 *   - BOOK = contabilizzata, PDNG = in attesa; ogni altro stato (CNCL, RJCT, SCHD, HOLD, OTHR, ...) e' ESCLUSO con il motivo;
 *   - importo sempre positivo nella fonte, segno da credit_debit_indicator; un importo gia' negativo che contraddice
 *     l'indicatore e' un problema (file rifiutato), mai un segno indovinato;
 *   - valuta diversa da quella del conto = problema (un conto per valuta);
 *   - id della fonte = transaction_id, altrimenti entry_reference, altrimenti nessuno (deduplica per impronta);
 *   - saldi: CLBD (chiusura contabile) con reference_date; gli altri tipi sono contati in un avviso. Se la banca NON
 *     manda nessun CLBD (es. Revolut solo ITAV, MPS solo XPCD/OTHR) si usa UN saldo di ripiego, nell'ordine ITBD,
 *     CLAV, ITAV, XPCD, OTHR (cioe' il saldo che il titolare vede nell'app della banca), con la sua data o, se manca,
 *     l'ultimo giorno della finestra letta; un avviso `balance_type_fallback` dice quale tipo e' stato usato.
 * Il dato grezzo di ogni riga e' l'oggetto completo come ricevuto (`json`), conservato nello storico immutabile.
 */
import { canonicalJson, type Json } from "../domain/hash.ts";
import { MoneyError, parseAmountToMinor } from "../domain/money.ts";
import { isIsoDate } from "../domain/text.ts";
import { issue } from "../import/parsers/common.ts";
import type {
  ExcludedRow,
  ParseContext,
  ParsedBalance,
  ParsedTransaction,
  ParseIssue,
  ParseNotice,
  ParseResult,
  StatementParser,
} from "../import/types.ts";
import { utf8TextParser } from "../import/types.ts";

export const PAYLOAD_SCHEMA = "cuore.sync.payload/1";
export const OPEN_BANKING_PARSER_ID = "open-banking-json";
/** righe dei saldi numerate dopo questa soglia: una riga = un oggetto del pacchetto, mai due con lo stesso numero */
export const BALANCE_LINE_BASE = 1_000_000;

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);
const str = (v: unknown): string | null => (typeof v === "string" && v.trim() !== "" ? v.trim() : null);

const STATUS: Readonly<Record<string, "booked" | "pending">> = { BOOK: "booked", PDNG: "pending" };

export interface SyncPayload {
  schema: typeof PAYLOAD_SCHEMA;
  provider: string;
  window: { from: string; to: string };
  transactions: unknown[];
  balances: unknown[];
}

/** Pacchetto canonico: stesso contenuto = stessi byte = stessa impronta (il motore lo riconosce come gia' importato). */
export function encodePayload(p: SyncPayload): Uint8Array {
  return new TextEncoder().encode(canonicalJson(p as unknown as Json));
}

function signedAmount(t: Obj, line: number, currency: string, issues: ParseIssue[]): number | null {
  const amt = isObj(t.transaction_amount) ? t.transaction_amount : null;
  const text = str(amt?.amount);
  if (text === null) {
    issues.push(issue(line, "transaction_amount.amount", "missing_amount", "importo assente"));
    return null;
  }
  let minor: number;
  try {
    minor = parseAmountToMinor(text, currency);
  } catch (e) {
    issues.push(
      issue(
        line,
        "transaction_amount.amount",
        e instanceof MoneyError ? e.code : "invalid_amount",
        String(e),
      ),
    );
    return null;
  }
  const ind = t.credit_debit_indicator;
  if (ind !== "CRDT" && ind !== "DBIT") {
    issues.push(
      issue(
        line,
        "credit_debit_indicator",
        "missing_direction",
        "indicatore entrata/uscita assente o non ammesso",
      ),
    );
    return null;
  }
  if (minor < 0 && ind === "CRDT") {
    issues.push(
      issue(line, "transaction_amount.amount", "sign_conflict", "importo negativo con indicatore di entrata"),
    );
    return null;
  }
  const abs = Math.abs(minor);
  return ind === "DBIT" ? -abs : abs;
}

function parsePayload(text: string, ctx: ParseContext): ParseResult {
  const issues: ParseIssue[] = [];
  const empty = (code: string, message: string): ParseResult => ({
    transactions: [],
    balances: [],
    issues: [issue(null, null, code, message)],
  });
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch {
    return empty("invalid_json", "pacchetto non e' JSON valido");
  }
  if (
    !isObj(doc) ||
    doc.schema !== PAYLOAD_SCHEMA ||
    !Array.isArray(doc.transactions) ||
    !Array.isArray(doc.balances)
  )
    return empty("payload_schema", `pacchetto non conforme a ${PAYLOAD_SCHEMA}`);

  const transactions: ParsedTransaction[] = [];
  const excluded: ExcludedRow[] = [];
  doc.transactions.forEach((t, i) => {
    const line = i + 1;
    if (!isObj(t)) {
      issues.push(issue(line, null, "invalid_record", "transazione: oggetto atteso"));
      return;
    }
    const raw = { json: canonicalJson(t as Json) };
    const sourceTxnId = str(t.transaction_id) ?? str(t.entry_reference);
    const status = typeof t.status === "string" ? STATUS[t.status] : undefined;
    if (status === undefined) {
      excluded.push({
        line,
        raw,
        sourceTxnId,
        reason: `stato ${String(t.status)} non contabile (annullata, rifiutata o futura)`,
      });
      return;
    }
    const rowIssues: ParseIssue[] = [];
    const currency = str(isObj(t.transaction_amount) ? t.transaction_amount.currency : null);
    if (currency !== ctx.accountCurrency)
      rowIssues.push(
        issue(
          line,
          "transaction_amount.currency",
          "currency_mismatch",
          `valuta ${currency ?? "(assente)"} diversa da ${ctx.accountCurrency}`,
        ),
      );
    const amountMinor = signedAmount(t, line, ctx.accountCurrency, rowIssues);
    const booking = str(t.booking_date);
    const txDate = str(t.transaction_date) ?? booking ?? str(t.value_date);
    for (const [field, v] of [
      ["booking_date", booking],
      ["transaction_date", txDate],
    ] as const)
      if (v !== null && !isIsoDate(v))
        rowIssues.push(issue(line, field, "invalid_date", `data non valida: "${v}"`));
    if (txDate === null)
      rowIssues.push(issue(line, "transaction_date", "missing_date", "nessuna data nella transazione"));
    if (status === "booked" && booking === null)
      rowIssues.push(
        issue(line, "booking_date", "missing_booking_date", "contabilizzata senza data contabile"),
      );
    if (rowIssues.length > 0 || amountMinor === null || txDate === null) {
      issues.push(...rowIssues);
      return;
    }
    const remittance = Array.isArray(t.remittance_information)
      ? t.remittance_information
          .filter((x): x is string => typeof x === "string" && x.trim() !== "")
          .map((x) => x.trim())
      : [];
    const party = isObj(amountMinor < 0 ? t.creditor : t.debtor)
      ? ((amountMinor < 0 ? t.creditor : t.debtor) as Obj)
      : null;
    const code = isObj(t.bank_transaction_code) ? t.bank_transaction_code : null;
    const description =
      remittance.join(" ") || str(code?.description) || str(party?.name) || "(senza descrizione)";
    const sourceType =
      [str(code?.code), str(code?.sub_code)].filter(Boolean).join("/") ||
      (str(t.merchant_category_code) ? `MCC ${str(t.merchant_category_code)}` : null);
    transactions.push({
      line,
      raw,
      sourceTxnId,
      status,
      transactionDate: txDate,
      bookingDate: status === "booked" ? booking : null,
      amountMinor,
      currency: ctx.accountCurrency,
      originalAmountMinor: null,
      originalCurrency: null,
      description,
      counterparty: str(party?.name),
      sourceType,
    });
  });

  const balances: ParsedBalance[] = [];
  const ignored = new Map<string, number>();
  const dates = new Set<string>();
  const FALLBACK = ["ITBD", "CLAV", "ITAV", "XPCD", "OTHR"];
  let fallback: { b: Obj; type: string; line: number } | null = null;
  doc.balances.forEach((b, i) => {
    const line = BALANCE_LINE_BASE + i + 1;
    if (!isObj(b)) {
      issues.push(issue(line, null, "invalid_record", "saldo: oggetto atteso"));
      return;
    }
    const type = str(b.balance_type) ?? "(assente)";
    if (type !== "CLBD") {
      ignored.set(type, (ignored.get(type) ?? 0) + 1);
      const rank = FALLBACK.indexOf(type);
      if (rank >= 0 && (fallback === null || rank < FALLBACK.indexOf(fallback.type)))
        fallback = { b, type, line };
      return;
    }
    addBalance(b, line, str(b.reference_date));
  });
  const notices: ParseNotice[] = [];
  if (balances.length === 0 && fallback !== null) {
    const f = fallback as { b: Obj; type: string; line: number };
    ignored.delete(f.type);
    if (addBalance(f.b, f.line, str(f.b.reference_date) ?? str(isObj(doc.window) ? doc.window.to : null)))
      notices.push({
        code: "balance_type_fallback",
        message: `la banca non manda il saldo di chiusura: usato il saldo di tipo ${f.type}`,
        count: 1,
      });
  }
  function addBalance(b: Obj, line: number, day: string | null): boolean {
    const amt = isObj(b.balance_amount) ? b.balance_amount : null;
    if (day === null || !isIsoDate(day)) {
      issues.push(issue(line, "reference_date", "invalid_date", "saldo di chiusura senza data valida"));
      return false;
    }
    if (str(amt?.currency) !== ctx.accountCurrency) {
      issues.push(
        issue(
          line,
          "balance_amount.currency",
          "currency_mismatch",
          "valuta del saldo diversa da quella del conto",
        ),
      );
      return false;
    }
    if (dates.has(day)) {
      issues.push(issue(line, "reference_date", "duplicate_date", `saldo di chiusura ripetuto per ${day}`));
      return false;
    }
    dates.add(day);
    try {
      balances.push({
        line,
        raw: { json: canonicalJson(b as Json) },
        asOfDate: day,
        balanceMinor: parseAmountToMinor(str(amt?.amount) ?? "", ctx.accountCurrency),
        currency: ctx.accountCurrency,
      });
      return true;
    } catch (e) {
      issues.push(
        issue(line, "balance_amount.amount", e instanceof MoneyError ? e.code : "invalid_amount", String(e)),
      );
      return false;
    }
  }
  notices.push(
    ...[...ignored].map(([type, n]) => ({
      code: "balance_type_ignored",
      message: `saldi di tipo ${type} non importati (solo CLBD = chiusura contabile di fine giornata)`,
      count: n,
    })),
  );
  return { transactions, balances, issues, excluded, notices };
}

export const openBankingJsonV1: StatementParser = {
  id: OPEN_BANKING_PARSER_ID,
  version: 1,
  description:
    "Pacchetto della sincronizzazione Open Banking (forma Enable Banking AIS). Non verificato su risposte reali.",
  verifiedAgainstRealExports: false,
  parseFile: utf8TextParser(parsePayload),
};
