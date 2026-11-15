/**
 * Lettore CSV RFC 4180 (virgola, doppi apici, "" come apice, CRLF/LF, campi su piu' righe).
 * Non interpreta nulla: restituisce testo e numero di riga di inizio record. Errori strutturali = eccezione.
 */

export interface CsvRow {
  /** Numero della riga del file (1 = prima riga) su cui inizia il record. */
  readonly line: number;
  readonly fields: readonly string[];
}

export class CsvError extends Error {
  readonly line: number;
  constructor(line: number, message: string) {
    super(`riga ${line}: ${message}`);
    this.name = "CsvError";
    this.line = line;
  }
}

export type CsvDelimiter = "," | ";" | "\t";

export function parseCsv(input: string, delimiter: CsvDelimiter = ","): CsvRow[] {
  const text = input.startsWith("﻿") ? input.slice(1) : input;
  const rows: CsvRow[] = [];
  let fields: string[] = [];
  let field = "";
  let inQuotes = false;
  let fieldWasQuoted = false;
  let line = 1;
  let recordLine = 1;
  let i = 0;

  const endField = () => {
    fields.push(field);
    field = "";
    fieldWasQuoted = false;
  };
  const endRecord = () => {
    endField();
    rows.push({ line: recordLine, fields });
    fields = [];
  };

  while (i < text.length) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 2;
          continue;
        }
        inQuotes = false;
        i += 1;
        const next = text[i];
        if (next !== undefined && next !== delimiter && next !== "\n" && next !== "\r") {
          throw new CsvError(line, "carattere inatteso dopo un campo tra apici");
        }
        continue;
      }
      if (ch === "\n") line += 1;
      field += ch;
      i += 1;
      continue;
    }
    if (ch === '"') {
      if (field.length > 0 || fieldWasQuoted)
        throw new CsvError(line, "apice in mezzo a un campo non quotato");
      inQuotes = true;
      fieldWasQuoted = true;
      i += 1;
      continue;
    }
    if (ch === delimiter) {
      endField();
      i += 1;
      continue;
    }
    if (ch === "\r" || ch === "\n") {
      endRecord();
      i += ch === "\r" && text[i + 1] === "\n" ? 2 : 1;
      line += 1;
      recordLine = line;
      continue;
    }
    field += ch;
    i += 1;
  }
  if (inQuotes) throw new CsvError(recordLine, "apice non chiuso");
  if (field.length > 0 || fields.length > 0 || fieldWasQuoted) endRecord();
  return rows;
}

/** Riga composta solo da un campo vuoto (riga bianca). */
export function isBlankRow(row: CsvRow): boolean {
  return row.fields.length === 1 && row.fields[0]?.trim() === "";
}

/** Riga in cui tutti i campi sono vuoti (es. ";;;;" nelle esportazioni da foglio di calcolo). */
export function isEmptyRow(fields: readonly string[]): boolean {
  return fields.every((f) => f.trim() === "");
}
