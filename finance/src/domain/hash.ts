/** Hash e JSON canonico. Usa Web Crypto (globale sia in Node >= 24 sia nei Cloudflare Workers). */

export type Json = string | number | boolean | null | Json[] | { [key: string]: Json };

/** JSON con chiavi ordinate a ogni livello: stesso contenuto -> stessa stringa -> stesso hash. */
export function canonicalJson(value: Json): string {
  return JSON.stringify(sortKeys(value));
}

function sortKeys(value: Json): Json {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value !== null && typeof value === "object") {
    const out: { [key: string]: Json } = {};
    for (const key of Object.keys(value).sort()) {
      const v = value[key];
      if (v !== undefined) out[key] = sortKeys(v);
    }
    return out;
  }
  return value;
}

export async function sha256Hex(data: string | Uint8Array): Promise<string> {
  const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data;
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}
