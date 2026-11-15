/**
 * MOTORE generico per formati tabellari dichiarati con una TabularSpec (CSV o XLSX).
 *
 * Garanzie:
 *   - intestazione cercata solo nelle prime `maxRowsBefore` righe; le righe precedenti (dati del documento, spesso
 *     personali) NON vengono salvate, solo contate;
 *   - ogni riga di dati produce: una transazione (piu' l'eventuale commissione separata), una riga ESCLUSA con il
 *     motivo dichiarato nella specifica, oppure un PROBLEMA. Nessuna riga sparisce in silenzio;
 *   - righe completamente vuote e righe dopo un marcatore di chiusura sono solo contate (non contengono dati);
 *   - nessuna classificazione: il tipo della fonte e' conservato come dato (`sourceType`).
 */
import { type CsvRow, isEmptyRow, parseCsv } from "../../domain/csv.ts";
import {
  calendarDateIn,
  type DateFormat,
  type DateTimeFormat,
  parseDate,
  parseDateTime,
  parseLocalizedAmount,
  parseSpreadsheetNumber,
  spreadsheetSerialToDate,
} from "../../domain/localized.ts";
import { isSupportedCurrency, MoneyError } from "../../domain/money.ts";
import type {
  ExcludedRow,
  ParseContext,
  ParsedBalance,
  ParsedTransaction,
  ParseIssue,
  ParseNotice,
  ParseResult,
  StatementParser,
  TxnStatus,
} from "../types.ts";
import { type Cell, readSheet, XlsxError } from "../xlsx/sheet.ts";
import { isZip } from "../xlsx/zip.ts";
import { DATETIME_FORMATS, type DateField, type TabularSpec, validateTabularSpec } from "./spec.ts";

interface Row {
  line: number;
  cells: readonly Cell[];
}

const FAIL = (issues: ParseIssue[], notices: ParseNotice[] = []): ParseResult => ({
  transactions: [],
  balances: [],
  issues,
  notices,
});

function issue(line: number | null, field: string | null, code: string, message: string): ParseIssue {
  return { line, field, code, message };
}

/** Riconosce il tipo reale del file dai primi byte, per rifiutare esplicitamente cio' che non e' supportato. */
export function sniffFileKind(bytes: Uint8Array): "zip" | "ole2" | "html" | "pdf" | "text" {
  if (isZip(bytes)) return "zip";
  const sig = [0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1];
  if (sig.every((b, i) => bytes[i] === b)) return "ole2";
  const head = new TextDecoder("latin1").decode(bytes.subarray(0, 512)).trimStart().toLowerCase();
  if (head.startsWith("%pdf")) return "pdf";
  if (head.startsWith("<!doctype html") || head.startsWith("<html") || head.startsWith("<table"))
    return "html";
  return "text";
}

async function readRows(
  spec: TabularSpec,
  bytes: Uint8Array,
): Promise<{ rows: Row[]; date1904: boolean } | ParseIssue> {
  const kind = sniffFileKind(bytes);
  const unsupported: Record<string, string> = {
    ole2: "file Excel 97-2003 (.xls binario): formato non supportato; serve XLSX o CSV",
    html: "file HTML (spesso salvato con estensione .xls): formato non supportato",
    pdf: "file PDF: formato non supportato per l'importazione",
  };
  const msg = unsupported[kind];
  if (msg !== undefined) return issue(null, null, "unsupported_file_type", msg);
  if (spec.input.kind === "xlsx") {
    if (kind !== "zip") return issue(null, null, "unsupported_file_type", "atteso un file XLSX");
    try {
      const sheet = await readSheet(bytes, spec.input.sheet, spec.input.sheetPrefix);
      return { rows: sheet.rows.map((r) => ({ line: r.line, cells: r.cells })), date1904: sheet.date1904 };
    } catch (err) {
      if (err instanceof XlsxError) return issue(null, null, "xlsx_malformed", err.message);
      throw err;
    }
  }
  if (kind === "zip")
    return issue(null, null, "unsupported_file_type", "atteso un file CSV, ricevuto un archivio (XLSX?)");
  let text: string;
  try {
    text = new TextDecoder(spec.input.encoding, { fatal: true }).decode(bytes);
  } catch {
    return issue(null, null, "invalid_encoding", `il file non e' testo ${spec.input.encoding} valido`);
  }
  let csv: CsvRow[];
  try {
    csv = parseCsv(text, spec.input.delimiter);
  } catch (err) {
    return issue(null, null, "csv_malformed", err instanceof Error ? err.message : String(err));
  }
  return {
    rows: csv.map((r) => ({
      line: r.line,
      cells: r.fields.map((f) => ({ type: "string" as const, text: f })),
    })),
    date1904: false,
  };
}

const cellText = (c: Cell | undefined) => (c?.text ?? "").trim();

export function createTabularParser(spec: TabularSpec): StatementParser {
  const problems = validateTabularSpec(spec);
  if (problems.length > 0) throw new Error(`specifica ${spec.id} non valida:\n- ${problems.join("\n- ")}`);

  return {
    id: spec.id,
    version: spec.version,
    description: spec.description,
    verifiedAgainstRealExports: spec.verifiedAgainstRealExports,

    async parseFile(bytes: Uint8Array, ctx: ParseContext): Promise<ParseResult> {
      const read = await readRows(spec, bytes);
      if ("code" in read) return FAIL([read]);
      const { rows, date1904 } = read;
      const notices: ParseNotice[] = [];
      const expected = spec.header.columns;

      // 1. intestazione
      const searchable = rows.slice(0, spec.header.maxRowsBefore + 1);
      const headerIdx = searchable.findIndex((r) => {
        const names = r.cells.map(cellText);
        return expected.every((c) => names.includes(c));
      });
      if (headerIdx === -1) {
        return FAIL([
          issue(
            null,
            null,
            "header_not_found",
            `intestazione attesa non trovata nelle prime ${spec.header.maxRowsBefore + 1} righe (colonne: ${expected.join(", ")})`,
          ),
        ]);
      }
      const headerRow = rows[headerIdx] as Row;
      const names = headerRow.cells.map(cellText);
      const headerIssues: ParseIssue[] = [];
      const seen = new Set<string>();
      for (const n of names) {
        if (n === "") continue;
        if (seen.has(n))
          headerIssues.push(issue(headerRow.line, n, "duplicate_column", `colonna ripetuta: ${n}`));
        seen.add(n);
        if (!expected.includes(n) && spec.header.extraColumns === "reject") {
          headerIssues.push(issue(headerRow.line, n, "unknown_column", `colonna non prevista: ${n}`));
        }
      }
      if (headerIssues.length > 0) return FAIL(headerIssues);
      if (headerIdx > 0) {
        notices.push({
          code: "rows_before_header",
          message: "righe prima dell'intestazione (non salvate)",
          count: headerIdx,
        });
      }
      const colIndex = new Map(names.map((n, i) => [n, i] as const).filter(([n]) => n !== ""));

      // 2. righe di dati
      const transactions: ParsedTransaction[] = [];
      const excluded: ExcludedRow[] = [];
      const issues: ParseIssue[] = [];
      const ids = new Map<string, number>();
      // righe per la catena dei saldi (solo con `balanceAfter`): incluse ed escluse, nell'ordine del file
      const balanceCol = spec.fields.balanceAfter?.column ?? null;
      const chain: {
        line: number;
        raw: Record<string, string>;
        date: string;
        delta: number;
        balance: number;
      }[] = [];
      let emptyRows = 0;
      let footerRows = 0;
      let inFooter = false;

      for (const row of rows.slice(headerIdx + 1)) {
        const texts = row.cells.map((c) => c.text);
        if (inFooter) {
          if (!isEmptyRow(texts)) footerRows += 1;
          continue;
        }
        if (isEmptyRow(texts)) {
          emptyRows += 1;
          continue;
        }
        const first = texts.find((t) => t.trim() !== "")?.trim() ?? "";
        if ((spec.footerMarkers ?? []).some((m) => first.startsWith(m))) {
          inFooter = true;
          footerRows += 1;
          continue;
        }
        const extra = texts.slice(names.length).some((t) => t.trim() !== "");
        if (extra) {
          issues.push(
            issue(row.line, null, "wrong_field_count", "valori oltre le colonne dell'intestazione"),
          );
          continue;
        }
        const raw: Record<string, string> = {};
        for (const [n, i] of colIndex) raw[n] = row.cells[i]?.text ?? "";
        const cell = (col: string) => row.cells[colIndex.get(col) ?? -1];
        const rowIssues: ParseIssue[] = [];
        const bad = (field: string, code: string, message: string) =>
          rowIssues.push(issue(row.line, field, code, message));
        const required = (col: string) => {
          const v = cellText(cell(col));
          if (v === "") bad(col, "missing_value", `valore obbligatorio mancante in "${col}"`);
          return v;
        };

        const sourceTxnId = spec.fields.id === null ? null : cellText(cell(spec.fields.id)) || null;
        if (spec.fields.id !== null && sourceTxnId === null)
          bad(spec.fields.id, "missing_value", "identificativo mancante");
        // identificativi ripetuti: controllati su TUTTE le righe, escluse comprese (stesso ID "annullato" e
        // "completato" nello stesso file e' ambiguo: si rifiuta finche' un export reale non chiarisce il caso)
        if (sourceTxnId !== null) {
          const firstLine = ids.get(sourceTxnId);
          if (firstLine !== undefined)
            bad(spec.fields.id ?? "id", "duplicate_id", `identificativo ripetuto (riga ${firstLine})`);
          else ids.set(sourceTxnId, row.line);
        }

        // stato (prima dei campi descrittivi: le righe escluse non devono avere altri campi validi)
        let status: TxnStatus | null = null;
        const st = spec.fields.status;
        if ("fixed" in st) status = st.fixed;
        else {
          const value = cellText(cell(st.column));
          const target = Object.hasOwn(st.map, value) ? st.map[value] : undefined;
          if (target === undefined) {
            bad(st.column, "unknown_status", `stato non previsto dalla specifica: "${value}"`);
          } else if (typeof target === "object") {
            if (rowIssues.length === 0 && balanceCol !== null) {
              // una riga esclusa resta nella catena con importo zero: se il saldo cambia, il file e' rifiutato
              const d = readDate(
                spec.fields.transactionDate,
                cell(spec.fields.transactionDate.column),
                date1904,
                spec,
                bad,
              );
              const b = requiredNumber(cell(balanceCol), ctx.accountCurrency, spec, balanceCol, bad);
              if (d !== null && b !== null)
                chain.push({ line: row.line, raw, date: d, delta: 0, balance: b });
            }
            if (rowIssues.length === 0)
              excluded.push({ line: row.line, raw, reason: target.exclude, sourceTxnId });
            else issues.push(...rowIssues);
            continue;
          } else status = target;
        }

        const currency =
          "fixed" in spec.fields.currency ? ctx.accountCurrency : required(spec.fields.currency.column);
        if (currency !== "" && !isSupportedCurrency(currency)) {
          bad("currency", "unsupported_currency", `valuta non supportata: ${currency}`);
        } else if (currency !== "" && currency !== ctx.accountCurrency) {
          bad(
            "currency",
            "currency_mismatch",
            `valuta ${currency} diversa da quella del conto (${ctx.accountCurrency})`,
          );
        }
        const cur = ctx.accountCurrency;

        const transactionDate = readDate(
          spec.fields.transactionDate,
          cell(spec.fields.transactionDate.column),
          date1904,
          spec,
          bad,
        );
        let bookingDate: string | null = null;
        const bd = spec.fields.bookingDate;
        if (status === "booked") {
          bookingDate =
            bd === "same_as_transaction_date"
              ? transactionDate
              : readDate(bd, cell(bd.column), date1904, spec, bad);
        } else if (
          status === "pending" &&
          bd !== "same_as_transaction_date" &&
          cellText(cell(bd.column)) !== ""
        ) {
          bad(
            bd.column,
            "unexpected_booking_date",
            "riga in attesa con data di contabilizzazione: stato ambiguo",
          );
        }

        const amount = readAmount(spec, cell, cur, bad);

        let originalAmountMinor: number | null = null;
        let originalCurrency: string | null = null;
        if (spec.fields.original !== null) {
          const oa = cellText(cell(spec.fields.original.amount));
          const oc = cellText(cell(spec.fields.original.currency));
          if ((oa === "") !== (oc === "")) {
            bad(
              spec.fields.original.amount,
              "original_incomplete",
              "importo e valuta originali vanno indicati insieme",
            );
          } else if (oa !== "") {
            const o = spec.fields.original;
            if (!isSupportedCurrency(oc))
              bad(o.currency, "unsupported_currency", `valuta non supportata: ${oc}`);
            else {
              const v = number(cell(o.amount), oc, spec, o.amount, bad);
              if (v !== null && amount !== null) {
                let signed: number | null = v;
                if (o.sign === "unsigned") {
                  if (v < 0) {
                    bad(
                      o.amount,
                      "unexpected_sign",
                      "importo originale negativo in una colonna dichiarata senza segno",
                    );
                    signed = null;
                  } else signed = amount < 0 ? -v : v;
                } else if (Math.sign(v) !== Math.sign(amount)) {
                  bad(
                    o.amount,
                    "original_sign_mismatch",
                    "segno dell'importo originale incoerente con l'importo",
                  );
                  signed = null;
                }
                if (signed !== null && oc === cur) {
                  // stessa valuta del conto: l'originale non aggiunge informazione, ma deve coincidere
                  if (signed !== amount)
                    bad(o.amount, "original_mismatch", "importo originale nella stessa valuta ma diverso");
                } else if (signed !== null) {
                  originalAmountMinor = signed;
                  originalCurrency = oc;
                }
              }
            }
          }
        }

        const parts = spec.fields.description.map((c) => cellText(cell(c))).filter((t) => t !== "");
        const description = parts.join(" | ");
        if (description === "")
          bad(spec.fields.description[0] ?? "description", "missing_description", "descrizione mancante");
        if (description.length > 500) bad("description", "too_long", "descrizione oltre 500 caratteri");
        const counterparty =
          spec.fields.counterparty === null ? null : cellText(cell(spec.fields.counterparty)) || null;
        const sourceType =
          spec.fields.sourceType === null ? null : cellText(cell(spec.fields.sourceType)) || null;

        let feeMinor = 0;
        if (spec.fields.fee !== null) {
          const ft = cellText(cell(spec.fields.fee.column));
          if (ft !== "") {
            const v = number(cell(spec.fields.fee.column), cur, spec, spec.fields.fee.column, bad);
            if (v !== null) feeMinor = spec.fields.fee.costSign === "positive" ? v : -v; // > 0 = costo
          }
        }

        let balance: number | null = null;
        if (balanceCol !== null) {
          if (status === "pending")
            bad(balanceCol, "balance_with_pending", "saldo dopo l'operazione su una riga in attesa: ambiguo");
          else balance = requiredNumber(cell(balanceCol), cur, spec, balanceCol, bad);
        }

        if (rowIssues.length > 0 || status === null || transactionDate === null || amount === null) {
          issues.push(...rowIssues);
          continue;
        }
        if (status === "booked" && bookingDate === null) {
          issues.push(issue(row.line, null, "missing_booking_date", "data di contabilizzazione mancante"));
          continue;
        }
        if (balance !== null && bookingDate !== null)
          chain.push({ line: row.line, raw, date: bookingDate, delta: amount, balance });
        const base = {
          line: row.line,
          raw,
          sourceTxnId,
          status,
          transactionDate,
          bookingDate,
          currency: cur,
          counterparty,
          sourceType,
        };
        transactions.push({
          ...base,
          amountMinor: amount,
          originalAmountMinor,
          originalCurrency,
          description,
        });
        if (feeMinor !== 0 && spec.fields.fee !== null && !spec.fields.fee.amountIncludesFee) {
          transactions.push({
            ...base,
            sourceTxnId: sourceTxnId === null ? null : `${sourceTxnId}#fee`,
            amountMinor: -feeMinor,
            originalAmountMinor: null,
            originalCurrency: null,
            description: `Commissione | ${description}`,
            sourceType: "derived:fee",
          });
        }
      }

      if (emptyRows > 0)
        notices.push({ code: "empty_rows", message: "righe vuote ignorate (nessun dato)", count: emptyRows });
      if (footerRows > 0) {
        notices.push({
          code: "footer_rows",
          message: "righe dopo il marcatore di chiusura (non salvate)",
          count: footerRows,
        });
      }
      if (transactions.length === 0 && excluded.length === 0 && issues.length === 0) {
        issues.push(issue(null, null, "no_rows", "nessuna riga di dati"));
      }
      const balances =
        balanceCol === null || issues.length > 0 ? [] : endOfDayBalances(chain, balanceCol, ctx, issues);
      return { transactions, balances, issues, excluded, notices };
    },
  };
}

type Bad = (field: string, code: string, message: string) => void;

/**
 * Catena dei saldi dichiarata dalla fonte: ordine cronologico e saldo precedente + importo = saldo su ogni riga.
 * Restituisce l'ultimo saldo di ogni giorno (fine giornata osservato); un solo errore = nessun saldo e file rifiutato.
 */
function endOfDayBalances(
  chain: readonly {
    line: number;
    raw: Record<string, string>;
    date: string;
    delta: number;
    balance: number;
  }[],
  column: string,
  ctx: ParseContext,
  issues: ParseIssue[],
): ParsedBalance[] {
  const before = issues.length;
  for (let i = 1; i < chain.length; i += 1) {
    const prev = chain[i - 1];
    const cur = chain[i];
    if (prev === undefined || cur === undefined) continue;
    if (cur.date < prev.date)
      issues.push(
        issue(cur.line, column, "balance_order", `riga precedente (${prev.date}) successiva a questa`),
      );
    else if (prev.balance + cur.delta !== cur.balance)
      issues.push(
        issue(
          cur.line,
          column,
          "balance_chain_break",
          `saldo incoerente: precedente ${prev.balance} + importo ${cur.delta} <> ${cur.balance} (unita' minime)`,
        ),
      );
  }
  if (issues.length > before) return [];
  const lastOfDay = new Map<string, (typeof chain)[number]>();
  for (const r of chain) lastOfDay.set(r.date, r);
  return [...lastOfDay.values()].map((r) => ({
    line: r.line,
    raw: r.raw,
    asOfDate: r.date,
    balanceMinor: r.balance,
    currency: ctx.accountCurrency,
  }));
}

function requiredNumber(
  c: Cell | undefined,
  currency: string,
  spec: TabularSpec,
  field: string,
  bad: Bad,
): number | null {
  if (cellText(c) === "") {
    bad(field, "missing_value", `valore obbligatorio mancante in "${field}"`);
    return null;
  }
  return number(c, currency, spec, field, bad);
}

function number(
  c: Cell | undefined,
  currency: string,
  spec: TabularSpec,
  field: string,
  bad: Bad,
): number | null {
  const t = cellText(c);
  try {
    if (c?.type === "number") return parseSpreadsheetNumber(t, currency);
    if (c?.type === "string") return parseLocalizedAmount(t, currency, spec.numbers);
    bad(field, "unexpected_cell_type", `cella di tipo ${c?.type ?? "assente"} dove serve un importo`);
  } catch (err) {
    bad(
      field,
      err instanceof MoneyError ? err.code : "invalid_amount",
      err instanceof Error ? err.message : String(err),
    );
  }
  return null;
}

function readAmount(
  spec: TabularSpec,
  cell: (col: string) => Cell | undefined,
  currency: string,
  bad: Bad,
): number | null {
  const a = spec.fields.amount;
  if (a.kind === "signed") {
    if (cellText(cell(a.column)) === "") {
      bad(a.column, "missing_value", `importo mancante in "${a.column}"`);
      return null;
    }
    const v = number(cell(a.column), currency, spec, a.column, bad);
    return v === null ? null : a.outflowSign === "negative" ? v : -v;
  }
  const dt = cellText(cell(a.debit));
  const ct = cellText(cell(a.credit));
  if ((dt === "") === (ct === "")) {
    bad(
      a.debit,
      "ambiguous_amount",
      dt === "" ? "ne' dare ne' avere valorizzati" : "dare e avere entrambi valorizzati",
    );
    return null;
  }
  if (dt !== "") {
    const v = number(cell(a.debit), currency, spec, a.debit, bad);
    if (v === null) return null;
    if ((a.debitValues === "positive" && v < 0) || (a.debitValues === "negative" && v > 0)) {
      bad(a.debit, "unexpected_sign", `segno inatteso in "${a.debit}"`);
      return null;
    }
    return -Math.abs(v);
  }
  const v = number(cell(a.credit), currency, spec, a.credit, bad);
  if (v === null) return null;
  if (v < 0) {
    bad(a.credit, "unexpected_sign", `importo negativo in "${a.credit}"`);
    return null;
  }
  return v;
}

function readDate(
  f: DateField,
  c: Cell | undefined,
  date1904: boolean,
  spec: TabularSpec,
  bad: Bad,
): string | null {
  const t = cellText(c);
  if (t === "") {
    bad(f.column, "missing_value", `data mancante in "${f.column}"`);
    return null;
  }
  const isDateTime = (DATETIME_FORMATS as readonly string[]).includes(f.format);
  if (c?.type === "number") {
    if (!f.allowSpreadsheetSerial) {
      bad(f.column, "unexpected_cell_type", "data come numero del foglio non ammessa dalla specifica");
      return null;
    }
    const d = spreadsheetSerialToDate(t, date1904);
    if (d === null) bad(f.column, "invalid_date", `numero di serie non valido come data: ${t}`);
    return d;
  }
  if (c?.type !== "string") {
    bad(f.column, "unexpected_cell_type", `cella di tipo ${c?.type ?? "assente"} dove serve una data`);
    return null;
  }
  if (!isDateTime) {
    const d = parseDate(t, f.format as DateFormat);
    if (d === null) bad(f.column, "invalid_date", `data non valida o non nel formato ${f.format}: "${t}"`);
    return d;
  }
  const dt = parseDateTime(t, f.format as DateTimeFormat);
  if (dt === null) {
    bad(f.column, "invalid_date", `data e ora non valide o non nel formato ${f.format}: "${t}"`);
    return null;
  }
  return calendarDateIn(dt, f.timeZone ?? spec.reportingTimeZone, spec.reportingTimeZone);
}
