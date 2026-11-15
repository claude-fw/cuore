# Architettura scuola: componenti e decisioni (stato 2026-10-03)

> **Avviso (30/10/2026):** Notion è uscito da CUORE il 24/10.
> Le parti su Notion (proiezione, `notion_projection`, F4) sono storiche: oggi la dashboard è l'app CUORE
> (`app/`, feed `cuore.school.feed/1`). Strati L1–L3 e Registry restano validi.

```
FONTI                     L1 RAW IMMUTABILE    L2 NORMALIZZATO   L3 DERIVATO      CONTESTO AGENTI      DASHBOARD
ClasseViva (cvv_client) -> registry.sqlite3 -> cvv_registry   ->              ->  school_context  ->  Notion (proiezione)
Classroom  (cls_client)    versions/events     cls_registry   ->  cls_derive  ->  per materia         notion_projection
                                                                  (work_view)    (selezionato)
                                         L-MAN (futuro): decisioni manuali separate, mai dentro L1/L2
```
Registro locale: `~/Library/Application Support/Cloud/school/registry.sqlite3` (non in iCloud). Notion è proiezione/interfaccia, **mai** fonte di verità.

### Patrimonio completo vs contesto selezionato
```
Classroom → Registry/Cloud (L1) → L2 → L3 cls_derive → school_context → futuro agente materia → Claude
            └──────────── corpus COMPLETO ────────────┘  └ selezione ┘  └ consumatore/interfaccia ┘
```
- **L1/L2/L3 = corpus completo e interrogabile del Cloud.** Tutti i record (987 Classroom + ClasseViva), tutti gli anni, anche quelli che non diventano mai righe Notion.
- **`school_context` = selezione per materia e perimetro** (F3-min, schema `cloud.school.context/2`): compatta, deterministica (stesso `now` → stesso JSON), citata (uid + versione + hash). **Non** carica L2/L3 per intero. La **superficie agent-facing** (`build`, `build_from_path`, tool MCP `school_context`) restituisce **solo il perimetro operativo 3ª/4ª**: lo storico non è esposto agli agenti. `_build(..., ambito, anno)` resta solo interno (test/manutenzione).
- **Futuro agente materia = consumatore/interfaccia** del contesto. Prompt, personalità, autonomia, memoria, Skills e meccanismo Claude (Project/Agent/Skill/MCP/file) **non** stanno in `school_context`: si decidono nella Cloud Academy.
- **L2/L3 e `school_context` non decidono cosa entra in Notion.** Il filtro di proiezione appartiene solo al pianificatore Notion (F4) ed è ancora da definire osservando i dati reali.

### `school_context` v2 (F3-min)
- **Materie** (`school_context.AGENT_SUBJECTS`, decisione dell'utente): `italiano`, `latino`, `inglese`, `storia`, `filosofia`, `matematica`, `fisica`, `scienze`. **Escluse** (nessun contesto, richiesta rifiutata): `arte`, `motorie`, `civica`, `religione`, `studio`. Un contesto = una sola materia; tool `school_subjects` (sola lettura) le elenca.
- **Classroom da L3**: `cls_derive.from_registry` una volta per chiamata; ogni compito è una `work_view` compatta con la **consegna collegata** (nessuna lista separata di consegne; le orfane sono riportate), `stato_operativo`, `anno_compito`, `perimetro`, `fonte` (course_work + submission con uid/versione/hash), `derive_version`.
- **Confermato vs proposto**: `classroom.confermato` usa solo i corsi confermati in `school_manual.json` (letto, mai creato dal codice); `classroom_proposto` è una sezione **separata**, marcata "PROPOSTA NON CONFERMATA", con le proposte di `cls_subjects` (vedi sotto) e le loro evidenze. Nessuna proposta diventa conferma. Corsi `excluded` / `non_attributed` non compaiono in nessun contesto.
- **Corsi condivisi**: un corso confermato per più materie compare in ciascuna, con `condiviso_con[]`; la decisione di materia del compito resta `null` (`ambigua_confermata`) finché non esisterà un override manuale (struttura già prevista in L3).
- **Perimetro del contesto per annunci e materiali**: anno scolastico dalla **data di creazione** (regola di contesto, distinta da `anno_compito` di L3). **Argomenti**: inclusi solo se collegati a compiti o materiali inclusi.
- **Nessun taglio silenzioso**: compiti ordinati con gli `aperto` in testa, poi dal più recente (scadenza, altrimenti creazione); tetto di sicurezza 200 compiti / 30 annunci / 50 materiali con `totale`, `inclusi`, `omessi`, `troncato` e `come_richiedere_il_resto`.
- `now` è esplicito: solo `server.py` legge l'orologio (Europe/Rome).

### Superficie per gli agenti di materia (`school_agent.py`, chiusura del filone)
```
Classroom → Registry L1 (immutabile, fonte di verità) → L3 cls_derive → cls_subjects + school_manual.json
          → school_context._build (vista COMPLETA, diagnostica)
          → school_agent.brief(materia)        CONTESTO DI AVVIO dell'agente (compatto)
          → school_agent.tasks/task/announcements/materials   DETTAGLI su richiesta
          → agente materia (Cloud Academy) → Notion solo come dashboard (F4, futuro)
```
| Tool MCP (tutti sola lettura) | Ruolo | Per gli agenti |
|---|---|---|
| `school_brief(subject)` | contesto di avvio: corsi confermati, compiti operativi (attivi + 8 recenti + conteggi per stato) con stato consegna, ultimi 8 annunci (testo tagliato a 280 caratteri), ultimi 10 materiali, argomenti, ClasseViva della materia, da_verificare, regole, elenco degli strumenti di recupero | **sì** |
| `school_tasks(subject, stato?)` | tutti i compiti operativi dei corsi confermati, compatti, filtro per `stato_operativo` | sì |
| `school_task(subject, uid)` | dettaglio di un compito (descrizione, allegati, link, consegna, punti, provenienza con hash); rifiuta altre materie, corsi non confermati, storico | sì |
| `school_announcements(subject, cerca?, limite?)` | annunci operativi con testo completo e allegati, ricerca per parola (max 50) | sì |
| `school_materials(subject, argomento?, cerca?, limite?)` | materiali operativi con descrizione e allegati (max 100) | sì |
| `school_subjects()` | elenco delle 8 materie e delle escluse | sì |
| `school_context(subject)` | vista COMPLETA, output grande (70–97 mila caratteri): **diagnostica** | **no** |
| `cls_course_subjects()` | mappa corso → materia con evidenze, per le conferme dell'utente | no (utente) |

- **Isolamento:** ogni funzione lavora su UNA materia; i dati provengono solo dai corsi CONFERMATI per quella materia in `school_manual.json` e solo dal perimetro operativo. I corsi proposti compaiono nel brief solo come nome + conteggi (e solo se hanno contenuto operativo); esclusi, non attribuiti, biennio e storico mai. Il recupero rifiuta uid fuori da questi limiti. Verificato sui dati reali: 0 uid di corsi di altre materie nei brief delle 8 materie.
- **Citazioni:** nel brief `ref` = `uid@versione`; hash e provenienza completi con `school_task`.
- **Dimensioni reali** (registro di prova, conferme attuali, JSON come il tool MCP): brief 4.380–9.821 caratteri per materia (65.040 in tutto) contro 68–97 mila della vista completa. Nessun conteggio token ufficiale ancora fatto.
- **Estratti deterministici**: nessun riassunto generato; stesso registro + stesso `now` → stesso JSON.
- **Nessun tool di scrittura**: nessuno strumento modifica registro, `school_manual.json`, Notion o le fonti.

### Conferme corso → materia in uso (`school_manual.json`)
File privato `~/Library/Application Support/Cloud/school/manual/school_manual.json` (cartella 0700, file 0600, fuori dal repository). Regola applicata: si conferma un corso solo se la materia è certa (evidenze E1/E2/E3) **e** il corso è chiaramente della 4ª 2026/27 (4Z, 4X, 2026/27) con attività nell'anno in corso.
| Corso | Materie confermate |
|---|---|
| 4Z Mate & Fisica (prova) 2026/27 | matematica, fisica (condiviso) |
| Lettere - Bianchi | italiano, latino (condiviso) |
| Storia e Filosofia (prova) - 4Z | storia, filosofia (condiviso) |
| 4X - Inglese | inglese |
| SCIENZE 4 (prova) | scienze |
Non confermati (restano proposte automatiche): "ITALIANO, LATINO" e "2 Z Mate & Fisica (prova) 24/25" (biennio, nessun contenuto operativo: nascosti dai brief); "3^ Z (prova) - Scienze naturali" (corso di 3ª con 16 compiti nel perimetro operativo): **decisione dell'utente del 2026-10-03: NON confermato**, resta proposta; l'agente di scienze parte dal solo corso di 4ª "SCIENZE 4 (prova)".

### Retention dello storico (decisione R1)
Nessuna cancellazione fisica: il registro L1 resta immutabile (trigger SQLite invariati) e conserva anche i dati Classroom degli anni passati (~0,5 MB) come dato tecnico (la sync li rilegge comunque: i corsi vengono riusati). Lo storico **non è agent-facing**: il tool `school_context` non ha parametri per richiederlo.

### Corso Classroom → materia (`cls_subjects.py`)
| Livello | Dove vive | Persistente |
|---|---|---|
| Corso (`name`, `subject`, `ownerId`) | registro L1 | sì (immutabile) |
| Docenti per materia | ClasseViva `subjects()` | no (letto) |
| **Proposta** (categoria, materie, evidenze) | `cls_subjects.classify` | **no**: ricalcolata a ogni chiamata |
| **Conferma** | `~/Library/Application Support/Cloud/school/manual/school_manual.json` → `subjects.<materia>.classroom_course_ids` | sì, scritta **solo dall'utente** (o da Claude su sua richiesta esplicita) |
| Contesto della materia | `school_context(materia)`: solo corsi confermati per quella materia | no |

- Evidenze (parole intere su testo normalizzato, nessun confronto approssimativo): **E1** campo `subject` del corso Classroom; **E2** nome del corso (`mate` → matematica; `lettere` da sola = evidenza **debole** italiano + latino); **E3** cognome di un docente ClasseViva come parola nel nome del corso → materie del docente (mai civica/studio). Locuzioni prima delle parole: `storia dell'arte` → arte (mai storia), `scienze motorie` / `educazione fisica` → motorie (mai scienze/fisica).
- Categorie: `confirmed` (solo dal file manuale) · `proposed` · `excluded` (solo materie escluse) · `non_attributed` (nessuna evidenza) · `ambiguous` (E1 ed E2 discordi, o evidenze miste tra materie con agente ed escluse).
- Un corso condiviso resta **un solo `course_id`**: compare sotto più materie nel file manuale e in più contesti con `condiviso_con[]`; la materia del singolo compito non viene scelta in automatico.
- Tool in sola lettura `cls_course_subjects`: mostra la mappa per la revisione dell'utente. Nessun tool MCP scrive il file manuale.
- **Unica fonte delle proposte per il contesto agente: `cls_subjects`.** Le conferme arrivano a L3 tramite il parametro `manual`.
- **`derivato.materia_proposte` di L3 è LEGACY / DEPRECATO** (prodotto da `cls_derive.subject_proposals`, vecchia regola: sola citazione nel nome del corso, es. per "MATE & FISICA" propone solo fisica). Resta nell'output di F1 solo perché F1 è chiusa e invariata (`cls-d2-9840a4b2`). **Non è consumato** da `school_context`, dagli agenti, da `server.py`, da Notion (`notion_projection`) né dagli script di sync; **non va usato** come fonte per nuove funzionalità. Test di guardia in `tests/test_school_context.py` (`SingleProposalSource`) garantiscono che il contesto non lo esponga e non ne dipenda. La vecchia funzione v1 `school_context.propose_links` è stata rimossa.
- **C2 (futuro, non ora):** la rimozione di `subject_proposals` / `derivato.materia_proposte` / `subject_rx` da F1 è rinviata alla prossima eventuale revisione di F1 per altri motivi (vedi `docs/TODO-cloud.md`). F1 **non** è riaperta.

### Perimetro operativo (dichiarato dall'utente)
Classroom contiene circa 4 anni di corsi. Il **patrimonio completo** resta intatto: nessun record viene cancellato, riscritto o filtrato in modo distruttivo. Le **viste operative**, i filtri e il contesto degli agenti considerano per default solo:
| Anno scolastico | Classe |
|---|---|
| 2025-26 | 3ª classe |
| 2026-27 | 4ª classe (anno attuale) |
Gli anni precedenti restano consultabili come **storico**. La corrispondenza anno → classe è una dichiarazione dell'utente (`cls_derive.CLASSI`), da aggiornare a ogni nuovo anno; il risultato segnala se l'anno corrente esce dal perimetro (`perimetro_operativo.anno_corrente_nel_perimetro`).

### L3 Classroom: `cls_derive.work_view` (F1)
Una vista per compito Classroom: compito + consegna (unione **solo** su `(courseId, courseWorkId)`) + corso + anno del compito + anno del corso + perimetro + materia. Modulo puro: nessuna rete, nessun orologio (`now` è un input esplicito con fuso), input mai modificato, output ordinato e riproducibile. `from_registry` legge con `Registry.from_snapshot` (sola lettura).
Ogni `work_view` separa quattro gruppi:
| Gruppo | Contenuto | Regola |
|---|---|---|
| `fonte` | dati del compito, della consegna (`late`, punti) e del corso come li dà Classroom | nessun ricalcolo; i punti sono "NON un voto" |
| `derivato` | `scadenza` (UTC→Europe/Rome, `solo_data` = fine giornata), `stato_operativo`, `corso_attivo`, `anno_compito`, `anno_corso`, `perimetro`, `materia_proposte` (**legacy/deprecato**, non consumato: vedi "Corso Classroom → materia") | regole dichiarate, versione in `derive_version` |
| `manuale` | materia confermata/override, `anno_compito` e `anno_corso` decisi | vuoto finché non esiste L-MAN; unica fonte di `decisione` |
| `da_verificare` | anomalie (nessuna/multiple consegne, corso mancante, stati sconosciuti, conflitti d'anno) | mai risolte in silenzio |

- `stato_operativo` ∈ `aperto`, `scaduto_non_consegnato`, `senza_scadenza`, `consegnato`, `restituito`, `bozza`, `rimosso`, `da_verificare`. È una **classificazione tecnica**: `senza_scadenza` non significa "da fare"; `scaduto_non_consegnato` non significa "da recuperare" (Classroom non dice se la consegna era richiesta). Lo stato del corso (attivo/archiviato) è un campo separato e non cambia lo stato operativo.
- Consegne multiple → `da_verificare` con i candidati, nessuna scelta; consegne senza compito → elenco `consegne_orfane`.
- **Due anni scolastici distinti** (1/9–31/8, date locali), entrambi deduzioni con certezza e candidati:
  - `anno_compito`: solo dalle evidenze del compito (data di creazione e scadenza). Accordo → `alta`; una sola evidenza → `media`; disaccordo (es. compiti per le vacanze creati a giugno e in scadenza a settembre) → `conflitto` senza valore, in `da_verificare`; nessuna data → `sconosciuta`. **L'anno del corso non è mai usato per il compito**: i corsi vengono riusati per più anni (dato reale: un corso creato nel 2022 è ancora attivo).
  - `anno_corso`: solo contesto. Nome del corso con coppia di anni consecutivi → `alta` (la data di creazione resta un candidato, non un conflitto); solo creazione → `bassa`; più anni nel nome → `conflitto`.
- **`perimetro`** ∈ `operativo`, `storico`, `da_verificare`, calcolato da `anno_compito` (o da `manuale.anno_compito`, che prevale). Con anno in conflitto: `operativo` se tutti i candidati sono nel perimetro, `storico` se nessuno, altrimenti `da_verificare`. È un'etichetta: `operational_views()` produce una vista filtrata **non distruttiva** (il risultato completo e il registro restano intatti).
- **Materia**: le proposte (solo citazione esplicita nel nome del corso) non diventano mai decisione; `manuale.materia.decisione` è valorizzata solo da conferma singola o override del livello manuale.
- `derive_version` = `cls-<rev>-<hash di regole e costanti + interp_version L2>`: risultati prodotti da regole diverse sono distinguibili.

## Moduli (`mcp/classeviva/`)
| Modulo | Ruolo | Scrive? |
|---|---|---|
| `cvv_client.py`, `cvv_registry.py` | ClasseViva sola lettura + adattatore registro | no |
| `registry.py` | registro generico multi-fonte (append-only, eventi, guardie anomalie) | solo il DB locale, via sync |
| `cls_client.py` | client Classroom, solo GET, retry/backoff, classificazione errori | no |
| `cls_auth.py` | OAuth installed-app + PKCE, refresh token nel Keychain, solo scope di lettura | Keychain (con consenso) |
| `cls_registry.py` | adattatore Classroom (6 tipi) | no (pure) |
| `cvv_dryrun.py`, `cvv_sync.py` | dry-run e sync, indipendenti dalla fonte (parametro `adapter`) | sync solo con `commit=true` |
| `cls_derive.py` | L3 Classroom: `work_view` (compito + consegna + corso + anno + materia), puro | **no** (sola lettura) |
| `notion_projection.py` | pianificatore PURO delle righe Notion | **no** (simula) |
| `cls_subjects.py` | corso Classroom → materia: proposte E1/E2/E3 (non persistenti), validazione di `school_manual.json`, puro | no |
| `school_context.py` | vista completa per materia (diagnostica): selezione da L3 solo operativa, solo corsi confermati (tool `school_context`, `school_subjects`, `cls_course_subjects`) | no |
| `school_agent.py` | superficie per gli agenti: `brief` + recupero su richiesta (tool `school_brief`, `school_tasks`, `school_task`, `school_announcements`, `school_materials`) | no |
| `server.py` v0.6.0 | strumenti MCP `cv_*`, `cls_*`, `school_context` | come sopra |

I nuovi strumenti (`cls_status`, `cls_registry_dryrun`, `cls_registry_sync`, `school_context`) compaiono **dopo il riavvio di Claude Desktop** (Cmd+Q e riapertura).

## Decisioni architetturali
1. **Un registro generico, due adattatori.** Classroom riusa il motore senza forzare la forma di ClasseViva; identità `(source, kind, source_uid)`, uid Classroom con prefisso `cls:`.
2. **Nessuna fusione automatica** ClasseViva↔Classroom: il sistema *propone* collegamenti (stato `proposto`), l'utente conferma nel livello manuale.
3. **Solo scope di lettura**, il minimo per tipo: `courses.readonly`, `coursework.me.readonly`, `student-submissions.me.readonly`, `courseworkmaterials.readonly`, `announcements.readonly`, `topics.readonly`. Niente Drive, niente scritture, niente dati dei compagni. `cls_auth` rifiuta e revoca se Google restituisce uno scope diverso da quelli richiesti. Unica tolleranza (`SCOPE_ALIASES`): `coursework.me.readonly` e `student-submissions.me.readonly` sono lo stesso permesso con due nomi; Google ne restituisce uno solo (osservato al consenso del 2026-10-03: chiesti 6, restituiti 5, mancava `coursework.me.readonly`). Il confronto accetta l'alias, ma resta rigido su mancanti reali e su scope non richiesti, alias compreso. Subito dopo il consenso, `cls_auth` fa una chiamata reale in sola lettura a `courseWork.list` su un corso. Se fallisce (403, rete, nessun corso), revoca il token e non salva nulla (`outcome: coursework_check_failed`). `status --online` mostrerà 5 scope, non 6: è normale.
4. **Credenziali solo nel Keychain** (`cloud.classroom`/`refresh_token`; client in `cloud.classroom-probe`); l'access token vive solo in memoria; la scrittura nel Keychain usa stdin (mai argv); nulla in repo, log o iCloud.
5. **Orari Classroom**: `dueTime` è UTC (documentato) → convertito in Europe/Rome nel normalizzato; il RAW resta intatto. *Da verificare con dati reali.*
6. **`assignedGrade` ≠ voto**: i punti di Classroom sono segnalati come "NON un voto"; i voti ufficiali sono solo quelli ClasseViva.
7. **Materiali Classroom**: si registrano i metadati (titolo, link, tipo); il contenuto dei file Drive non si legge (servirebbe Drive API).
8. **Notion**: prima il pianificatore puro (create / update / adopt / flag_missing / restore). Aggiorna solo campi gestiti; non tocca righe `Manuale`; nessuna cancellazione; mai "Fatto" → "Non più nel registro"; `restore` (2026-10-19) riporta allo Stato iniziale solo una riga marcata dal sistema il cui elemento è ricomparso.
9. **Un agente per materia = un template + un contesto** (vedi `docs/agente-materia-template.md`).
10. **Scheduler**: non attivo. Pronti (2026-10-19) il job `school_job.py` e un modello launchd inattivo con script di installazione che richiede `--conferma`: vedi `docs/automazione-scuola.md`. Attivazione solo con consenso esplicito.

## Rischi noti / decisioni dell'utente
- **App OAuth in "Testing"**: i refresh token scadono dopo 7 giorni. Per un uso stabile serve "In production" (app non verificata, <100 utenti, schermata di avviso). Decisione dell'utente. 2026-10-19: dichiarata "In production" dall'utente; non verificabile dal sistema, persistenza da provare (vedi `TODO-cloud.md`, punto 3).
- **Dominio scolastico**: potrebbe bloccare app di terze parti (errore da registrare, nessuno scraping).
- ~~**Drive `thumbnailUrl`** può ruotare e creare versioni spurie~~ **RISOLTO 2026-10-03**: confermato dal vivo, Google lo rigenera a ogni GET; prima della correzione la doppia lettura dava 495 falsi "update". Ora è escluso dall'hash a qualsiasi profondità (`"**.thumbnailUrl"` in `cls_registry.VOLATILE`, `registry.strip_volatile`), mentre il RAW lo conserva. Con la correzione, la doppia lettura reale dà 0 cambiamenti su 987 record.
- **Timeout MCP**: il dry-run Classroom completo (154 GET, circa 135 s) supera il limite di tempo dei tool di Claude Desktop. Da Terminale c'è ora `school_job.py run` (dry-run reale completo di entrambe le fonti: 76 s, 2026-10-19).
- **`security -i`** (scrittura Keychain) testato solo con dati sintetici; verificare al primo `authorize`.
- **Hash legacy Notion** (sha1-12 con `readStatus`) diverso dall'hash del registro (sha256 senza `readStatus`): la proiezione degli avvisi richiede migrazione esplicita; l'operazione `adopt` copre Verifiche/Compiti.
- Riga `[TEST]` in Verifiche da eliminare a mano.

## Rollback
Tutto il lavoro recente è codice + test + documenti in commit separati: `git revert <commit>`. Nessun dato esterno modificato (registro reale, Notion, ClasseViva, Classroom, Keychain non toccati dopo la prima sync).
