# CUORE — contratto degli eventi (`cuore.events/1`) e delle azioni (`cuore.actions/1`)

Stato: **in vigore dal 2026-10-25**. Codice `app/bridge/cuore_events.py`, `app/bridge/cuore_actions.py`,
`app/bridge/cuore_mcp.py`; test `app/tests/test_events.py` (21), `app/tests/test_actions.py` (12). Per Finance vedi anche
[contratto-money.md](contratto-money.md#registro-degli-eventi-cuore-cuoreevents1--adattatore-finance).

## Registro degli eventi
Vista **calcolata in sola lettura** a ogni richiesta dalle fonti che esistono già: nessun database o file nuovo. Due
letture con le stesse fonti e lo stesso `now` danno lo stesso documento (id e ordine deterministici).

| Fonte (`fonti.<nome>`) | Da dove | Stato possibile |
|---|---|---|
| `school.log_job` | log JSONL del job (e ruotati) | `ok`, `con_righe_illeggibili`, `assente` |
| `school.calendario` | calendario del LaunchAgent + log | `ok`, `illeggibile`, `non_configurata` |
| `school.stato_job` | `state.json` del job | `ok`, `illeggibile`, `assente` |
| `school.registry_runs` | Registry, tabella `runs` (copia in memoria) | `ok`, `illeggibile`, `assente` |
| `finance.data_status` | feed Money già calcolato (solo metadati) | `ok`, `illeggibile`, `non_collegata` |
| `finance.sincronizzazione` | stato `cuore.bank.sync_status/1` scritto da `finance/scripts/bank-sync.ts` (`~/Library/Application Support/CUORE/banca-stato.json`) | `ok`, `illeggibile`, `non_configurata` (file assente = nessun collegamento) |
| `core.azioni` | registro delle azioni, solo se indicato | `ok`, `illeggibile` (catena interrotta), `assente`, `non_configurata` |

Ogni fonte riporta `letti`, `scartati` ed `eventi_prodotti` (prima di deduplica e finestra). Il ponte (`/api/events`) e
il server MCP collegano le fonti School e, dal 25/10 notte, Finance (soli metadati di `data_status` dalla cache del feed
Money; feed non calcolabile = `illeggibile`). Il registro delle azioni resta da configurare con una decisione.

### Evento
`id` (`evt:` + 16 esadecimali = hash di area, tipo e chiave naturale), `area`, `tipo`, `natura`, `quando` e `acquisito`
(istanti UTC `+00:00` o `null`), `esito`, `gravita` (`info` | `avviso` | `errore`), `fonte` {`origine`, `riferimento`},
`qualita` (`valido` | `incompleto` = manca l'istante | `timestamp_incoerente` = nel futuro o fine prima dell'inizio),
`dettaglio`. Nessun altro campo.

| Tipo | Area | Natura | Dettaglio ammesso | Riferimento |
|---|---|---|---|---|
| `giro_job` | school | fatto osservato (inferenza se in corso o incompleto) | modo, exit_code, durate, esito per fonte, tentativi, tipi oltre soglia | `avvio <istante>` |
| `anomalia_giro` | school | anomalia | categoria | `fine del giro <istante>` |
| `orario_mancato` | school | inferenza | causa | `orario <istante>` |
| `orario_non_verificabile` | school | non verificato | motivo | `orario <istante>` |
| `problema_aperto` | school | stato corrente | tipo di problema, fonte, giri | `state.json#problemi/<8 hex>` |
| `notifica_inviata` | school | fatto osservato | come sopra | come sopra |
| `chiusura_in_attesa` | school | stato corrente (senza istante; `acquisito` = fine dell'ultimo giro) | tipo di problema, fonte | `state.json#risolti_in_attesa/<8 hex>` |
| `run_registro` | school | fatto osservato | fonte, tipo di dato, modo, durata, letti, conteggi, legacy | `run_id <id>` |
| `importazione_riuscita` | finance | fatto osservato | conto | `conto <id>` |
| `anomalia_dati` | finance | anomalia | conto, codice, periodo, data | `conto <id>` o `generale` |
| `sincronizzazione_bancaria` | finance | fatto osservato (ultimo tentativo) | conto, banca, categoria d'errore, copia o reale, nuovi, aggiornati, ore prima di riprovare | `banca-stato.json#<conto>` |
| `consenso_bancario` | finance | stato corrente, RICALCOLATO a ogni lettura dalla scadenza (in scadenza sotto i 14 giorni) | conto, banca, giorni rimasti, scadenza | `banca-stato.json#<conto>` |
| `azione` | core | fatto osservato (ultimo passaggio) | tipo e area dell'azione, passi, proposta da | `azioni.jsonl#act-<12 hex>` |

### Regole
- **Mai inventato**: un dato assente è `null` (con qualità `incompleto` se è l'istante); una fonte non letta è dichiarata
  in `fonti`, mai trasformata in «nessun evento».
- **Deduplica**: per `id`; la chiave naturale contiene ciò che distingue due fatti (per Finance anche periodo o data
  dichiarati dal motore: due periodi non quadrati dello stesso conto sono due anomalie). `diagnostica`:
  `duplicati_scartati`, `record_non_validi`, `timestamp_incoerenti` (contati **dopo** la deduplica).
- **Ordine**: più recenti prima; eventi senza istante in fondo. Finestra predefinita 7 giorni (gli eventi senza istante restano).
- **Privacy**: chiavi complete dei problemi (possono contenere uid), testi d'errore, titoli e messaggi delle notifiche,
  messaggi e importi di Finance, motivi delle azioni non escono mai. Il validatore rifiuta chiavi del dettaglio fuori
  vocabolario e valori testuali multiriga, oltre 120 caratteri, con `@`, URL o numeri di 5 o più cifre.
- **Validazione prima dell'uso**: il ponte risponde 503 `contratto_non_valido` (o `non_disponibile` se il calcolo fallisce,
  con il solo tipo d'errore) invece di inviare un documento non conforme; il server MCP fa lo stesso. La dashboard mostra
  il motivo nella scheda «Eventi recenti», mai «nessun evento».

## Registro delle azioni
Per le azioni che **modificano** dati o configurazione (vocabolario chiuso): `sincronizza_school`, `ripristina_registry`,
`importa_estratto`, `decidi_revisione`, `modifica_automazione`, `collega_fonte`, `commit_locale`.

| Passaggio | Da | Chi |
|---|---|---|
| `richiesta` (con motivo di una riga e riferimento facoltativo) | — | agente o titolare |
| `approvata` / `rifiutata` | richiesta | solo titolare: CLI da terminale interattivo, ridigitando il codice dell'azione |
| `eseguita` / `fallita` (con riferimento, es. hash del rapporto) | approvata, entro 24 ore | esecutore o titolare |
| `annullata` | richiesta o approvata | titolare o agente |

- L'esecuzione avviene **fuori** dal registro, con gli strumenti esistenti; prima di agire l'esecutore controlla
  `approved_and_valid` (approvata, non scaduta, non ancora eseguita). Un'approvazione vale una sola esecuzione.
- File JSON a righe, una riga per passaggio, con `prev` e `hash` (sha256 della riga canonica): una riga modificata, tolta,
  riordinata o illeggibile rompe la catena; `verifica` lo dichiara e il registro non accetta nuove righe.
- Lock esclusivo e `O_APPEND` (scritture concorrenti da più processi verificate), permessi 0600, **nessun percorso
  predefinito**: il registro esiste solo dove il titolare lo indica.
- Il controllo del terminale interattivo è una procedura, non una barriera crittografica: impedisce a un agente che esegue
  comandi senza terminale di approvare, non a chi ha accesso al Mac.

## Ricerca globale (`cuore.search/1`, `app/bridge/cuore_search.py`)
Una sola implementazione per la dashboard (`/api/search?q=`, sezione Sistema → Cerca) e per l'agente (`cuore_cerca`).
Dal 25/10 sera anche l'indice Classroom (`app/bridge/cuore_classroom.py`, `cuore.school.classroom_index/1`): corsi,
argomenti, compiti, annunci e riferimenti ai materiali del catalogo M15 con il **link originale** (solo https) e lo stato
del contenuto («non letto» / «letto (archivio materiali)»). Si cerca in titolo, corso, argomento, materia e testo di
compiti e annunci; il contenuto dei file non è indicizzato perché non è letto.
Cerca solo nei feed già esposti, in memoria: School (materie e voci dei blocchi, campi descrittivi), Money (conti,
categorie, obiettivi, acquisti, movimenti degli ultimi 30 giorni: per l'agente solo con `movimenti: true`), eventi
validati. Tutte le parole, senza accenti e maiuscole; titoli prima, poi i più recenti. Ogni risultato: modulo, tipo,
titolo, dettaglio, data, rotta della dashboard, provenienza (pagina e blocco o operazione); mai uid, hash o versioni.
`fonti`: `letta` | `non_disponibile` (con il solo tipo d'errore) | `non_collegata`. Domanda fuori da 2-80 caratteri: 400.

## Accesso dell'agente (`cuore_mcp.py`, non registrato)
Strumenti `cuore_cerca` (ricerca globale), `cuore_eventi` (registro validato, filtrabile per area; default solo gli eventi da guardare), `cuore_azioni`
(stato e integrità, con `eseguibile`), `cuore_proponi_azione` (solo richieste). Nessuno strumento per approvare o
eseguire. Senza `--azioni FILE` gli strumenti delle azioni rispondono «non configurato». Registrazione in un client:
solo con autorizzazione del titolare.
