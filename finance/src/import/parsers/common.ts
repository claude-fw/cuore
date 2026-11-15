/** Utilita' condivise dai parser CSV a intestazione fissa. */
import { type CsvRow, isBlankRow, parseCsv } from "../../domain/csv.ts";
import type { ParseIssue } from "../types.ts";

export interface HeaderedRows {
  readonly rows: { line: number; values: Record<string, string> }[];
  readonly issues: ParseIssue[];
}

export function issue(line: number | null, field: string | null, code: string, message: string): ParseIssue {
  return { line, field, code, message };
}

/**
 * Legge un CSV con intestazione obbligatoria. Le colonne devono essere ESATTAMENTE quelle previste (ordine libero):
 * colonne mancanti, sconosciute o ripetute rendono il file non valido. Le righe vuote sono ignorate.
 */
export function readHeaderedCsv(text: string, columns: readonly string[]): HeaderedRows {
  const issues: ParseIssue[] = [];
  let parsed: CsvRow[];
  try {
    parsed = parseCsv(text);
  } catch (err) {
    const line = err instanceof Error && "line" in err && typeof err.line === "number" ? err.line : null;
    return {
      rows: [],
      issues: [issue(line, null, "csv_malformed", err instanceof Error ? err.message : String(err))],
    };
  }
  const nonBlank = parsed.filter((r) => !isBlankRow(r));
  const header = nonBlank[0];
  if (header === undefined) return { rows: [], issues: [issue(null, null, "empty_file", "file vuoto")] };

  const names = header.fields.map((f) => f.trim());
  const seen = new Set<string>();
  for (const name of names) {
    if (seen.has(name))
      issues.push(issue(header.line, name, "duplicate_column", `colonna ripetuta: ${name}`));
    seen.add(name);
    if (!columns.includes(name))
      issues.push(issue(header.line, name, "unknown_column", `colonna sconosciuta: ${name}`));
  }
  for (const col of columns) {
    if (!seen.has(col)) issues.push(issue(header.line, col, "missing_column", `colonna mancante: ${col}`));
  }
  if (issues.length > 0) return { rows: [], issues };

  const rows: { line: number; values: Record<string, string> }[] = [];
  for (const row of nonBlank.slice(1)) {
    if (row.fields.length !== names.length) {
      issues.push(
        issue(
          row.line,
          null,
          "wrong_field_count",
          `attesi ${names.length} campi, trovati ${row.fields.length}`,
        ),
      );
      continue;
    }
    const values: Record<string, string> = {};
    names.forEach((name, i) => {
      values[name] = row.fields[i] ?? "";
    });
    rows.push({ line: row.line, values });
  }
  if (rows.length === 0 && issues.length === 0)
    issues.push(issue(null, null, "no_rows", "nessuna riga di dati"));
  return { rows, issues };
}

/** Valore di una colonna (sempre presente dopo readHeaderedCsv), senza spazi esterni. */
export function col(values: Record<string, string>, name: string): string {
  return (values[name] ?? "").trim();
}
