/**
 * SPECIFICA DICHIARATIVA di un formato tabellare (CSV o XLSX) di un'esportazione bancaria.
 *
 * Un formato bancario reale si aggiunge scrivendo una specifica (non codice) SOLO dopo averla verificata su
 * un'esportazione reale; i test usano una copia anonimizzata. Tutto cio' che e' ambiguo va dichiarato:
 * formato delle date, fuso orario, separatori numerici, segno delle uscite, valori dello stato e cosa escludere.
 */
import type { CsvDelimiter } from "../../domain/csv.ts";
import type { DateFormat, DateTimeFormat, NumberFormat } from "../../domain/localized.ts";

export const DATE_FORMATS: readonly DateFormat[] = ["YYYY-MM-DD", "DD/MM/YYYY", "DD-MM-YYYY", "DD.MM.YYYY"];
export const DATETIME_FORMATS: readonly DateTimeFormat[] = [
  "YYYY-MM-DD HH:mm:ss",
  "YYYY-MM-DDTHH:mm:ss",
  "DD/MM/YYYY HH:mm:ss",
  "DD/MM/YYYY HH:mm",
];

export interface DateField {
  readonly column: string;
  readonly format: DateFormat | DateTimeFormat;
  /** obbligatorio per i formati con ora: "UTC" oppure lo stesso fuso di `reportingTimeZone` */
  readonly timeZone?: string;
  /** XLSX: accetta numeri di serie interi del foglio come date (solo per formati senza ora) */
  readonly allowSpreadsheetSerial?: boolean;
}

export type AmountField =
  | {
      readonly kind: "signed";
      readonly column: string;
      /** segno con cui la fonte scrive le USCITE */
      readonly outflowSign: "negative" | "positive";
    }
  | {
      /** due colonne (es. "Dare"/"Avere"): esattamente una valorizzata per riga */
      readonly kind: "debit_credit";
      readonly debit: string;
      readonly credit: string;
      /** come la fonte scrive gli importi in "Dare" */
      readonly debitValues: "positive" | "negative";
    };

export type StatusTarget = "pending" | "booked" | { readonly exclude: string };

export interface TabularSpec {
  readonly id: string;
  readonly version: number;
  readonly description: string;
  readonly verifiedAgainstRealExports: boolean;
  readonly input:
    | { readonly kind: "csv"; readonly encoding: "utf-8" | "windows-1252"; readonly delimiter: CsvDelimiter }
    | {
        readonly kind: "xlsx";
        readonly sheet?: string;
        /** in alternativa a `sheet`: l'UNICO foglio il cui nome inizia cosi' (nomi con dati personali) */
        readonly sheetPrefix?: string;
      };
  readonly header: {
    /** intestazioni attese, scritte esattamente come nel file */
    readonly columns: readonly string[];
    /** colonne in piu' rispetto a quelle attese: rifiutare il file o conservarle solo nel grezzo */
    readonly extraColumns: "reject" | "keep_raw";
    /** quante righe al massimo possono precedere l'intestazione (dati del documento: mai salvati) */
    readonly maxRowsBefore: number;
  };
  /** testi con cui inizia la prima cella non vuota di una riga che CHIUDE la tabella (es. "Saldo finale") */
  readonly footerMarkers?: readonly string[];
  readonly numbers: NumberFormat;
  /** fuso in cui si ragiona sulle date di calendario (es. "Europe/Rome") */
  readonly reportingTimeZone: string;
  readonly fields: {
    readonly id: string | null;
    readonly transactionDate: DateField;
    readonly bookingDate: DateField | "same_as_transaction_date";
    readonly amount: AmountField;
    readonly currency: { readonly column: string } | { readonly fixed: "account" };
    /** colonne unite (con " | ") per formare la descrizione; almeno una non vuota per riga */
    readonly description: readonly string[];
    readonly counterparty: string | null;
    readonly status:
      | { readonly column: string; readonly map: Readonly<Record<string, StatusTarget>> }
      | { readonly fixed: "booked" | "pending" };
    /** colonna con il tipo di operazione della fonte: conservato come dato, mai usato per classificare da solo */
    readonly sourceType: string | null;
    readonly original: {
      readonly amount: string;
      readonly currency: string;
      /** "same_as_amount": stesso segno dell'importo (incoerenza = errore); "unsigned": sempre >= 0, prende il segno dell'importo */
      readonly sign: "same_as_amount" | "unsigned";
    } | null;
    readonly fee: {
      readonly column: string;
      /** l'importo della riga comprende gia' la commissione? se no, la commissione diventa una transazione a parte */
      readonly amountIncludesFee: boolean;
      /** come la fonte scrive una commissione pagata */
      readonly costSign: "positive" | "negative";
    } | null;
    /**
     * Colonna con il saldo DOPO ogni operazione (facoltativa). Se dichiarata, il file e' accettato solo se:
     * righe in ordine cronologico, saldo precedente + importo = saldo su OGNI riga (le righe escluse non devono
     * cambiare il saldo: nessun movimento nascosto dietro un'esclusione), nessuna riga in attesa. L'ultimo saldo di
     * ogni giorno diventa un saldo di fine giornata osservato; il saldo prima della prima riga NON viene dedotto.
     */
    readonly balanceAfter?: { readonly column: string } | null;
  };
}

/** Controlla la coerenza interna di una specifica. Restituisce i problemi (vuoto = valida). */
export function validateTabularSpec(spec: TabularSpec): string[] {
  const p: string[] = [];
  const cols = new Set(spec.header.columns);
  if (cols.size !== spec.header.columns.length) p.push("intestazioni ripetute");
  if (spec.header.columns.some((c) => c.trim() === "")) p.push("intestazione vuota");
  const need = (col: string | null | undefined, what: string) => {
    if (col != null && !cols.has(col)) p.push(`${what}: colonna "${col}" non presente in header.columns`);
  };
  const date = (d: DateField, what: string) => {
    need(d.column, what);
    const isDateTime = (DATETIME_FORMATS as readonly string[]).includes(d.format);
    if (!isDateTime && !(DATE_FORMATS as readonly string[]).includes(d.format))
      p.push(`${what}: formato sconosciuto`);
    if (isDateTime && d.timeZone !== "UTC" && d.timeZone !== spec.reportingTimeZone) {
      p.push(`${what}: formato con ora: timeZone obbligatorio ("UTC" o "${spec.reportingTimeZone}")`);
    }
    if (!isDateTime && d.timeZone !== undefined) p.push(`${what}: timeZone ammesso solo per formati con ora`);
    if (isDateTime && d.allowSpreadsheetSerial)
      p.push(`${what}: numeri di serie ammessi solo per date senza ora`);
  };
  const f = spec.fields;
  need(f.id, "id");
  date(f.transactionDate, "transactionDate");
  if (f.bookingDate !== "same_as_transaction_date") date(f.bookingDate, "bookingDate");
  if (f.amount.kind === "signed") need(f.amount.column, "amount");
  else {
    need(f.amount.debit, "amount.debit");
    need(f.amount.credit, "amount.credit");
    if (f.amount.debit === f.amount.credit) p.push("amount: dare e avere nella stessa colonna");
  }
  if ("column" in f.currency) need(f.currency.column, "currency");
  if (f.description.length === 0) p.push("description: almeno una colonna");
  for (const c of f.description) need(c, "description");
  need(f.counterparty, "counterparty");
  need(f.sourceType, "sourceType");
  if ("column" in f.status) {
    need(f.status.column, "status");
    if (Object.keys(f.status.map).length === 0) p.push("status.map vuota");
    for (const [k, v] of Object.entries(f.status.map)) {
      if (typeof v === "object" && v.exclude.trim() === "")
        p.push(`status.map["${k}"]: motivo di esclusione vuoto`);
    }
  }
  if (f.original !== null) {
    need(f.original.amount, "original.amount");
    need(f.original.currency, "original.currency");
  }
  if (f.fee !== null) need(f.fee.column, "fee");
  if (f.balanceAfter != null) {
    need(f.balanceAfter.column, "balanceAfter");
    if (f.fee !== null && !f.fee.amountIncludesFee)
      p.push("balanceAfter: incompatibile con commissioni separate dall'importo (catena dei saldi ambigua)");
  }
  if (spec.numbers.thousands !== null && spec.numbers.thousands === spec.numbers.decimal) {
    p.push("numbers: separatori coincidenti");
  }
  if (spec.input.kind === "csv" && spec.numbers.decimal === spec.input.delimiter) {
    p.push("numbers: separatore decimale uguale al separatore di colonna");
  }
  if (spec.input.kind === "xlsx" && spec.input.sheetPrefix !== undefined) {
    if (spec.input.sheet !== undefined) p.push("input: sheet e sheetPrefix sono alternativi");
    if (spec.input.sheetPrefix.trim() === "") p.push("input.sheetPrefix vuoto");
  }
  if (!Number.isInteger(spec.header.maxRowsBefore) || spec.header.maxRowsBefore < 0)
    p.push("header.maxRowsBefore");
  try {
    new Intl.DateTimeFormat("en", { timeZone: spec.reportingTimeZone });
  } catch {
    p.push(`reportingTimeZone sconosciuto: ${spec.reportingTimeZone}`);
  }
  if (!/^[a-z0-9][a-z0-9.-]{1,63}$/.test(spec.id)) p.push("id del formato non valido");
  return p;
}
