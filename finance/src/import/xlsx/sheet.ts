/**
 * Lettura di un foglio da un file XLSX (Office Open XML, ECMA-376) senza dipendenze.
 * Restituisce le celle con il loro TIPO: i numeri restano testo lessicale (es. "-45.2"), le stringhe condivise
 * vengono risolte, i valori booleani ed errori sono marcati. Nessuna interpretazione di stili o formati numerici:
 * il significato di ogni colonna lo decide la specifica del formato.
 */

import { childElements, firstChild, parseXml, textContent, type XmlElement, XmlError } from "./xml.ts";
import { readEntries, ZipError } from "./zip.ts";

export type CellType = "string" | "number" | "boolean" | "error" | "empty";

export interface Cell {
  readonly type: CellType;
  readonly text: string;
}

export interface SheetRow {
  /** numero di riga del foglio (1 = prima riga) */
  readonly line: number;
  readonly cells: readonly Cell[];
}

export interface Sheet {
  readonly name: string;
  readonly date1904: boolean;
  readonly rows: readonly SheetRow[];
}

export class XlsxError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "XlsxError";
  }
}

const EMPTY: Cell = { type: "empty", text: "" };
const MAX_COLUMNS = 16384;

function decode(bytes: Uint8Array | undefined, what: string): XmlElement {
  if (bytes === undefined) throw new XlsxError(`parte mancante nel file XLSX: ${what}`);
  return parseXml(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
}

function relTargets(rels: XmlElement): { id: string; type: string; target: string }[] {
  return childElements(rels, "Relationship").map((r) => ({
    id: r.attrs.Id ?? "",
    type: r.attrs.Type ?? "",
    target: r.attrs.Target ?? "",
  }));
}

function resolvePath(base: string, target: string): string {
  if (target.startsWith("/")) return target.slice(1);
  const parts = base.split("/").slice(0, -1);
  for (const seg of target.split("/")) {
    if (seg === "..") parts.pop();
    else if (seg !== ".") parts.push(seg);
  }
  return parts.join("/");
}

export function columnIndex(ref: string): number {
  const m = /^([A-Z]{1,3})\d*$/.exec(ref);
  if (m === null) throw new XlsxError(`riferimento di cella non valido: ${ref}`);
  let n = 0;
  for (const ch of m[1] ?? "") n = n * 26 + (ch.charCodeAt(0) - 64);
  if (n > MAX_COLUMNS) throw new XlsxError(`colonna oltre il limite: ${ref}`);
  return n - 1;
}

/**
 * Legge il foglio indicato per nome, oppure l'UNICO foglio il cui nome inizia con `sheetPrefix` (nomi che contengono
 * dati personali, es. "Movimenti <titolare>": la specifica non li scrive), oppure il primo del workbook. Nessuno o piu'
 * fogli col prefisso = errore, mai una scelta. Errori di struttura -> XlsxError con messaggio leggibile; un file non
 * ZIP o cifrato e' rifiutato (vedi zip.ts).
 */
export async function readSheet(bytes: Uint8Array, sheetName?: string, sheetPrefix?: string): Promise<Sheet> {
  try {
    const root = await readEntries(bytes, ["_rels/.rels"]);
    const officeDoc = relTargets(decode(root.get("_rels/.rels"), "_rels/.rels")).find((r) =>
      r.type.endsWith("/officeDocument"),
    );
    if (officeDoc === undefined) throw new XlsxError("documento principale non trovato (_rels/.rels)");
    const workbookPath = resolvePath("", officeDoc.target);
    const relsPath = resolvePath(workbookPath, `_rels/${workbookPath.split("/").pop()}.rels`);
    const parts = await readEntries(bytes, [workbookPath, relsPath]);
    const workbook = decode(parts.get(workbookPath), workbookPath);
    const rels = relTargets(decode(parts.get(relsPath), relsPath));
    const date1904 = ["1", "true"].includes(firstChild(workbook, "workbookPr")?.attrs.date1904 ?? "");
    const sheets = childElements(firstChild(workbook, "sheets") ?? workbook, "sheet");
    if (sheetPrefix !== undefined) {
      const matching = sheets.filter((s) => (s.attrs.name ?? "").startsWith(sheetPrefix));
      if (matching.length !== 1)
        throw new XlsxError(
          `fogli il cui nome inizia con "${sheetPrefix}": ${matching.length} (atteso esattamente uno; fogli nel file: ${sheets.length})`,
        );
    }
    const sheet =
      sheetPrefix !== undefined
        ? sheets.find((s) => (s.attrs.name ?? "").startsWith(sheetPrefix))
        : sheetName === undefined
          ? sheets[0]
          : sheets.find((s) => s.attrs.name === sheetName);
    if (sheet === undefined) {
      throw new XlsxError(
        sheetName === undefined
          ? "nessun foglio nel file"
          : `foglio non trovato: ${sheetName} (presenti: ${sheets.map((s) => s.attrs.name).join(", ")})`,
      );
    }
    const relId = sheet.attrs["r:id"] ?? Object.entries(sheet.attrs).find(([k]) => k.endsWith(":id"))?.[1];
    const sheetRel = rels.find((r) => r.id === relId);
    if (sheetRel === undefined) throw new XlsxError(`relazione del foglio non trovata: ${String(relId)}`);
    const sheetPath = resolvePath(workbookPath, sheetRel.target);
    const sstRel = rels.find((r) => r.type.endsWith("/sharedStrings"));
    const sstPath = sstRel === undefined ? null : resolvePath(workbookPath, sstRel.target);
    const data = await readEntries(bytes, sstPath === null ? [sheetPath] : [sheetPath, sstPath]);

    const shared =
      sstPath === null || !data.has(sstPath)
        ? []
        : childElements(decode(data.get(sstPath), sstPath), "si").map((si) =>
            textContent(si, ["rPh", "phoneticPr"]),
          );

    const ws = decode(data.get(sheetPath), sheetPath);
    const sheetData = firstChild(ws, "sheetData");
    if (sheetData === undefined) throw new XlsxError("foglio senza sheetData");
    const rows: SheetRow[] = [];
    let nextRow = 1;
    for (const r of childElements(sheetData, "row")) {
      const line = r.attrs.r === undefined ? nextRow : Number(r.attrs.r);
      if (!Number.isInteger(line) || line < nextRow)
        throw new XlsxError(`numero di riga non valido: ${r.attrs.r}`);
      nextRow = line + 1;
      const cells: Cell[] = [];
      let nextCol = 0;
      for (const c of childElements(r, "c")) {
        const col = c.attrs.r === undefined ? nextCol : columnIndex(c.attrs.r);
        if (col < nextCol) throw new XlsxError(`celle fuori ordine alla riga ${line}`);
        while (cells.length < col) cells.push(EMPTY);
        cells.push(readCell(c, shared, line));
        nextCol = col + 1;
      }
      rows.push({ line, cells });
    }
    return { name: sheet.attrs.name ?? "", date1904, rows };
  } catch (err) {
    if (err instanceof XlsxError) throw err;
    if (err instanceof ZipError || err instanceof XmlError || err instanceof TypeError) {
      throw new XlsxError(`file XLSX non leggibile: ${err.message}`);
    }
    throw err;
  }
}

function readCell(c: XmlElement, shared: readonly string[], line: number): Cell {
  const t = c.attrs.t ?? "n";
  const v = firstChild(c, "v");
  const vText = v === undefined ? null : textContent(v);
  switch (t) {
    case "s": {
      const idx = Number(vText);
      const s = Number.isInteger(idx) ? shared[idx] : undefined;
      if (s === undefined) throw new XlsxError(`stringa condivisa inesistente alla riga ${line}`);
      return { type: "string", text: s };
    }
    case "inlineStr": {
      const is = firstChild(c, "is");
      return { type: "string", text: is === undefined ? "" : textContent(is, ["rPh", "phoneticPr"]) };
    }
    case "str":
      return vText === null ? EMPTY : { type: "string", text: vText };
    case "b":
      return { type: "boolean", text: vText === "1" ? "TRUE" : "FALSE" };
    case "e":
      return { type: "error", text: vText ?? "" };
    case "n":
      return vText === null || vText === "" ? EMPTY : { type: "number", text: vText.trim() };
    case "d":
      throw new XlsxError(`celle di tipo data ISO (t="d") non supportate alla riga ${line}`);
    default:
      throw new XlsxError(`tipo di cella sconosciuto "${t}" alla riga ${line}`);
  }
}
