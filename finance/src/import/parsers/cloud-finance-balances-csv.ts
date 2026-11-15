/**
 * Formato `cloud-finance-balances-csv` v1: saldi di fine giornata osservati (date,balance,currency).
 * Formato neutro di questo progetto, non verificato su esportazioni bancarie reali.
 */
import { MoneyError, parseAmountToMinor } from "../../domain/money.ts";
import { isIsoDate } from "../../domain/text.ts";
import type { ParseContext, ParsedBalance, ParseIssue, ParseResult, StatementParser } from "../types.ts";
import { utf8TextParser } from "../types.ts";
import { col, issue, readHeaderedCsv } from "./common.ts";

export const BALANCE_COLUMNS = ["date", "balance", "currency"] as const;

export const cloudFinanceBalancesCsvV1: StatementParser = {
  id: "cloud-finance-balances-csv",
  version: 1,
  description:
    "CSV neutro di Cloud Finance (saldi di fine giornata). Formato di esempio documentato, non bancario.",
  verifiedAgainstRealExports: false,

  parseFile: utf8TextParser((text: string, ctx: ParseContext): ParseResult => {
    const { rows, issues } = readHeaderedCsv(text, BALANCE_COLUMNS);
    const out: ParsedBalance[] = [];
    const dates = new Map<string, number>();
    for (const { line, values } of rows) {
      const rowIssues: ParseIssue[] = [];
      const date = col(values, "date");
      if (!isIsoDate(date)) rowIssues.push(issue(line, "date", "invalid_date", `data non valida: "${date}"`));
      const firstLine = dates.get(date);
      if (firstLine !== undefined) {
        rowIssues.push(issue(line, "date", "duplicate_date", `data ripetuta (gia' alla riga ${firstLine})`));
      } else dates.set(date, line);
      const currency = col(values, "currency");
      if (currency !== ctx.accountCurrency) {
        rowIssues.push(
          issue(
            line,
            "currency",
            "currency_mismatch",
            `valuta ${currency || "(vuota)"} diversa da ${ctx.accountCurrency}`,
          ),
        );
      }
      let balanceMinor: number | null = null;
      try {
        balanceMinor = parseAmountToMinor(col(values, "balance"), ctx.accountCurrency);
      } catch (err) {
        rowIssues.push(
          issue(
            line,
            "balance",
            err instanceof MoneyError ? err.code : "invalid_amount",
            err instanceof Error ? err.message : String(err),
          ),
        );
      }
      if (rowIssues.length > 0 || balanceMinor === null) {
        issues.push(...rowIssues);
        continue;
      }
      out.push({ line, raw: values, asOfDate: date, balanceMinor, currency });
    }
    return { transactions: [], balances: out, issues };
  }),
};
