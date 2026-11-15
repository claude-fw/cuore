/**
 * Formati SINTETICI per i test ("esempio-a/b/c"): NON sono formati bancari e non vengono registrati.
 * Modulo senza API di Node: usato sia dai test in Node sia dal Worker di prova eseguito in workerd.
 */
import type { TabularSpec } from "../../src/import/tabular/spec.ts";

// ---------- formato sintetico A: CSV UTF-8, data e ora UTC, importo con segno, stato, commissione, id ----------
export const A_COLUMNS = [
  "ID",
  "Data operazione",
  "Data contabile",
  "Stato",
  "Tipo",
  "Descrizione",
  "Controparte",
  "Importo",
  "Commissione",
  "Valuta",
  "Importo originale",
  "Valuta originale",
];

export const SPEC_A: TabularSpec = {
  id: "esempio-a",
  version: 1,
  description: "Formato SINTETICO A (solo test)",
  verifiedAgainstRealExports: false,
  input: { kind: "csv", encoding: "utf-8", delimiter: "," },
  header: { columns: A_COLUMNS, extraColumns: "reject", maxRowsBefore: 0 },
  numbers: { decimal: ".", thousands: null },
  reportingTimeZone: "Europe/Rome",
  fields: {
    id: "ID",
    transactionDate: { column: "Data operazione", format: "YYYY-MM-DD HH:mm:ss", timeZone: "UTC" },
    bookingDate: { column: "Data contabile", format: "YYYY-MM-DD HH:mm:ss", timeZone: "UTC" },
    amount: { kind: "signed", column: "Importo", outflowSign: "negative" },
    currency: { column: "Valuta" },
    description: ["Descrizione"],
    counterparty: "Controparte",
    status: {
      column: "Stato",
      map: {
        COMPLETATA: "booked",
        IN_ATTESA: "pending",
        ANNULLATA: { exclude: "operazione annullata dalla fonte" },
      },
    },
    sourceType: "Tipo",
    original: { amount: "Importo originale", currency: "Valuta originale", sign: "same_as_amount" },
    fee: { column: "Commissione", amountIncludesFee: false, costSign: "positive" },
  },
};

export interface ARow {
  id: string;
  op: string;
  book?: string;
  stato?: string;
  tipo?: string;
  desc?: string;
  cp?: string;
  amount: string;
  fee?: string;
  cur?: string;
  orig?: [string, string];
}

export const q = (s: string) => (/[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s);
export const aLine = (r: ARow) =>
  [
    r.id,
    r.op,
    r.book ?? (r.stato === "IN_ATTESA" ? "" : r.op),
    r.stato ?? "COMPLETATA",
    r.tipo ?? "PAGAMENTO",
    r.desc ?? "Negozio Esempio",
    r.cp ?? "",
    r.amount,
    r.fee ?? "0.00",
    r.cur ?? "EUR",
    r.orig?.[0] ?? "",
    r.orig?.[1] ?? "",
  ]
    .map(q)
    .join(",");
export const aFile = (rows: ARow[], header = A_COLUMNS.join(",")) =>
  new TextEncoder().encode(`${[header, ...rows.map(aLine)].join("\n")}\n`);

// ---------- formato sintetico B: CSV windows-1252, ";" , date italiane, dare/avere, righe prima e dopo ----------
export const SPEC_B: TabularSpec = {
  id: "esempio-b",
  version: 1,
  description: "Formato SINTETICO B (solo test)",
  verifiedAgainstRealExports: false,
  input: { kind: "csv", encoding: "windows-1252", delimiter: ";" },
  header: {
    columns: ["Data contabile", "Data valuta", "Descrizione", "Dare", "Avere"],
    extraColumns: "keep_raw",
    maxRowsBefore: 4,
  },
  footerMarkers: ["Saldo"],
  numbers: { decimal: ",", thousands: "." },
  reportingTimeZone: "Europe/Rome",
  fields: {
    id: null,
    transactionDate: { column: "Data contabile", format: "DD/MM/YYYY", allowSpreadsheetSerial: true },
    bookingDate: "same_as_transaction_date",
    amount: { kind: "debit_credit", debit: "Dare", credit: "Avere", debitValues: "positive" },
    currency: { fixed: "account" },
    description: ["Descrizione"],
    counterparty: null,
    status: { fixed: "booked" },
    sourceType: null,
    original: null,
    fee: null,
  },
};

export const latin1 = (s: string) =>
  Uint8Array.from(s, (c) => {
    const code = c.charCodeAt(0);
    if (code > 255) throw new Error(`carattere fuori da windows-1252 nel test: ${c}`);
    return code;
  });

export const B_PREAMBLE = ["Lista movimenti DEMO;;;;", "Intestatario: PERSONA FITTIZIA;;;;", ";;;;"];
export const B_HEADER = "Data contabile;Data valuta;Descrizione;Dare;Avere;Note";
export const bFile = (
  rows: string[],
  footer: string[] = ["Saldo contabile;;;;1.234,56;"],
  preamble = B_PREAMBLE,
) => latin1([...preamble, B_HEADER, ...rows, ";;;;;", ...footer].join("\r\n"));

export const SPEC_C: TabularSpec = {
  ...SPEC_B,
  id: "esempio-c",
  description: "Formato SINTETICO C (XLSX, solo test)",
  input: { kind: "xlsx" },
};
