/**
 * Fonte di dati di un conto per la sincronizzazione (accesso ai conti PSD2 tramite un fornitore AISP, o una fonte
 * sintetica nei test). La fonte LEGGE soltanto: non scrive nulla e non conosce il database.
 */

/** Categorie d'errore che decidono cosa fa la sincronizzazione (sempre: nessuna importazione parziale). */
export type SourceErrorKind =
  | "consent_expired" // consenso scaduto o revocato, credenziali dell'applicazione rifiutate: serve il titolare
  | "rate_limited" // troppe richieste (limiti del fornitore o della banca, PSD2: 4 accessi al giorno senza l'utente)
  | "temporary" // rete o servizio non disponibile: si riprova al giro successivo
  | "invalid_request" // richiesta rifiutata dal fornitore per un errore di parametri
  | "period_unavailable" // periodo richiesto non disponibile presso la banca (Enable Banking: WRONG_TRANSACTIONS_PERIOD)
  | "invalid_response"; // risposta non interpretabile (forma inattesa, paginazione che non termina)

/** Ore dopo cui riprovare per categoria (null = serve il titolare). FAQ Enable Banking: dopo un limite, 6 ore. */
export const RETRY_AFTER_HOURS: Readonly<Record<SourceErrorKind | "unexpected", number | null>> = {
  consent_expired: null,
  rate_limited: 6,
  temporary: 1,
  invalid_request: null,
  period_unavailable: null,
  invalid_response: null,
  unexpected: null,
};

export class SourceError extends Error {
  readonly kind: SourceErrorKind;
  readonly status: number | null;
  constructor(kind: SourceErrorKind, message: string, status: number | null = null) {
    super(message);
    this.name = "SourceError";
    this.kind = kind;
    this.status = status;
  }
}

export interface TransactionsPage {
  readonly transactions: readonly unknown[];
  /** null = ultima pagina */
  readonly continuationKey: string | null;
}

/**
 * `default`: esattamente il periodo richiesto (aggiornamenti); `longest`: dal primo movimento disponibile in poi,
 * `to` ignorato (prima acquisizione). Una pagina puo' essere VUOTA con una chiave di continuazione: si prosegue.
 */
export type FetchStrategy = "default" | "longest";

export interface AccountDataSource {
  /** nome del fornitore, registrato nella provenienza dell'importazione */
  readonly provider: string;
  transactions(
    accountRef: string,
    from: string,
    to: string,
    continuationKey: string | null,
    strategy?: FetchStrategy,
  ): Promise<TransactionsPage>;
  balances(accountRef: string): Promise<readonly unknown[]>;
}
