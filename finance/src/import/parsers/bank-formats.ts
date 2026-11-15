/**
 * Formati bancari PREDISPOSTI ma NON implementati: le fonti ufficiali indicano il tipo di file, non le colonne.
 * Nessuna intestazione e' stata inventata. Ogni voce dice cosa e' verificato, da dove, e cosa manca.
 * Un formato diventa importabile solo trasformandolo in una TabularSpec verificata su un'esportazione reale
 * (con fixture anonimizzata nei test) e spostandolo nel registro dei parser.
 */

export interface PendingBankFormat {
  readonly id: string;
  readonly bank: string;
  readonly product: string;
  readonly status: "predisposto";
  /** fatti verificati su fonti ufficiali, con la fonte */
  readonly verified: readonly { fact: string; source: string }[];
  /** cio' che serve per scrivere la specifica (senza estratti conto completi) */
  readonly needed: readonly string[];
}

const COMMON_NEEDED = [
  "output (riletto) di `node scripts/probe-format.ts --file <export>` e poi con `--header-row <N>`: analisi locale che mostra solo la forma dei dati (docs/checklist-export-bancari.md); in alternativa i punti seguenti",
  "riga di intestazione ESATTA (copiata dal file, stessi nomi, maiuscole, spazi e ordine)",
  "2-3 righe di dati con valori INVENTATI ma con lo stesso formato (stesse cifre decimali, stesso separatore, stesso formato di data/ora, stessi testi di stato)",
  "se ci sono righe prima dell'intestazione (titolo, dati del conto): solo QUANTE sono e la prima parola di ciascuna, non il contenuto",
  "se ci sono righe dopo la tabella (totali, saldi): la prima parola di ciascuna",
  "tipo di file ottenuto (estensione e, se possibile, se aprendolo con un editor di testo si vede testo leggibile)",
];

export const PENDING_BANK_FORMATS: readonly PendingBankFormat[] = [
  {
    id: "revolut-personal-statement",
    bank: "Revolut",
    product: "Conto personale (estratto per valuta, app Revolut)",
    status: "predisposto",
    verified: [
      {
        fact: "estratto per singola valuta generato in app: tipo documento PDF o Excel",
        source:
          "https://help.revolut.com/en-IT/help/profile-and-plan/managing-my-account/account-statement-per-chosen-currency/",
      },
      {
        fact: "l'estratto contiene transazioni pending, completed e reverted e le transazioni dei Pocket",
        source:
          "https://help.revolut.com/en-IT/help/profile-and-plan/managing-my-account/account-statement-per-chosen-currency/",
      },
      {
        fact: "CSV/CAMT.053/MT940 e orari UTC sono documentati SOLO per Revolut Business: non si applicano al conto personale",
        source:
          "https://help.revolut.com/en-IT/business/help/managing-my-business/viewing-my-account-statements/how-to-get-a-monthly-statement/",
      },
    ],
    needed: [
      ...COMMON_NEEDED,
      "testi ESATTI usati per lo stato (es. per pending, completed, reverted) e per il tipo di operazione",
      "le date hanno anche l'ora? se si', l'app indica se e' ora italiana o UTC?",
      "c'e' una colonna commissione separata? l'importo della riga la comprende gia'?",
      "come compaiono i movimenti dei Pocket (righe separate? colonna dedicata?)",
    ],
  },
  {
    id: "mps-digital-banking-movimenti",
    bank: "Banca Monte dei Paschi di Siena",
    product: "Lista movimenti del conto corrente (sito Digital Banking)",
    status: "predisposto",
    verified: [
      {
        fact: "dal sito Digital Banking (browser) la lista movimenti si salva in PDF o Excel, intera o solo i movimenti selezionati",
        source: "https://www.mps.it/supporto/conti/lista-movimenti.html",
      },
      {
        fact: "sono visibili i movimenti dell'ultimo anno; i movimenti non ancora contabilizzati compaiono in fondo alla lista",
        source: "https://www.mps.it/supporto/conti/lista-movimenti.html",
      },
      {
        fact: "la pagina non documenta l'esportazione dall'app ne' le colonne del file Excel",
        source: "https://www.mps.it/supporto/conti/lista-movimenti.html",
      },
    ],
    needed: [
      ...COMMON_NEEDED,
      "nomi esatti delle colonne delle date (es. data contabile, data valuta) e degli importi (colonna unica con segno oppure dare/avere)",
      "come sono distinti nel file i movimenti non ancora contabilizzati (sezione separata? colonna stato?)",
      "il file Excel e' un vero .xlsx, un .xls binario o una pagina HTML salvata come .xls?",
    ],
  },
];

export function findPendingFormat(id: string): PendingBankFormat | undefined {
  return PENDING_BANK_FORMATS.find((f) => f.id === id);
}
