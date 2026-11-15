import type { StatementParser } from "../types.ts";
import { findPendingFormat, PENDING_BANK_FORMATS } from "./bank-formats.ts";
import { cloudFinanceBalancesCsvV1 } from "./cloud-finance-balances-csv.ts";
import { cloudFinanceCsvV1 } from "./cloud-finance-csv.ts";
import { DOSSIER_PARSERS } from "./dossier-formats.ts";

/**
 * Parser disponibili. Un parser per banca si aggiunge qui SOLO dopo aver verificato il formato su un'esportazione
 * reale (fixture anonimizzata nei test), creandolo con `createTabularParser(spec)` e dichiarando
 * `verifiedAgainstRealExports: true`. I formati bancari solo predisposti stanno in bank-formats.ts.
 */
const PARSERS: readonly StatementParser[] = [
  cloudFinanceCsvV1,
  cloudFinanceBalancesCsvV1,
  ...DOSSIER_PARSERS,
];

export class FormatNotAvailableError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "FormatNotAvailableError";
  }
}

export function getParser(id: string): StatementParser {
  const parser = PARSERS.find((p) => p.id === id);
  if (parser !== undefined) return parser;
  const pending = findPendingFormat(id);
  if (pending !== undefined) {
    throw new FormatNotAvailableError(
      `formato "${id}" (${pending.bank}, ${pending.product}) solo PREDISPOSTO: le colonne non sono ancora verificate. ` +
        `Per completarlo servono:\n- ${pending.needed.join("\n- ")}`,
    );
  }
  throw new FormatNotAvailableError(
    `formato sconosciuto: ${id} (disponibili: ${PARSERS.map((p) => p.id).join(", ")})`,
  );
}

export function listParsers(): readonly StatementParser[] {
  return PARSERS;
}

export interface FormatInfo {
  id: string;
  status: "implementato" | "predisposto";
  description: string;
  verifiedAgainstRealExports: boolean;
}

export function listFormats(): FormatInfo[] {
  return [
    ...PARSERS.map((p) => ({
      id: p.id,
      status: "implementato" as const,
      description: p.description,
      verifiedAgainstRealExports: p.verifiedAgainstRealExports,
    })),
    ...PENDING_BANK_FORMATS.map((f) => ({
      id: f.id,
      status: "predisposto" as const,
      description: `${f.bank} — ${f.product}`,
      verifiedAgainstRealExports: false,
    })),
  ];
}
