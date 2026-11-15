/**
 * Costruttore di file XLSX SINTETICI per i test, indipendente dal lettore: compressione e CRC32 da node:zlib.
 * Produce la struttura minima prevista da ECMA-376: [Content_Types].xml, _rels/.rels, xl/workbook.xml,
 * xl/_rels/workbook.xml.rels, xl/worksheets/sheetN.xml, xl/sharedStrings.xml.
 */
import { crc32, deflateRawSync } from "node:zlib";

export type XCell =
  | null
  | string
  | number
  | { inline: string }
  | { bool: boolean }
  | { error: string }
  | { raw: string; type: string }
  | { rich: string[]; phonetic?: string };

export interface XSheet {
  name: string;
  rows: XCell[][];
  /** righe con numero esplicito (per righe mancanti) */
  rowNumbers?: number[];
}

const esc = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

function colName(i: number): string {
  let n = i + 1;
  let s = "";
  while (n > 0) {
    const r = (n - 1) % 26;
    s = String.fromCharCode(65 + r) + s;
    n = Math.floor((n - 1) / 26);
  }
  return s;
}

export function buildXlsxParts(sheets: XSheet[], opts: { date1904?: boolean } = {}): Map<string, string> {
  const shared: string[] = [];
  const sharedXml: string[] = [];
  const sharedIndex = (s: string) => {
    let i = shared.indexOf(s);
    if (i === -1) {
      i = shared.length;
      shared.push(s);
      sharedXml.push(`<si><t xml:space="preserve">${esc(s)}</t></si>`);
    }
    return i;
  };
  const parts = new Map<string, string>();
  sheets.forEach((sheet, si) => {
    const rowsXml = sheet.rows
      .map((row, ri) => {
        const r = sheet.rowNumbers?.[ri] ?? ri + 1;
        const cells = row
          .map((c, ci) => {
            if (c === null) return "";
            const ref = `${colName(ci)}${r}`;
            if (typeof c === "string") return `<c r="${ref}" t="s"><v>${sharedIndex(c)}</v></c>`;
            if (typeof c === "number") return `<c r="${ref}"><v>${c}</v></c>`;
            if ("inline" in c) return `<c r="${ref}" t="inlineStr"><is><t>${esc(c.inline)}</t></is></c>`;
            if ("bool" in c) return `<c r="${ref}" t="b"><v>${c.bool ? 1 : 0}</v></c>`;
            if ("error" in c) return `<c r="${ref}" t="e"><v>${esc(c.error)}</v></c>`;
            if ("raw" in c) return `<c r="${ref}" t="${c.type}"><v>${c.raw}</v></c>`;
            const idx = shared.length;
            shared.push(c.rich.join(""));
            sharedXml.push(
              `<si>${c.rich.map((t) => `<r><rPr><b/></rPr><t>${esc(t)}</t></r>`).join("")}${
                c.phonetic === undefined ? "" : `<rPh sb="0" eb="1"><t>${esc(c.phonetic)}</t></rPh>`
              }</si>`,
            );
            return `<c r="${ref}" t="s"><v>${idx}</v></c>`;
          })
          .join("");
        return `<row r="${r}">${cells}</row>`;
      })
      .join("");
    parts.set(
      `xl/worksheets/sheet${si + 1}.xml`,
      `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>${rowsXml}</sheetData></worksheet>`,
    );
  });
  parts.set(
    "[Content_Types].xml",
    `<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/></Types>`,
  );
  parts.set(
    "_rels/.rels",
    `<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>`,
  );
  parts.set(
    "xl/workbook.xml",
    `<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">${
      opts.date1904 ? '<workbookPr date1904="1"/>' : "<workbookPr/>"
    }<sheets>${sheets.map((s, i) => `<sheet name="${esc(s.name)}" sheetId="${i + 1}" r:id="rId${i + 1}"/>`).join("")}</sheets></workbook>`,
  );
  parts.set(
    "xl/_rels/workbook.xml.rels",
    `<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">${sheets
      .map(
        (_, i) =>
          `<Relationship Id="rId${i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet${i + 1}.xml"/>`,
      )
      .join(
        "",
      )}<Relationship Id="rIdS" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/></Relationships>`,
  );
  parts.set(
    "xl/sharedStrings.xml",
    `<?xml version="1.0" encoding="UTF-8"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="${shared.length}" uniqueCount="${shared.length}">${sharedXml.join("")}</sst>`,
  );
  return parts;
}

export interface ZipOptions {
  compress?: boolean;
  /** forza un metodo di compressione (per i test di errore) */
  method?: number;
  /** imposta il bit "cifrato" */
  encrypted?: boolean;
}

export function zip(files: Map<string, string | Uint8Array>, opts: ZipOptions = {}): Uint8Array {
  const locals: Buffer[] = [];
  const centrals: Buffer[] = [];
  let offset = 0;
  for (const [name, content] of files) {
    const data = typeof content === "string" ? Buffer.from(content, "utf8") : Buffer.from(content);
    const compress = opts.compress ?? true;
    const method = opts.method ?? (compress ? 8 : 0);
    const body = method === 8 ? deflateRawSync(data) : data;
    const crc = crc32(data);
    const nameBuf = Buffer.from(name, "utf8");
    const flags = 0x0800 | (opts.encrypted ? 1 : 0);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(flags, 6);
    local.writeUInt16LE(method, 8);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(body.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBuf.length, 26);
    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt16LE(flags, 8);
    central.writeUInt16LE(method, 10);
    central.writeUInt32LE(crc, 16);
    central.writeUInt32LE(body.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(nameBuf.length, 28);
    central.writeUInt32LE(offset, 42);
    locals.push(local, nameBuf, body);
    centrals.push(central, nameBuf);
    offset += local.length + nameBuf.length + body.length;
  }
  const cd = Buffer.concat(centrals);
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(files.size, 8);
  eocd.writeUInt16LE(files.size, 10);
  eocd.writeUInt32LE(cd.length, 12);
  eocd.writeUInt32LE(offset, 16);
  return new Uint8Array(Buffer.concat([...locals, cd, eocd]));
}

export function buildXlsx(sheets: XSheet[], opts: { date1904?: boolean } & ZipOptions = {}): Uint8Array {
  return zip(buildXlsxParts(sheets, opts) as Map<string, string | Uint8Array>, opts);
}
