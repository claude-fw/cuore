/**
 * Formati di DOSSIER DERIVATI (non esportazioni bancarie): ricostruzioni di estratti conto PDF preparate per la
 * revisione umana. Strutture verificate sui file reali (intestazioni, tipi delle celle, formati di data e numero),
 * ma i dati restano derivati: `verifiedAgainstRealExports: false` perche' NON sono export ufficiali della banca.
 *
 * Revolut — workbook "Revolut_Titolare_Cloud_Finance_v3.xlsx" (dal PDF dell'estratto conto):
 *   - un foglio per registro: "Movimenti <titolare>" (conto: l'unico foglio con prefisso "Movimenti "), "Pocket DEMO"
 *     (pocket), "Deposito" (conto deposito);
 *     il foglio della carta e "Verifiche" sono VISTE degli stessi movimenti e non si importano (sarebbero doppioni);
 *   - importo in due colonne (Uscita/Entrata, valori positivi); "Importo €" e' il valore assoluto (solo nel grezzo);
 *   - "Saldo dopo operazione €" dal PDF: catena verificata riga per riga, saldi di fine giornata osservati;
 *   - righe "Zero/ND" (nessun importo, saldo invariato) escluse con motivo dichiarato, la catena lo verifica;
 *   - "Classificazione provvisoria" e "Contesto utente" NON classificano: restano solo nel grezzo;
 *   - "Carta (mascherata)" diventa il tipo di operazione della fonte (`source_type`): vuoto = operazione senza carta.
 * MPS — staging CSV "cloud_finance_mps_transactions_staging.csv" (dai PDF trimestrali):
 *   - nessun identificativo della banca: identita' per impronta + occorrenza;
 *   - data dell'operazione = data VALUTA (verificata sui PDF per tutte le righe), data contabile dalla colonna
 *     `booking_date`, separate; nello staging di esempio alcune righe hanno la valuta al posto della data contabile del
 *     PDF: si correggono in importazione con correzioni dichiarate (src/import/corrections.ts), mai modificando il file.
 */
import { createTabularParser } from "../tabular/engine.ts";
import type { TabularSpec } from "../tabular/spec.ts";
import type { StatementParser } from "../types.ts";

const REVOLUT_V3_COLUMNS = [
  "Data",
  "Descrizione originale",
  "Direzione stimata",
  "Importo €",
  "Uscita €",
  "Entrata €",
  "Saldo dopo operazione €",
  "Carta (mascherata)",
  "Controparte (dal PDF)",
  "Riferimento",
  "ID transazione",
  "Classificazione provvisoria",
  "Dettagli originali",
  "Sezione origine",
  "Contesto utente / classificazione affinata",
];

// Il foglio del conto si chiama "Movimenti <nome del titolare>": il nome non si scrive nel codice, si cerca l'UNICO
// foglio con quel prefisso (dal 2026-10-24; prima il nome fisso "Movimenti Titolare" non trovava il foglio reale).
function revolutV3(id: string, sheet: string, what: string, byPrefix = false): TabularSpec {
  return {
    id,
    version: 2,
    description: `Revolut — dossier v3 (derivato dal PDF), foglio "${byPrefix ? `${sheet}…` : sheet}": ${what}`,
    verifiedAgainstRealExports: false,
    input: byPrefix ? { kind: "xlsx", sheetPrefix: sheet } : { kind: "xlsx", sheet },
    header: { columns: REVOLUT_V3_COLUMNS, extraColumns: "reject", maxRowsBefore: 0 },
    numbers: { decimal: ".", thousands: null },
    reportingTimeZone: "Europe/Rome",
    fields: {
      id: "ID transazione",
      transactionDate: { column: "Data", format: "YYYY-MM-DD" },
      bookingDate: "same_as_transaction_date",
      amount: { kind: "debit_credit", debit: "Uscita €", credit: "Entrata €", debitValues: "positive" },
      currency: { fixed: "account" },
      description: ["Descrizione originale"],
      counterparty: "Controparte (dal PDF)",
      status: {
        column: "Direzione stimata",
        map: {
          Entrata: "booked",
          Uscita: "booked",
          "Zero/ND": {
            exclude: "riga senza importo nel dossier (saldo invariato, verificato dalla catena dei saldi)",
          },
        },
      },
      // carta (mascherata) usata per l'operazione, come scritta dal PDF: dato osservato, usabile da una regola esplicita
      sourceType: "Carta (mascherata)",
      original: null,
      fee: null,
      balanceAfter: { column: "Saldo dopo operazione €" },
    },
  };
}

export const REVOLUT_V3_CONTO = revolutV3(
  "revolut-dossier-v3-conto",
  "Movimenti ",
  "conto del titolare (pagamenti con carta compresi)",
  true,
);
export const REVOLUT_V3_DEMO = revolutV3("revolut-dossier-v3-demo", "Pocket DEMO", "pocket Pocket DEMO");
export const REVOLUT_V3_DEPOSITO = revolutV3("revolut-dossier-v3-deposito", "Deposito", "conto deposito");

export const MPS_STAGING_CSV: TabularSpec = {
  id: "mps-staging-csv",
  version: 2,
  description:
    "MPS — staging CSV derivato dai PDF trimestrali (data operazione = valuta, data contabile dalla sua colonna; nessun id della banca)",
  verifiedAgainstRealExports: false,
  input: { kind: "csv", encoding: "utf-8", delimiter: ";" },
  header: {
    columns: [
      "source_id",
      "booking_date",
      "value_date",
      "description_raw_redacted",
      "direction",
      "amount",
      "currency",
      "signed_amount",
      "classification_status",
      "category",
      "counterparty",
      "source_format",
      "source_file",
      "transaction_id_source",
      "import_status",
    ],
    extraColumns: "reject",
    maxRowsBefore: 0,
  },
  numbers: { decimal: ".", thousands: null },
  reportingTimeZone: "Europe/Rome",
  fields: {
    id: null,
    transactionDate: { column: "value_date", format: "DD/MM/YYYY" },
    bookingDate: { column: "booking_date", format: "DD/MM/YYYY" },
    amount: { kind: "signed", column: "signed_amount", outflowSign: "negative" },
    currency: { column: "currency" },
    description: ["description_raw_redacted"],
    counterparty: "counterparty",
    status: { fixed: "booked" },
    sourceType: null,
    original: null,
    fee: null,
  },
};

export const DOSSIER_PARSERS: readonly StatementParser[] = [
  REVOLUT_V3_CONTO,
  REVOLUT_V3_DEMO,
  REVOLUT_V3_DEPOSITO,
  MPS_STAGING_CSV,
].map(createTabularParser);
