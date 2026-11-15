/**
 * Analisi LOCALE e in SOLA LETTURA della struttura di un export bancario, per scrivere la specifica di un formato
 * senza condividere i dati. Non scrive nulla, non salva nulla, non usa la rete.
 *
 * Cosa mostra:
 *   - sempre: tipo di file, codifica, separatore candidato, numero di righe, e la FORMA delle celle (cifre -> 9,
 *     maiuscole -> A, minuscole -> a, spazi compressi, punteggiatura invariata), mai il testo;
 *   - con `headerRow`: i NOMI delle colonne di quella riga (scelta dall'utente dopo aver visto le forme) e, per
 *     colonna, le forme piu' frequenti e i tipi di cella;
 *   - con `valuesOf`: i valori distinti delle colonne indicate SOLO se sono al massimo 15, e in ogni caso i valori che
 *     contengono cifre restano mascherati (pensato per colonne come "stato" o "tipo").
 * L'utente deve comunque rileggere l'output prima di condividerlo.
 */
import { type CsvDelimiter, parseCsv } from "../domain/csv.ts";
import { sniffFileKind } from "./tabular/engine.ts";
import { type CellType, readSheet, XlsxError } from "./xlsx/sheet.ts";

export const MAX_DISTINCT_VALUES = 15;
const SHAPE_MAX = 40;
const FIRST_ROWS = 20;
const LAST_ROWS = 5;

export function maskShape(text: string): string {
  // una sola sostituzione per carattere: una "A" prodotta non deve essere ritrasformata
  const shape = text
    .replace(/\s+/gu, " ")
    .trim()
    .replace(/\p{Nd}|\p{L}/gu, (ch) => (/\p{Nd}/u.test(ch) ? "9" : /\p{Lu}/u.test(ch) ? "A" : "a"));
  return shape.length > SHAPE_MAX ? `${shape.slice(0, SHAPE_MAX)}…` : shape;
}

interface ProbeCell {
  type: CellType;
  text: string;
}
interface ProbeRow {
  line: number;
  cells: ProbeCell[];
}

export interface ProbeOptions {
  /** riga d'intestazione (numero di riga mostrato dalla prima analisi) */
  headerRow?: number;
  /** nomi di colonna (esatti) di cui mostrare i valori distinti */
  valuesOf?: readonly string[];
  /** foglio XLSX (predefinito: il primo) */
  sheet?: string;
  /** separatore CSV (predefinito: il candidato piu' frequente) */
  delimiter?: CsvDelimiter;
}

export interface RowShape {
  line: number;
  filled: number;
  shapes: string[];
}

export interface ProbeReport {
  file: {
    kind: "zip" | "ole2" | "html" | "pdf" | "text";
    bytes: number;
    encoding: string | null;
    bom: boolean;
    lineEndings: "CRLF" | "LF" | "misti" | "nessuno" | null;
    delimiterCounts: Record<string, number> | null;
    delimiterUsed: string | null;
    /** foglio letto (forma del nome); con piu' fogli si sceglie con `sheet` */
    sheet: string | null;
  };
  rows: { total: number; first: RowShape[]; last: RowShape[] } | null;
  header: {
    line: number;
    names: string[];
    columns: {
      name: string;
      filled: number;
      types: Partial<Record<CellType, number>>;
      topShapes: { shape: string; count: number }[];
    }[];
  } | null;
  values: Record<string, { values: string[] } | { refused: string }>;
  warnings: string[];
}

const shapeOf = (c: ProbeCell) =>
  c.type === "string" ? maskShape(c.text) : `${c.type}:${maskShape(c.text)}`;
const isFilled = (c: ProbeCell | undefined) => c !== undefined && c.text.trim() !== "";

function rowShape(r: ProbeRow): RowShape {
  return { line: r.line, filled: r.cells.filter(isFilled).length, shapes: r.cells.map(shapeOf) };
}

export async function probeFile(bytes: Uint8Array, opts: ProbeOptions = {}): Promise<ProbeReport> {
  const warnings: string[] = [];
  const kind = sniffFileKind(bytes);
  const report: ProbeReport = {
    file: {
      kind,
      bytes: bytes.length,
      encoding: null,
      bom: false,
      lineEndings: null,
      delimiterCounts: null,
      delimiterUsed: null,
      sheet: null,
    },
    rows: null,
    header: null,
    values: {},
    warnings,
  };

  let rows: ProbeRow[];
  if (kind === "zip") {
    try {
      const sheet = await readSheet(bytes, opts.sheet);
      rows = sheet.rows.map((r) => ({
        line: r.line,
        cells: r.cells.map((c) => ({ type: c.type, text: c.text })),
      }));
      report.file.sheet = maskShape(sheet.name);
      report.file.encoding = "xlsx (UTF-8 interno)";
    } catch (err) {
      warnings.push(
        `file ZIP non leggibile come XLSX: ${err instanceof XlsxError ? err.message : String(err)}`,
      );
      return report;
    }
  } else if (kind === "text") {
    report.file.bom = bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf;
    let text: string;
    try {
      text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      report.file.encoding = "utf-8";
    } catch {
      text = new TextDecoder("windows-1252").decode(bytes);
      report.file.encoding = "non UTF-8 (letto come windows-1252: da confermare)";
    }
    const crlf = (text.match(/\r\n/g) ?? []).length;
    const lf = (text.match(/\n/g) ?? []).length - crlf;
    report.file.lineEndings =
      crlf + lf === 0 ? "nessuno" : crlf > 0 && lf > 0 ? "misti" : crlf > 0 ? "CRLF" : "LF";
    const sample = text
      .split(/\r?\n/)
      .filter((l) => l.trim() !== "")
      .slice(0, 30)
      .join("\n");
    const counts = { ",": 0, ";": 0, "\t": 0 } as Record<CsvDelimiter, number>;
    for (const ch of sample) if (ch in counts) counts[ch as CsvDelimiter] += 1;
    report.file.delimiterCounts = { virgola: counts[","], puntoEVirgola: counts[";"], tab: counts["\t"] };
    const delimiter =
      opts.delimiter ?? ((Object.entries(counts).sort((a, b) => b[1] - a[1])[0]?.[0] ?? ",") as CsvDelimiter);
    report.file.delimiterUsed = delimiter === "\t" ? "tab" : delimiter;
    try {
      rows = parseCsv(text, delimiter).map((r) => ({
        line: r.line,
        cells: r.fields.map((f) => ({ type: "string" as const, text: f })),
      }));
    } catch (err) {
      warnings.push(
        `CSV non leggibile con il separatore "${report.file.delimiterUsed}": ${err instanceof Error ? err.message : String(err)}`,
      );
      return report;
    }
  } else {
    warnings.push(
      {
        ole2: "Excel 97-2003 binario (.xls): non leggibile dal progetto; serve un'esportazione XLSX o CSV, oppure un lettore dedicato (da decidere)",
        html: "pagina HTML (spesso salvata come .xls): non leggibile dal progetto; serve XLSX o CSV, oppure un lettore dedicato (da decidere)",
        pdf: "PDF: non adatto all'importazione; serve XLSX o CSV",
      }[kind],
    );
    return report;
  }

  report.rows = {
    total: rows.length,
    first: rows.slice(0, FIRST_ROWS).map(rowShape),
    last: rows.length > FIRST_ROWS ? rows.slice(-LAST_ROWS).map(rowShape) : [],
  };

  if (opts.headerRow === undefined) {
    if ((opts.valuesOf ?? []).length > 0) warnings.push("valuesOf richiede headerRow: valori non mostrati");
    return report;
  }
  const headerIdx = rows.findIndex((r) => r.line === opts.headerRow);
  const headerRow = rows[headerIdx];
  if (headerRow === undefined) {
    warnings.push(`riga ${opts.headerRow} non trovata`);
    return report;
  }
  const names = headerRow.cells.map((c) => c.text.trim());
  const data = rows.slice(headerIdx + 1);
  report.header = {
    line: headerRow.line,
    names,
    columns: names.map((name, i) => {
      const cells = data.map((r) => r.cells[i]).filter(isFilled) as ProbeCell[];
      const types: Partial<Record<CellType, number>> = {};
      const shapes = new Map<string, number>();
      for (const c of cells) {
        types[c.type] = (types[c.type] ?? 0) + 1;
        const s = shapeOf(c);
        shapes.set(s, (shapes.get(s) ?? 0) + 1);
      }
      const topShapes = [...shapes.entries()]
        .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
        .slice(0, 3)
        .map(([shape, count]) => ({ shape, count }));
      return { name, filled: cells.length, types, topShapes };
    }),
  };

  for (const wanted of opts.valuesOf ?? []) {
    const i = names.indexOf(wanted);
    if (i === -1) {
      report.values[wanted] = { refused: "colonna non presente nell'intestazione" };
      continue;
    }
    const distinct = [
      ...new Set(
        data
          .map((r) => r.cells[i])
          .filter(isFilled)
          .map((c) => (c as ProbeCell).text.trim()),
      ),
    ];
    if (distinct.length > MAX_DISTINCT_VALUES) {
      report.values[wanted] = {
        refused: `${distinct.length} valori distinti (> ${MAX_DISTINCT_VALUES}): probabile colonna con dati personali, non mostrata`,
      };
      continue;
    }
    report.values[wanted] = {
      values: [...new Set(distinct.map((v) => (/\p{Nd}/u.test(v) ? maskShape(v) : v)))].sort(),
    };
  }
  return report;
}
