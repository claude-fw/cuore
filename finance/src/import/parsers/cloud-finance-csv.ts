/**
 * Formato `cloud-finance-csv` v1: formato NEUTRO definito da questo progetto (docs/formati-import.md).
 * NON e' il formato di Revolut ne' di MPS: i loro formati reali non sono ancora stati verificati.
 * Serve per inserire dati preparati a mano o convertiti, e come riferimento per i futuri parser per banca.
 */
import { MoneyError, parseAmountToMinor } from "../../domain/money.ts";
import { isIsoDate } from "../../domain/text.ts";
import type {
  ParseContext,
  ParsedTransaction,
  ParseIssue,
  ParseResult,
  StatementParser,
  TxnStatus,
} from "../types.ts";
import { utf8TextParser } from "../types.ts";
import { col, issue, readHeaderedCsv } from "./common.ts";

export const TRANSACTION_COLUMNS = [
  "transaction_id",
  "transaction_date",
  "booking_date",
  "status",
  "amount",
  "currency",
  "description",
  "counterparty",
  "original_amount",
  "original_currency",
] as const;

const MAX_TEXT = 500;

export const cloudFinanceCsvV1: StatementParser = {
  id: "cloud-finance-csv",
  version: 1,
  description: "CSV neutro di Cloud Finance (transazioni). Formato di esempio documentato, non bancario.",
  verifiedAgainstRealExports: false,

  parseFile: utf8TextParser((text: string, ctx: ParseContext): ParseResult => {
    const { rows, issues } = readHeaderedCsv(text, TRANSACTION_COLUMNS);
    const out: ParsedTransaction[] = [];
    const ids = new Map<string, number>();

    for (const { line, values } of rows) {
      const rowIssues: ParseIssue[] = [];
      const bad = (field: string, code: string, message: string) =>
        rowIssues.push(issue(line, field, code, message));

      const sourceTxnId = col(values, "transaction_id") || null;
      if (sourceTxnId !== null) {
        const firstLine = ids.get(sourceTxnId);
        if (firstLine !== undefined) {
          bad("transaction_id", "duplicate_id", `transaction_id ripetuto (gia' alla riga ${firstLine})`);
        } else ids.set(sourceTxnId, line);
        if (sourceTxnId.length > 200) bad("transaction_id", "too_long", "transaction_id troppo lungo");
      }

      const transactionDate = col(values, "transaction_date");
      if (!isIsoDate(transactionDate))
        bad("transaction_date", "invalid_date", `data non valida: "${transactionDate}"`);

      const statusText = col(values, "status");
      const status: TxnStatus | null =
        statusText === "pending" || statusText === "booked" ? statusText : null;
      if (status === null)
        bad("status", "invalid_status", `stato non valido: "${statusText}" (pending|booked)`);

      const bookingText = col(values, "booking_date");
      const bookingDate = bookingText === "" ? null : bookingText;
      if (bookingDate !== null && !isIsoDate(bookingDate)) {
        bad("booking_date", "invalid_date", `data non valida: "${bookingDate}"`);
      }
      if (status === "booked" && bookingDate === null) {
        bad("booking_date", "missing_booking_date", "booking_date obbligatoria per le righe booked");
      }
      if (status === "pending" && bookingDate !== null) {
        bad("booking_date", "unexpected_booking_date", "booking_date deve essere vuota per le righe pending");
      }

      const currency = col(values, "currency");
      if (currency !== ctx.accountCurrency) {
        bad(
          "currency",
          "currency_mismatch",
          `valuta ${currency || "(vuota)"} diversa da quella del conto (${ctx.accountCurrency})`,
        );
      }

      let amountMinor: number | null = null;
      try {
        amountMinor = parseAmountToMinor(col(values, "amount"), ctx.accountCurrency);
      } catch (err) {
        bad("amount", err instanceof MoneyError ? err.code : "invalid_amount", errorText(err));
      }

      const originalAmountText = col(values, "original_amount");
      const originalCurrencyText = col(values, "original_currency");
      let originalAmountMinor: number | null = null;
      let originalCurrency: string | null = null;
      if ((originalAmountText === "") !== (originalCurrencyText === "")) {
        bad(
          "original_amount",
          "original_incomplete",
          "original_amount e original_currency vanno indicati insieme",
        );
      } else if (originalAmountText !== "") {
        try {
          originalAmountMinor = parseAmountToMinor(originalAmountText, originalCurrencyText);
          originalCurrency = originalCurrencyText;
        } catch (err) {
          bad("original_amount", err instanceof MoneyError ? err.code : "invalid_amount", errorText(err));
        }
        if (originalCurrencyText === currency) {
          bad(
            "original_currency",
            "original_same_currency",
            "original_currency uguale alla valuta del conto",
          );
        }
      }
      if (
        originalAmountMinor !== null &&
        amountMinor !== null &&
        Math.sign(originalAmountMinor) !== Math.sign(amountMinor)
      ) {
        bad("original_amount", "original_sign_mismatch", "original_amount con segno diverso da amount");
      }

      const description = col(values, "description");
      if (description === "") bad("description", "missing_description", "descrizione obbligatoria");
      if (description.length > MAX_TEXT)
        bad("description", "too_long", `descrizione oltre ${MAX_TEXT} caratteri`);
      const counterpartyText = col(values, "counterparty");
      if (counterpartyText.length > MAX_TEXT)
        bad("counterparty", "too_long", `controparte oltre ${MAX_TEXT} caratteri`);

      if (rowIssues.length > 0 || status === null || amountMinor === null) {
        issues.push(...rowIssues);
        continue;
      }
      out.push({
        line,
        raw: values,
        sourceTxnId,
        status,
        transactionDate,
        bookingDate,
        amountMinor,
        currency,
        originalAmountMinor,
        originalCurrency,
        description,
        counterparty: counterpartyText === "" ? null : counterpartyText,
      });
    }
    return { transactions: out, balances: [], issues };
  }),
};

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
