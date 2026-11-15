/**
 * Lettore ZIP minimo (PKWARE APPNOTE) per i file XLSX: solo lettura, solo metodi 0 (stored) e 8 (deflate).
 * Rifiuta esplicitamente: ZIP64, voci cifrate, metodi sconosciuti, CRC errato, dimensioni oltre i limiti.
 * Usa solo API web standard (DecompressionStream), disponibili in Node >= 24 e nei Cloudflare Workers.
 */

export class ZipError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ZipError";
  }
}

export interface ZipEntry {
  readonly name: string;
  readonly method: number;
  readonly crc32: number;
  readonly compressedSize: number;
  readonly size: number;
  readonly localHeaderOffset: number;
}

export const ZIP_LIMITS = {
  maxEntries: 2000,
  maxEntryBytes: 64 * 1024 * 1024,
  maxTotalBytes: 128 * 1024 * 1024,
} as const;

const u16 = (b: Uint8Array, o: number) => (b[o] ?? 0) | ((b[o + 1] ?? 0) << 8);
const u32 = (b: Uint8Array, o: number) => (u16(b, o) | (u16(b, o + 2) << 16)) >>> 0;

export function isZip(bytes: Uint8Array): boolean {
  return bytes.length >= 4 && u32(bytes, 0) === 0x04034b50;
}

export function listEntries(bytes: Uint8Array): ZipEntry[] {
  let eocd = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 22 - 0xffff); i -= 1) {
    if (u32(bytes, i) === 0x06054b50) {
      eocd = i;
      break;
    }
  }
  if (eocd < 0) throw new ZipError("archivio ZIP non valido (fine della directory non trovata)");
  const count = u16(bytes, eocd + 10);
  const cdSize = u32(bytes, eocd + 12);
  const cdOffset = u32(bytes, eocd + 16);
  if (count === 0xffff || cdSize === 0xffffffff || cdOffset === 0xffffffff) {
    throw new ZipError("ZIP64 non supportato");
  }
  if (count > ZIP_LIMITS.maxEntries) throw new ZipError(`troppe voci nell'archivio (${count})`);
  if (cdOffset + cdSize > bytes.length) throw new ZipError("directory centrale fuori dal file");
  const entries: ZipEntry[] = [];
  let p = cdOffset;
  for (let i = 0; i < count; i += 1) {
    if (u32(bytes, p) !== 0x02014b50) throw new ZipError("voce della directory centrale non valida");
    const flags = u16(bytes, p + 8);
    const method = u16(bytes, p + 10);
    const nameLen = u16(bytes, p + 28);
    const extraLen = u16(bytes, p + 30);
    const commentLen = u16(bytes, p + 32);
    const name = new TextDecoder("utf-8", { fatal: false }).decode(bytes.subarray(p + 46, p + 46 + nameLen));
    if ((flags & 0x1) !== 0) throw new ZipError(`voce cifrata non supportata: ${name}`);
    entries.push({
      name,
      method,
      crc32: u32(bytes, p + 16),
      compressedSize: u32(bytes, p + 20),
      size: u32(bytes, p + 24),
      localHeaderOffset: u32(bytes, p + 42),
    });
    p += 46 + nameLen + extraLen + commentLen;
  }
  return entries;
}

let CRC_TABLE: Uint32Array | null = null;

export function crc32(data: Uint8Array): number {
  if (CRC_TABLE === null) {
    CRC_TABLE = new Uint32Array(256);
    for (let n = 0; n < 256; n += 1) {
      let c = n;
      for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      CRC_TABLE[n] = c >>> 0;
    }
  }
  let crc = 0xffffffff;
  for (const byte of data) crc = (CRC_TABLE[(crc ^ byte) & 0xff] ?? 0) ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

async function inflateRaw(data: Uint8Array, expected: number): Promise<Uint8Array> {
  const stream = new Blob([data]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
  const reader = stream.getReader();
  const out = new Uint8Array(expected);
  let offset = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (offset + value.length > expected) {
      await reader.cancel();
      throw new ZipError("contenuto decompresso piu' grande del dichiarato");
    }
    out.set(value, offset);
    offset += value.length;
  }
  if (offset !== expected) throw new ZipError("contenuto decompresso piu' piccolo del dichiarato");
  return out;
}

export async function readEntry(bytes: Uint8Array, entry: ZipEntry): Promise<Uint8Array> {
  if (entry.size > ZIP_LIMITS.maxEntryBytes) throw new ZipError(`voce troppo grande: ${entry.name}`);
  const p = entry.localHeaderOffset;
  if (u32(bytes, p) !== 0x04034b50) throw new ZipError(`intestazione locale non valida: ${entry.name}`);
  const start = p + 30 + u16(bytes, p + 26) + u16(bytes, p + 28);
  const end = start + entry.compressedSize;
  if (end > bytes.length) throw new ZipError(`dati oltre la fine del file: ${entry.name}`);
  const raw = bytes.subarray(start, end);
  let data: Uint8Array;
  if (entry.method === 0) {
    if (entry.compressedSize !== entry.size) throw new ZipError(`dimensioni incoerenti: ${entry.name}`);
    data = raw;
  } else if (entry.method === 8) {
    data = await inflateRaw(raw, entry.size);
  } else {
    throw new ZipError(`metodo di compressione non supportato (${entry.method}): ${entry.name}`);
  }
  if (crc32(data) !== entry.crc32) throw new ZipError(`CRC errato: ${entry.name}`);
  return data;
}

/** Legge le voci richieste (per nome esatto) con un limite complessivo. Le assenti non compaiono nel risultato. */
export async function readEntries(
  bytes: Uint8Array,
  names: readonly string[],
): Promise<Map<string, Uint8Array>> {
  const entries = listEntries(bytes);
  const out = new Map<string, Uint8Array>();
  let total = 0;
  for (const name of names) {
    const entry = entries.find((e) => e.name === name);
    if (entry === undefined) continue;
    total += entry.size;
    if (total > ZIP_LIMITS.maxTotalBytes) throw new ZipError("contenuto complessivo troppo grande");
    out.set(name, await readEntry(bytes, entry));
  }
  return out;
}
