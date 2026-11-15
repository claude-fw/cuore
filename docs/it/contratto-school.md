# CUORE School — contratto dei dati `cuore.school.feed/1`

Stato: **in vigore dal 2026-10-24**. Codice: `app/bridge/cuore_school_contract.py` (validazione e normalizzazione),
applicato in `cuore_school.build_feed`; consumato da `/api/school` e da `app/index.html`. Test:
`app/tests/test_school_contract.py` (dati sintetici); verificato in sola lettura sul feed reale (8 pagine conformi).
Stesse convenzioni di CUORE Money ([contratto-money.md](contratto-money.md)).

## Livelli
| Livello | Responsabilità |
|---|---|
| Motore School (`scuola/mcp/classeviva/school_dashboard.py`) | documenti di pagina `cloud.school.dashboard/1`: blocchi con stato, dati e provenienza; grafici dichiarativi |
| Ponte (`cuore_school.py`) | lettura in sola lettura (Registry in memoria, rete bloccata, file confrontati prima/dopo) |
| Contratto (`cuore_school_contract.py`) | valida ogni pagina, toglie quelle non conformi, rende pubblici solo messaggi fissi, calcola esito e freschezza |
| Dashboard | controllo minimo all'ingresso (schema e versione, struttura principale), poi mostra solo pagine validate |

## Struttura del feed
| Campo | Tipo | Obbligatorio | Significato |
|---|---|---|---|
| `schema` | `"cuore.school.feed/1"` | sì | identificatore e versione; altre versioni sono rifiutate dalla dashboard |
| `esito` | `completo` \| `parziale` \| `non_disponibile` | sì | come Money: `parziale` = lettura verificata con errori; `non_disponibile` = nessuna pagina utilizzabile |
| `sola_lettura_verificata` | booleano | sì | `false` = nessun dato mostrato |
| `generato` | istante ISO con fuso | sì | ora del calcolo |
| `pagine` | oggetto `{nome: documento}` | sì (anche vuoto) | solo pagine conformi (sotto) |
| `errori` | lista `{ambito, codice, messaggio, dettaglio?}` | sì | `messaggio` = testo pubblico fisso del codice; `dettaglio` = solo nome della classe d'eccezione |
| `freschezza` | `{disponibile, stato, soglia_ore?, per_fonte?, tipi_obsoleti?}` | sì | `stato`: `aggiornati` \| `dati_vecchi` \| `fonti_non_lette` \| `non_disponibile` |
| `automazione` | `{disponibile, job?, storico?, problemi?}` | no | ultimo giro del job, storico dei giri (`scuola/mcp/_common/cuore_automation.py`) e problemi aperti del job: `{tipo, fonte, titolo, dal, giri, notificato, in_osservazione}` a vocabolario fisso, mai chiavi o testi liberi (dal 2026-10-25) |
| `versione_motore`, `data_riferimento` | testo | solo se ci sono pagine | presi dalla prima pagina **validata** |
| `materie` | `{chiave: nome}` | solo con lettura verificata | istantanea locale delle materie |
| `contratto` | `{schema, pagine, documentazione}` | sì | autodescrizione per consumatori esterni |
| `scritture` | oggetto di `false` | sì | il ponte non scrive mai |

### Documento di pagina (`cloud.school.dashboard/1`)
Obbligatori: `schema`, `pagina` (uguale alla chiave), `riferimento_temporale.now` (istante con fuso) e
`.data_riferimento` (data), `provenance.dashboard_version`, `blocchi` (lista), `grafici` (lista).
- Blocco: `id`, `titolo`, `stato` ∈ `dati` | `vuoto_valido` | `non_letto` | `non_disponibile` | `non_attivo` (definiti nel
  motore), `provenienza` (oggetto; `contratto` testo se presente); `stato: dati` richiede `dati`.
- Grafico: `id`, `tipo` ∈ `linea` | `barre` | `barre_impilate` | `calendario` | `indicatore`, `provenienza`, `serie` non
  vuota; punti con `y` numerico, tranne `calendario` (`x` data, `y` etichetta non vuota). Il motore emette un grafico solo
  con punti reali: un punto non conforme rende la pagina non conforme.
- Provenienza conservata invariata, tranne `provenance.sorgente`: il percorso assoluto del Registry (con la cartella
  personale) diventa il solo nome del file.

## I sei casi
| Caso | Rappresentazione |
|---|---|
| Valido con dati | `esito: completo`, pagine con blocchi `dati` |
| Valido senza risultati | `esito: completo`; i blocchi vuoti hanno `stato: vuoto_valido` (mai un errore) |
| Parziale | `esito: parziale`; le pagine riuscite ci sono, le altre sono in `errori` con il loro codice |
| Fonte non disponibile | `esito: non_disponibile`, `pagine: {}`, errore `registro_assente` o `materie_non_disponibili`; `freschezza.stato: non_disponibile` |
| Dati obsoleti | `esito` invariato; `freschezza.stato: dati_vecchi` con `tipi_obsoleti`; il banner lo dichiara (mai presentati come aggiornati) |
| Strutturalmente non valido | pagina: tolta, errore `contratto_non_valido`; feed intero (versione o struttura): la dashboard mostra «dati reali non disponibili» con un motivo generico, senza dettagli tecnici |

Freschezza non determinabile (pagina «qualità dati» assente o non conforme) = `disponibile: false`,
`stato: non_disponibile`: nessuna freschezza assegnata d'ufficio.

## Codici d'errore
| Codice | Messaggio pubblico |
|---|---|
| `registro_assente` | registro locale della scuola assente |
| `materie_non_disponibili` | istantanea delle materie assente o non valida |
| `rete_bloccata` | il motore ha tentato un accesso di rete durante la lettura: pagina non calcolata |
| `errore_motore` | errore del motore nel calcolo della pagina |
| `sola_lettura_violata` | un file letto è cambiato durante la lettura: nessun dato mostrato |
| `contratto_non_valido` | pagina non conforme al contratto: non mostrata |

## Uso da parte di altri consumatori (es. un agente)
`cuore_school_contract.validate_feed(feed)` restituisce l'elenco dei problemi (vuoto = conforme);
`validate_page(doc, nome)` vale per una singola pagina. Un consumatore dovrebbe: rifiutare `schema` diversi; trattare
`esito` e `freschezza.stato` come vincoli sulle risposte (mai «aggiornato» se `dati_vecchi`); usare la `provenienza` dei
blocchi per citare la fonte; non interpretare `vuoto_valido` come errore.

## Costi, prestazioni e cache
- La validazione costa circa 2 ms sul feed reale: irrilevante rispetto al calcolo delle pagine.
- Calcolo delle pagine (misure del 2026-10-24, stesso istante e stesso metodo, mediana; impronta canonica di pagine,
  esito, freschezza, errori, versione e materie identica in tutte le condizioni): reale 38,3 s → 18,7 s; sintetico
  1,78 s → 0,98 s. Due interventi, nessuno cambia l'uscita:
  - `cvv_subject.norm`: `functools.lru_cache` limitata a 65 536 voci. E' una funzione pura testo → testo: la cache vive
    quanto il processo (ponte o server MCP) e non richiede invalidazione.
  - `school_dashboard.PageSession`: una copia in memoria del Registry e un solo insieme di contratti sorgente per tutte le
    pagine di UNA costruzione del feed (stessi istante, manuale, materie, soglia, percorso). Vive solo dentro la
    costruzione (chiusa con `with`), nessuna cache persiste tra un feed e l'altro; riuso con parametri diversi rifiutato;
    ogni pagina dichiara nella provenienza solo i contratti che ha richiesto. Le scritture concorrenti sul Registry non
    toccano la copia in memoria e il confronto dei file prima/dopo scarta il feed. `build_from_path` (strumenti MCP) e'
    invariato.
- Strati di M7 indipendenti dalla materia (`school_context4._shared_layers`: collegamenti M5, comunicazioni M6
  `cvv_comms.compute`, derivazione Classroom L3, argomenti), prima ricalcolati 56 volte per feed: dal 2026-10-24 calcolati
  una volta per `PageSession` (ambito `school_context4.shared_layers`, chiave = copia del Registry, istante, manuale e
  materie; chiave diversa = ricalcolo). Fuori da una sessione (strumenti MCP, `build_from_path`) nessun riuso. Misura
  nello stesso processo e allo stesso istante: reale 19,2 s -> 3,6 s, sintetico 0,99 s -> 0,28 s, impronta del feed
  identica con e senza riuso (anche ricalcolando senza riuso dopo); test `app/tests/test_school_performance.py`.
- Il ponte ricalcola tutto a ogni cambio del Registry o del calendario/log del job (cache in memoria per stato dei file).
- Il contratto verifica la **struttura**, non il contenuto dei blocchi: i campi interni di `dati` sono contratti del
  motore (`DATA_CONTRACT.md`).
- Gli uid delle fonti nella provenienza (es. `cvv:…`) restano: servono a collegare i dati; il feed è servito solo in
  locale.
