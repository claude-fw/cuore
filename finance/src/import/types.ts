/**
 * Contratto STABILE tra i parser di formato e il motore di importazione.
 *
 * Un parser trasforma i byte di un file in righe tipizzate, righe escluse (con motivo dichiarato) e problemi.
 * Non accede al database, non deduplica, non classifica. Il motore rifiuta l'intero file se c'e' anche un solo
 * problema (nessuna importazione parziale).
 *
 * `verifiedAgainstRealExports` dichiara se il formato e' stato verificato su esportazioni reali della banca:
 * i parser di esempio NON lo sono, e il sistema non deve far credere il contrario.
 */

export type TxnStatus = "pending" | "booked";

export interface ParsedTransaction {
  /** Riga del file da cui nasce la transazione (piu' transazioni possono nascere dalla stessa riga, es. commissione). */
  readonly line: number;
  /** Campi grezzi esattamente come letti (salvati nello storico immutabile). */
  readonly raw: Readonly<Record<string, string>>;
  readonly sourceTxnId: string | null;
  readonly status: TxnStatus;
  readonly transactionDate: string;
  readonly bookingDate: string | null;
  readonly amountMinor: number;
  readonly currency: string;
  readonly originalAmountMinor: number | null;
  readonly originalCurrency: string | null;
  readonly description: string;
  readonly counterparty: string | null;
  /** Tipo di operazione come riportato dalla fonte (dato osservato, non classificazione). */
  readonly sourceType?: string | null;
}

export interface ParsedBalance {
  readonly line: number;
  readonly raw: Readonly<Record<string, string>>;
  readonly asOfDate: string;
  readonly balanceMinor: number;
  readonly currency: string;
}

/** Riga letta ma esclusa per una regola DICHIARATA nella specifica del formato (mai in silenzio). */
export interface ExcludedRow {
  readonly line: number;
  readonly raw: Readonly<Record<string, string>>;
  readonly reason: string;
  readonly sourceTxnId: string | null;
}

export interface ParseIssue {
  readonly line: number | null;
  readonly field: string | null;
  readonly code: string;
  readonly message: string;
}

/** Informazioni sul file che non sono transazioni (es. righe di intestazione del documento prima della tabella). */
export interface ParseNotice {
  readonly code: string;
  readonly message: string;
  readonly count: number;
}

export interface ParseResult {
  readonly transactions: readonly ParsedTransaction[];
  readonly balances: readonly ParsedBalance[];
  readonly issues: readonly ParseIssue[];
  readonly excluded?: readonly ExcludedRow[];
  readonly notices?: readonly ParseNotice[];
}

export interface ParseContext {
  /** Valuta del conto di destinazione: righe in altra valuta sono un errore (conti multivaluta = un conto per valuta). */
  readonly accountCurrency: string;
}

export interface StatementParser {
  readonly id: string;
  readonly version: number;
  readonly description: string;
  readonly verifiedAgainstRealExports: boolean;
  /** Analizza i byte del file, decodifica compresa (UTF-8 rigoroso, altra codifica dichiarata, o XLSX). */
  parseFile(bytes: Uint8Array, ctx: ParseContext): Promise<ParseResult>;
}

/** Adatta un parser di testo UTF-8 al contratto a byte: UTF-8 non valido = problema esplicito. */
export function utf8TextParser(parse: (text: string, ctx: ParseContext) => ParseResult) {
  return async (bytes: Uint8Array, ctx: ParseContext): Promise<ParseResult> => {
    let text: string;
    try {
      text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch {
      return {
        transactions: [],
        balances: [],
        issues: [{ line: null, field: null, code: "invalid_utf8", message: "il file non e' UTF-8 valido" }],
      };
    }
    return parse(text, ctx);
  };
}
