# DATA_CONTRACT — BOZZA (Fase 0, non operativa)

Stato: bozza non approvata. Non descrive comportamento nuovo: fissa le regole già vigenti più il kind `lesson` (Fase 1).

## 1. Livelli e direzione dei dati
RAW (record esatto della fonte) → registro (identità, hash, versioni, stato) → L3 (`work_view`) → viste (`school_context`, Notion).
Il flusso è a senso unico. Notion e Project Scuola non scrivono mai nel registro.

## 2. Il registro è canonico
- Append-only: ogni cambiamento di contenuto crea una nuova versione; nessuna cancellazione.
- Idempotente: stessa lettura → nessun create/update (`unchanged`).
- Un record sparito dalla fonte diventa `missing_in_source`, mai cancellato; una lettura vuota o in forte calo è rifiutata (`missing_refused`).
- Scrittura solo con `commit=true` e `expect_create`, dopo un dry-run; lock, backup prima di righe modificate, verifica post-scrittura.

## 3. Identità (UID)
| fonte | kind | uid | note |
|---|---|---|---|
| ClasseViva | agenda_event / grade / absence | `cvv:<evtId>` | invariato |
| ClasseViva | notice | `cvv:<pubId>` | invariato |
| ClasseViva | lesson | `cvv:lesson:<evtId>` | Fase 1; fallback `sec:lesson:<sha1(data\|ora\|docente\|materia)>`, argomento fuori dalla chiave |
| Classroom | * | `cls:<corso>/<id>` | invariato |
La chiave del registro è (source, kind, source_uid): gli id dei kind diversi non collidono.

## 4. Autorità per dato
Lezioni svolte, data/ora verifiche, voti, assenze, comunicazioni → ClasseViva. Contenuto e preparazione delle verifiche, compiti, consegne → Classroom. "Svolto" solo dalle lezioni. Notion: nessuna autorità.

## 5. Campi volatili
Esclusi dall'hash e registrati come stato (oggi: `notice.readStatus`). Per `lesson` nessun campo volatile: l'argomento è contenuto.

## 6. Da decidere (richiede approvazione)
- Finestra di lettura delle lezioni e frequenza di sync (Fase 2).
- Come ClasseViva e Classroom si collegano per materia (Fase 4).

## 7. Contesto per gli agenti (M4) — `school_context/3`, `school_overview`
Vale solo per M4. Non cambia il registro né le regole sopra.

**Fonte canonica.** Il registro (§2). `school_context`, `school_brief` e `school_overview` sono viste derivate in sola lettura: non sono un secondo registro, non scrivono, non duplicano i record (li citano con `uid` + `registry_version` + `content_hash`).

**Tre livelli, sempre con gli stessi nomi.**
| chiave | livello | cosa contiene |
|---|---|---|
| `dati` | canonico | i campi esatti della fonte (raw), citati in `fonte` |
| `interpretazione` | derivato | attribuzione di materia M2 (`stato`, `criterio`, `confidenza`, `candidati`, `evidenze`, `conflitti`, `avvisi`, `attr_version`), `tipo_dedotto` degli eventi; è una lettura del dato, non il dato |
| `freschezza` | operativo | quanto è recente la lettura da cui dipende la sezione (M3) |
Classroom (compito + consegna, `stato_operativo`) è una vista L3 del RAW: è derivata e la sua `fonte` cita i RAW.

**Attribuzioni incerte.** Un record entra in una materia solo se M2 la determina (`stato=determinata`) e coincide. Se M2 non decide (`da_verificare`) il record resta in `possibili_da_verificare` con i `candidati` e **non** appartiene alla materia: nessuna somiglianza testuale lo assegna. Un conflitto (es. `subjectDesc_vs_testo`, `evento_vs_lezioni`) è registrato in `interpretazione.conflitti` e segnalato nei `warning`; la materia non viene modificata né il conflitto risolto. L'evidenza A6 (lezioni → eventi) è solo corroborativa. Le conferme dei corsi Classroom restano solo in `school_manual.json`; i corsi `proposto` non sono fatti della materia.

**Freschezza.** Per sezione, dai run di commit M3 dei kind da cui dipende: `aggiornato` | `stale` (oltre soglia o ultimo run in errore) | `senza_lettura` (mai sincronizzato o nessuna lettura riuscita), più `legacy` (run precedenti a M3, senza fine/durata) e `con_rifiuti`. Soglia provvisoria 48 h, ricalcolata a ogni chiamata (nessuna cache). Un dry-run non aggiorna la freschezza. **`aggiornato` non garantisce che il dato sia corretto o completo**: dice solo che l'ultima lettura riuscita è entro la soglia.

**Warning e informazioni mancanti.** `warning` = elenco strutturato (`codice`, `gravita` info|attenzione, `sezione`, `uid`). `informazioni_mancanti` = ciò che non c'è (nessuna lezione, nessun corso confermato, `school_manual.json` assente, materiali/appunti non indicizzati…). Un conteggio a 0 è uno stato vuoto valido solo se la lettura è `aggiornato`.

**Cosa Claude può considerare affidabile.** I `dati` citati con la loro `fonte`; la materia di un record con `stato=determinata` (con le `evidenze`); la presenza di un `warning`/conflitto; la `freschezza` come indicazione di recenza. **Cosa non deve inventare.** Voti, date, docenti, materie, argomenti o consegne assenti; la materia di un record `da_verificare`; la risoluzione di un conflitto; che un dato sia "aggiornato" quando la sezione è `stale` o `senza_lettura`; i punti Classroom come voti; ciò che sta fuori dal perimetro operativo 3ª/4ª.

**Versioni e compatibilità.** `cloud.school.context/2` (tool `school_context`) resta invariato. `cloud.school.context/3` (tool `school_context_v3`) è una nuova versione esplicita e riusa v2 per Classroom e voti. `cloud.school.brief/2` = brief/1 più le chiavi additive `lezioni` e `freschezza` (nessuna chiave rimossa). `cloud.school.overview/1` (tool `school_overview`) è un indice sintetico che non sostituisce né il registro né i contesti per materia.


## 8. Collegamenti ClasseViva ↔ Classroom (M5) — `cloud.school.links/1`
Vale solo per M5. Non cambia il registro (nessuna tabella, nessuna migrazione), né M1–M4.

**Natura.** Un collegamento è una **interpretazione derivata**, ricalcolata a ogni lettura in sola lettura (`cvv_cls_links.py`, tool `school_links` e `school_link_explain`). Il Registry resta l'unica fonte canonica: i record ClasseViva e Classroom non vengono mai modificati né copiati nel Registry. Ogni collegamento ha una sua `link_version` (`r<rev>-<impronta di finestre, liste e soglie>`) e cita **entrambi** i lati con `uid` + `registry_version` + `content_hash` (`a.fonte`, `b.fonte`). `link_id` = hash deterministico di `link_version`, relazione, uid e hash dei due lati: se un record cambia, cambia l'id.

**Livelli.** `dati` (canonico, copiato e citato; i testi lunghi sono troncati con `testo_troncato`), `interpretazione` (relazione, stato, regola, prove, materia dei due lati), `freschezza` (operativo, M3: peggiore tra la sezione ClasseViva e quella Classroom; `aggiornato` non garantisce correttezza). Orientamento fisso: lato `a` = ClasseViva, lato `b` = Classroom; un solo collegamento per (a, b, relazione).

**Stati.** `confermato` (**riservato**: il motore automatico non lo produce mai; in M5 v1 non esistono conferme manuali), `probabile` (evidenza forte, candidato unico), `proposto` (evidenza media, candidato unico, da verificare), `conflitto` (candidati o segnali in contrasto: si riportano i valori di entrambe le fonti, **nessun candidato è scelto e nessuna fonte è modificata**), `non_determinabile` (evidenza presente ma materia di un lato non determinata: nessun collegamento). Nel dubbio: nessun collegamento. I falsi positivi pesano più dei falsi negativi.

**Relazioni.** `prepara` (un elemento Classroom prepara una verifica: **non** è identità), `stesso_argomento` (evento o lezione ↔ elemento Classroom), `stessa_scadenza` (compito ClasseViva ↔ compito Classroom con scadenza strutturata). Nessuna identità automatica.

**Segnali (prove).** Ogni prova ha `codice`, `esito` (a_favore|contro|neutro), `peso` (forte|media|debole|necessaria|corroborativa), `dettaglio`. K1 data esplicita nel testo Classroom (o scadenza strutturata) uguale alla data ClasseViva — "forte" solo se la data è *attribuita* a una verifica ("verifica del 5 ottobre"); le date senza anno si risolvono con l'anno scolastico (deduzione dichiarata); date impossibili o col giorno della settimana incoerente non si usano; **date relative non interpretate**. K2 materia determinata e uguale (necessaria). K3 parole specifiche in comune (stem; media se ≥2 oppure 1 di ≥6 lettere; esclusi stopword, termini generici e nomi di materia). K4 finestra temporale. K5 il testo cita una verifica e l'evento è una verifica. K6 docente (solo corroborativa, non conta per le soglie). K7 unicità del candidato di livello massimo.

**Materia di un elemento Classroom.** Solo corsi **confermati** in `school_manual.json`. `determinata` se il corso è monomateria, oppure se il topic nomina **una sola** materia del corso condiviso; altrimenti `da_verificare` (topic come "Laboratorio", elementi senza topic, **tutti gli annunci** dei corsi condivisi, che non hanno topic) e il collegamento non può superare `non_determinabile`. Nessuna mappa manuale topic→materia in v1. Dalla parte ClasseViva la materia è quella di M2 solo se `determinata`.

**Regole.** R1 probabile: data attribuita alla verifica == data evento + K2 + K4 + K7. R2 proposto: data uguale non attribuita + (K5 o K3 media). R3 proposto: K3 media. R4 proposto: solo K5 (unica verifica candidata; esclusa se il testo attribuisce la verifica a un'altra data). R5 proposto (lezione ↔ elemento, ±3 giorni, migliore corrispondenza unica). R6 probabile / R7 proposto: scadenza strutturata uguale + K3 media / debole. RC1 candidati multipli, RC2 segnali discordanti (la data punta a un evento, la parola "verifica" a un altro), RC3 data discordante (stesso argomento, data diversa tra le fonti). RN1 materia non determinata. Veti: anno scolastico diverso, elemento pubblicato dopo l'evento, fuori finestra (21 giorni per `prepara`/`stesso_argomento` evento, ±3 per lezione, 14/7 giorni per i conflitti di data), materia incompatibile. Finestre e soglie sono **provvisorie** e fanno parte della `link_version`.

**Cosa non fa M5 v1.** Conferme manuali; relazioni interne a ClasseViva (evento↔lezione); date relative; mappa manuale topic→materia; bacheca ClasseViva (M6); consegne e voti; scritture su Registry, Notion, ClasseViva, Classroom, `school_manual.json`.

**Cosa Claude non deve inventare.** Un collegamento "confermato"; che un elemento Classroom *sia* la verifica (solo "la prepara"); la materia mancante di un elemento `non_determinabile`; la risoluzione di un conflitto; collegamenti tra anni scolastici diversi.


## 9. Classificazione delle comunicazioni (M6) — `cloud.school.communications/1`
Vale solo per M6. Non cambia il registro (nessuna tabella, nessuna migrazione), né M1–M5 (`school_context_v3`, `school_overview`, `school_links` invariati).

**Natura.** Una classificazione è una **interpretazione derivata**, ricalcolata a ogni lettura in sola lettura (`cvv_comms.py`, tool `school_communications` e `school_communication_explain`). Il Registry resta l'unica fonte canonica: nessun record viene modificato o copiato. Ha una sua `comm_version` (`r<rev>-<impronta di lessico, regole, soglie e `link_version` di M5>`); `comm_id` = hash di `comm_version`, uid e `content_hash`. Ogni voce cita `uid` + `registry_version` + `content_hash` (`fonte_record`). Le primitive condivise con M5 (formato delle prove, hash corto, troncamento, ordine di freschezza, parole di verifica, ruolo delle date) stanno in `school_common.py`; gli helper di contesto restano in `school_context3.py`.

**Perimetro (v1).** Circolari ClasseViva (kind `notice`, solo titolo/categoria/allegati/validità) e annunci Classroom PUBBLICATI (solo testo e materiali; nessun titolo, topic o autore). Default **operativo** = anni di `cls_derive.CLASSI` (3ª/4ª); `perimetro=storico|tutti` o `dal`/`al` per cambiarlo. È un'etichetta: nulla esce dal Registry.

**Classi (multi-etichetta) e attributi.** Classi: `verifica`, `compito`, `attivita`, `organizzativa`, `avviso`, `materiale`. `classe_principale` = unica classe al livello massimo nell'ordine *specifiche (verifica, compito, attività, organizzativa) > avviso > materiale*; se più classi sono pari: **nessuna scelta** (`pari_livello`, regola RN2). Attributi (non classi): `ambito` (`generale` per le circolari, `materia` per annunci di corsi monomateria confermati, `corso` altrimenti), `materia`, `date` con ruolo (`verifica` | `scadenza` | `non_attribuita` | `non_utilizzabile`), `riferimenti_relativi_non_risolti`, validità nativa della circolare, numero di allegati/materiali.

**Stati.** `determinato` **solo da struttura nativa** (ambito della circolare; materiali nativi degli annunci): il testo libero non lo produce mai. `probabile` (≥2 parole del lessico, data esplicita attribuita, URL, corroborazione M5 su evidenza testuale già presente), `proposto` (una sola parola, o tetto per variazione/negazione), `conflitto` (RC1 il testo nomina una materia diversa da quella del corso; RC2 ≥2 date diverse attribuite alla verifica: **nessuna scelta**, nessuna fonte modificata), `non_determinabile` (RN1: nessun indizio, es. un nome solo come "Ariosto"; una circolare senza indizi nel titolo). Nel dubbio: `proposto` o `non_determinabile`; i falsi positivi pesano più dei falsi negativi.

**Regole.** A1–A3 ambito; M1 materiale determinato (materiali nativi di un annuncio; **l'allegato di una circolare è la circolare stessa e non conta**), M2 URL, M3 parole; V1 verifica probabile (parola + data attribuita), V2 proposta, V3 tetto per variazione/negazione (annullata, rinviata, nessuna verifica…), V4 = V2 + collegamento M5 `prepara` (probabile|proposto); C1 compito probabile (parola + data esplicita o riferimento relativo non risolto), C2 proposto; G1/G2 attività, organizzativa, avviso; RC1, RC2, RN1, RN2. Il lessico è in `LEX` e fa parte della versione.

**Materia.** Per gli annunci come in M5 (`item_subject`): `determinata` solo per corsi monomateria confermati; negli altri casi `da_verificare` (anche se il testo nomina una materia: è solo evidenza). Le circolari non hanno materia. Con il filtro `materia` le circolari generali e i casi `da_verificare` non sono inclusi (conteggiati in `esclusi_dai_filtri`; `includi_generali=true` per le generali).

**Relazione con M5.** I collegamenti M5 sono **solo evidenza corroborativa** (prova S8): non creano mai una classificazione, non producono `determinato`, e V4 agisce solo se il testo ha già una parola di verifica. I conflitti M5 sono riportati in `collegamenti_m5` ma non rendono conflittuale la classificazione M6. La freschezza di una classificazione che usa M5 include agenda e lezioni.

**Date.** Solo date esplicite già supportate (`it_dates`); gli intervalli "dal… al…" e le date relative non sono interpretati (v1). Anno dedotto dall'anno scolastico = dichiarato.

**Output.** Compatto per default (regola e stato per funzione); `dettaglio=true` e `school_communication_explain` aggiungono prove (`codice`, `segnale`, `esito`, `peso`, `dettaglio`) e motivazioni. Limite predefinito 20 comunicazioni (testi compatti a 160 caratteri), più recenti prima, troncamento dichiarato nei `warning`. `freschezza` per sezione `circolari`, `annunci`, `agenda`, `lezioni`.

**Cosa non fa M6 v1.** Conferme o correzioni manuali; destinatari/classi delle circolari (non presenti nel record); lettura del contenuto degli allegati; intervalli di date e date relative; integrazione in `school_context_v3`/`school_overview` (M7+); Notion, automazioni, scheduler; scritture su Registry, ClasseViva, Classroom, `school_manual.json`.

**Cosa Claude non deve inventare.** Una materia o un docente per una circolare; una data da un riferimento relativo; che un annuncio senza indizi abbia una classe; la risoluzione di un conflitto; che una circolare con validità scaduta sia ancora operativa.

**Nota M6 r2 (M7).** `COMM_RULES_REV` passa da 1 a 2 (`comm_version` `r2-…`, quindi cambiano anche tutti i `comm_id`). Unica novità: il caso **ASSENZA_DOCENTE** (regola AD1, prova S10). Scatta solo con un pattern esplicito di assenza di un docente in terza persona: titolo (`prof`, `prof.ssa`, `professoressa`, `professore`, `docente`) + "non c'è" / "è (sarà) assente" / "assenza del…". Risultato: funzione principale `organizzativa` (`probabile`), `classe_principale.secondarie` = `["compito"]` solo se il testo ha parole di compito, `interpretazione.caso_speciale` con codice, regola e motivazione. Queste due chiavi compaiono **solo** sulle comunicazioni intercettate. Non bastano: `sostituzione`/`sostituire`, `assenti` (studenti), `supplenza`, `professore` da solo, "Portate i compiti" senza un'assenza. I conflitti restano conflitti. Golden r1 delle fixture in `tests/comms_golden_r1.json`. Sui dati reali è intercettata 1 comunicazione su 490 (l'annuncio di scienze sull'assenza della prof.ssa Verdi); le altre sono identiche a r1 a parte versione e `comm_id`.


## 10. Contesto operativo per materia (M7) — `cloud.school.context/4`
Vale solo per M7. Non cambia il registro (nessuna tabella, migrazione o sync) né M2–M5 (`school_context_v3`, `school_overview` e `school_links` restano byte-identici). M6 cambia solo per la nota r2 qui sopra.

**Natura.** Uno strato di **aggregazione** in sola lettura (`school_context4.py`, tool `school_context_v4`) sopra M2/M4/M5/M6 per **una** delle 8 materie (`AGENT_SUBJECTS`, risolte con `school_context.resolve_subject`). Non è una nuova fonte né un nuovo matcher, e non attribuisce materie: usa gli stati che gli strati esistenti producono davvero. `context_version` = `r<rev>-<impronta>` (include sezioni, limiti, finestre e le versioni M2/M5/M6/L3). Il tool legge l'orologio **una sola volta** e passa lo stesso `now` a tutto il builder. Con lo stesso Registry, materia, now, manuale, dettaglio, limiti e soglia il JSON (`json.dumps(indent=2, ensure_ascii=False)`) è identico byte per byte. Il builder richiede anche `subjects` (keyword): M2/M4/M5/M6 ne dipendono e il Registry non li contiene.

**Struttura.** `schema`, `natura`, `materia` (docenti solo dall'elenco canonico ClasseViva `/subjects`), `riferimento_temporale` (now, data, finestre calcolate), `livelli` (modo, sezioni in dettaglio, limiti effettivi), `sintesi` (`prossimi_impegni`, `contatori`, `mostrati`, `verifiche_unite`), `sezioni`, `freshness`, `gap`, `ambiguita`, `warning`, `provenance`, `regole_agente`. Ogni elemento ha `uid` e `hash` (i primi 12 caratteri del `content_hash` del Registry; per collegamenti e conflitti, sha256 dei `content_hash` dei lati).

**Sezioni.**
- `lezioni`: lezioni con materia **determinata** da M2, ordinate per data DESC e poi uid; nessun raggruppamento per argomento.
- `verifiche`: eventi di agenda `verifica` (M2) più comunicazioni M6 della materia (`materia.stato=determinata`) che hanno la funzione verifica e **una** data esplicita con ruolo verifica. Copre le future e i 7 giorni passati; prima le future in ordine di data, poi le passate dalla più recente. Lo stato M6 non viene elevato. Due verifiche si **uniscono** solo se hanno lo stesso uid oppure un collegamento M5 `stessa_scadenza` nello stato più alto che M5 produce davvero (`probabile`; `confermato` è riservato). Ogni altro collegamento tra due verifiche le lascia distinte e produce l'ambiguità `possibile_stessa_verifica`. Nessuna deduplicazione per testo o data.
- `compiti`: compiti di agenda ClasseViva (M2) dagli ultimi 14 giorni in poi, più i compiti Classroom **aperti** (definizione M4: `stato_operativo='aperto'`) o `scaduto_non_consegnato` da al massimo 14 giorni. Per Classroom la materia deve essere determinata (`cvv_cls_links.item_subject`: corso monomateria confermato, oppure topic che nomina una sola materia). Stato di consegna e scadenza restano quelli della fonte: nessuna scadenza viene inventata e i compiti `senza_scadenza` sono solo un gap.
- `comunicazioni`: annunci M6 del perimetro operativo con materia `determinata` uguale a quella richiesta, per data DESC.
- `collegamenti`: collegamenti, conflitti e `non_determinabili` M5 con almeno un lato attribuito alla materia; classificazione M5 invariata.
- `possibili_da_verificare`: lezioni ed eventi con M2 da verificare, elementi di corsi condivisi senza topic (es. l'annuncio "Ariosto"), corsi proposti non confermati e i loro annunci, conflitti di materia M6. **Mai promossi** nelle sezioni certe né nei prossimi impegni.
- `generali`: circolari (ambito generale M6) degli ultimi 30 giorni o ancora valide; escluse quelle con validità nativa `scaduta`/`fuori_intervallo_nativo`. Non sono attribuite alla materia e non sono impegni.

**Prossimi impegni e contatori.** `prossimi_impegni` contiene solo elementi già attribuiti alla materia con data futura utilizzabile (≥ oggi): verifiche di agenda, verifiche M6 `probabile`, compiti ClasseViva e compiti Classroom aperti. Massimo 10. `contatori` = candidati totali dopo regole e finestre, **prima** dei limiti; `mostrati` = elementi presenti nel documento.

**Limiti e livelli.** Default: prossimi 10, lezioni 8, verifiche 8, compiti 10, comunicazioni 8, collegamenti 8, possibili 6, generali 5. Ammessi interi da 1 a 50. Come in `school_agent._limit`, un valore non valido viene rifiutato e uno oltre 50 viene normalizzato a 50 con un warning `limite_normalizzato`. I limiti si applicano dopo la costruzione dei candidati. Il default è compatto (testi ≤ 160 caratteri, `testo_troncato`, nessuna evidenza). `dettaglio=true`, o una lista di sezioni, espande **solo** quelle sezioni: testo completo, evidenze, `content_hash` completo, `registry_version`, `fonte_record` e freschezza per elemento.

**Freschezza, gap, ambiguità.** La freschezza per sezione riusa `registry.freshness` + `school_context3.section_freshness` (soglia di default 48 ore) e non cambia mai il contenuto. Una sezione non fresca produce il gap `sezione_non_fresca` e il warning `sezione_stale`/`sezione_senza_lettura`.

Gap: `nessuna_lezione_recente` (14 giorni), `docente_non_disponibile`, `corso_condiviso_senza_topic`, `corso_non_confermato`, `verifica_senza_data_utilizzabile`, `verifica_solo_da_annuncio`, `compito_senza_scadenza`, `sezione_non_fresca`, `circolare_senza_testo`, `sezione_troncata`, `possibili_non_comunicazioni_non_coperti` (materiali dei corsi confermati, coperti da `school_materials`, ed elementi di corsi non confermati).

Ambiguità: `pari_livello` (M6 RN2, M5 candidati multipli), `riferimenti_relativi_non_risolti`, `discordanza_date` (M6 RC2, M5 RC3/RC2), `discordanza_materia` (conflitti M2 con materia mantenuta), `conflitto_materia` (M6 RC1), `possibile_stessa_verifica`.

Un dato semplicemente vuoto non produce gap.

**Dimensione.** Sui dati reali il default compatto resta sotto 40 KB per ognuna delle 8 materie. Il dettaglio è su richiesta e può superarla.

**Cosa non fa M7.** Nuove fonti, matcher o attribuzioni; modifiche a M5; conferme o override manuali; viste multi-materia; voti; assenze; Notion, scheduler, automazioni; scritture su Registry, ClasseViva, Classroom, `school_manual.json`.

**Cosa Claude non deve inventare.** Che un elemento in `possibili_da_verificare` sia della materia; che due verifiche siano la stessa senza unione M5 `stessa_scadenza`/`probabile`; una data da un riferimento relativo; una scadenza per un compito che non ne ha; un docente non presente in `/subjects`; che una circolare generale sia un impegno della materia.

**Nota M7.1.** `school_context4.build()` = `render(collect(...))`. `collect()` restituisce **tutti** i candidati di ogni sezione (dopo regole e finestre, prima di limiti e dettaglio), con un campo interno `_fatti` per gli strati successivi (anno scolastico, confidenza M2, `late`, topic…) che non entra mai nel documento. Il contratto pubblico `cloud.school.context/4` è rimasto byte-identico: 280/280 combinazioni verificate su dati reali e fixture.


## 11. Stato oggettivo per materia (M8 v1) — `cloud.school.status/1`
Vale solo per M8. Non cambia M2–M7: i contratti di M4, M5, M6 e M7 restano byte-identici. Nessuna tabella, migrazione o sync.

**Natura.** `school_status.py`, tool `school_status_v1(materia, dettaglio=false, soglia_ore=48)`. È un livello in sola lettura sopra i candidati completi di M7 (`collect`), per una delle 8 materie. Non è una nuova fonte, non è un nuovo matcher e non attribuisce materie. `status_version` = `r<rev>-<impronta>` (regole, segnali, affidabilità, `context_version`). Il tool legge l'orologio una sola volta. Stesso Registry, materia, now, manuale, dettaglio e soglia producono lo stesso JSON byte per byte.

**Tre livelli.** `fatti`: conteggi e date osservabili dell'anno operativo. `segnali`: regole deterministiche sui fatti. `valutazioni`: **sempre `non_valutabile`** in v1, quindi nessun giudizio sul rendimento, nessun voto stimato e nessun indice di puntualità, anche quando i voti esistono.

**Perimetro temporale (D3).** L'anno operativo è l'anno scolastico di `now` (2026-27). Gli altri anni compaiono solo in `storico` (conteggi separati) e non sono **mai** usati nei segnali.

**Segnali.** Ognuno porta `evidenze` (uid + hash M7; per un conteggio, anche zero, un `contatore` che dice da dove viene il numero), `natura` (`fatto` | `derivato_deterministico` | `dipende_da_interpretazione`), `affidabilita` e `motivi_affidabilita`.
- S1 `verifica_in_calendario`: `presente`, `assente` oppure `assente_con_indizi_non_datati`.
- S2 `compiti_aperti`.
- S3 `compiti_scaduti_non_consegnati_14g`.
- S4 `continuita_lezioni`: nessun rapporto "poche/tante", perché l'orario non è disponibile.
- S5 `argomenti_recenti` (14 giorni).
- S6 `preparazione_collegata`: collegamenti M5 delle verifiche future, con il loro stato.
- S7 `comunicazioni_operative`.
- S8 `attribuzione_classroom`: `attribuiti`, `solo_possibili`, `nessun_elemento` oppure `nessun_corso_confermato`.
- S9 `dati_valutativi`: `nessun_voto`, `voti_presenti` oppure `non_disponibile`.

`possibili_da_verificare` e `generali` non alimentano mai i segnali. Le assenze sono fuori perimetro.

**Affidabilità.** I livelli sono `alta`, `media`, `bassa`, `non_determinabile`. Il calcolo è **monotono**: parte da `alta` e può solo scendere, con un motivo per ogni discesa.
- A1: un'interpretazione M5/M6 `probabile` porta al massimo a media.
- A2: `proposto`, `conflitto` o `non_determinabile` portano al massimo a bassa.
- A3: una confidenza M2 diversa da `alta` porta al massimo a media.
- A4: una sezione `stale` porta al massimo a bassa; una sezione `senza_lettura` porta a non_determinabile.
- A5: contano solo le ambiguità M7 **pertinenti** al segnale (tabella `AMBIGUITY_RELEVANCE`). Su un'evidenza primaria portano al massimo a bassa; su un'evidenza solo conteggiata, o con elenco troncato, al massimo a media.
- A6: una fonte mai letta dà valore `null` e `non_determinabile`.
- D1: `legacy` è solo provenance e **non** declassa.

Freschezza e ambiguità possono cambiare l'affidabilità, ma mai il valore di un segnale.

**Mancante e vuoto.** `copertura` indica per ogni fonte se è `presente`, `vuoto_valido` (letta, 0 record), `non_letto`, `fuori_perimetro` o `non_disponibile`. `0` vale solo per una fonte letta; una fonte mai letta dà `null`.

**Output.** Il default è compatto. `dettaglio=true` aggiunge gli elementi M7 di ogni evidenza. Il target di 20 KB per il compatto è desiderabile, non un limite rigido: sui dati reali va da 16 a 19 KB, mentre con dettaglio va da 20 a 28 KB. Completano l'output `gap` (M8: `dati_valutativi_assenti`, `fonte_non_letta`), `gap_m7` (riferimento), `ambiguita` (M7, con i segnali che toccano), `provenance` (versioni, `kind_legacy`, regole di affidabilità) e `regole_agente`.

**Cosa Claude non deve inventare.** Un giudizio sullo studente; un voto, una media o un rendimento; che le lezioni siano "poche" o "tante"; una data da un riferimento relativo; che un elemento possibile o una circolare generale riguardino la materia; fatti dell'anno precedente presentati come situazione attuale.


## 12. Priorità operative prudenti (M9 v1) — `cloud.school.priority/1`
Vale solo per M9. Non cambia M2–M8: i loro contratti restano byte-identici. Nessuna tabella, migrazione o sync.

**Natura.** `school_priority.py`, tool `school_priority_v1(materia?, dettaglio=false, soglia_ore=48)`. Senza materia produce la panoramica delle 8. M9 consuma **solo** il contratto pubblico di M8 (`school_status.build(dettaglio=true)`): nessun ricalcolo dei segnali, nessuna nuova fonte, nessuna attribuzione. `priority_version` = `r<rev>-<impronta>` (politica, regole, azioni, livelli, `status_version`). Il tool legge l'orologio una sola volta, anche per 8 materie. Lo stesso input produce lo stesso JSON byte per byte.

**Priorità e affidabilità sono separate.** `priorita` (nessuna | informativa | attenzione | alta | **non_determinabile**) dice quanto la situazione meriterebbe attenzione *se il dato è corretto*. `affidabilita` è propagata da M8. `stato_decisione` vale `completa`, `parziale` (qualche regola non valutata) o `da_verificare` (una voce ≥ attenzione con affidabilità ≤ bassa).

**Politica versionata (decisioni D-M9-1/2/3 del 2026-10-06).**
- P1 verifica futura (S1): ≤ 1 giorno alta, 2–7 attenzione, > 7 informativa; una voce per verifica; azione `controllare_verifica`. Con 0 giorni, nota "ora non disponibile".
- P1b indizio di verifica senza data: al massimo informativa, azione `verificare_informazione`.
- P2 preparazione (S6): collegamento `probabile`/`proposto` → azione `aprire_preparazione` sulla voce P1, **mai** aumento di priorità; `conflitto` → `verificare_informazione`; `non_determinabile` → nessuna azione.
- P3a compito aperto (S2): ≤ 7 giorni attenzione, oltre informativa; **mai alta**.
- P3b scaduto non consegnato (S3): attenzione, con il motivo "secondo Classroom; non dice se la consegna era richiesta".
- P4 comunicazione degli ultimi 14 giorni (S7) con classe compito, verifica o organizzativa in stato determinato o probabile: al massimo informativa, azione `leggere_comunicazione`. Avvisi, materiali e proposte non generano voci.
- P5 (S4, S5, S8, S9): **solo `contesto`**, nessuna voce.
- P6: una voce ≥ attenzione con affidabilità bassa o non_determinabile riceve anche `verificare_informazione`; la priorità non cambia.

Più regole sullo stesso oggetto: resta la voce di priorità più alta, a parità l'ordine fisso P1 > P1b > P3a > P3b > P4. Le azioni e le evidenze delle altre restano nella voce. La priorità della materia è la massima tra le voci: nessuna somma, nessuna escalation per numero.

**Affidabilità.**
- R1: voce = minimo dei segnali M8 che la generano.
- R2: un segnale di input con valore null (fonte non letta) → regola in `regole_non_valutate` e tetto `non_determinabile`.
- R3: materia = minimo tra le voci di priorità massima e il tetto R2; senza voci, minimo tra gli input valutati.
- R4: P2 e P6 non toccano l'affidabilità della voce.
- R5: `legacy` nessun effetto.
- R6: monotonia per voce sulla coppia (oggetto, regola) e per materia tramite R2/R3. Una voce di regola diversa sullo stesso oggetto (per esempio P4 che resta quando P1b non è valutabile) è un'altra affermazione, non un rialzo.

Una fonte non letta non diventa mai `nessuna`: se non ci sono voci, la priorità della materia è `non_determinabile`. Una fonte letta e vuota dà `nessuna`.

**Azioni (vocabolario chiuso).** `controllare_verifica`, `aprire_preparazione`, `controllare_compito`, `leggere_comunicazione`, `verificare_informazione`. Nessun piano di studio, durata, diagnosi, voto, giudizio o peso numerico.

**Evidenze e tempo.** Ogni voce cita `{segnale, uid, hash}` **copiati** da M8. I giorni sono differenze tra le date M8 e la `data_riferimento`. Solo l'anno operativo (2026-27) genera voci: lo storico è esposto come contesto e non è mai usato.

**Ordinamento.** Per livello (alta, attenzione, informativa, non_determinabile, nessuna), poi per data della voce decisiva, poi per materia. Non c'è nessun confronto di "importanza" tra materie.

**Cosa Claude non deve inventare.** Urgenze oltre la politica; che "non_determinabile" significhi "niente da fare"; piani, tempi o giudizi; che un collegamento M5 sia certo; date da riferimenti relativi.


## 13. Azioni assistive (M10 v1) — `cloud.school.action/1`
Vale solo per M10. Non cambia M2–M9: i loro contratti restano byte-identici. Nessuna tabella, migrazione o sync.

**Natura.** `school_action.py`, tool `school_action_v1(materia?, dettaglio=false, soglia_ore=48)`. M9 risponde a "cosa merita attenzione?", M10 a "quale azione assistiva concreta è disponibile adesso?".

**Input (soluzione B, 2026-10-06).** Il contratto pubblico di M9 (`school_priority.build(dettaglio=true)`), più il contratto pubblico di M8 (`school_status.build(dettaglio=true)`) usato **solo** per risolvere le evidenze `(segnale, uid, hash)` che M9 cita.
- Nessun ricalcolo di segnali, priorità o affidabilità.
- Nessuna lettura del testo libero `motivo`.
- Un'evidenza si risolve solo se uid **e** hash coincidono (in M8 e nell'elemento M7). Altrimenti l'azione è `non_eseguibile` con il gap `evidenza_non_risolvibile`.

Catena: Registry (content_hash) → M2/M5/M6 → M7 (elemento) → M8 (segnale) → M9 (voce) → M10 (azione). Il tool legge l'orologio una sola volta e passa lo stesso `now` a M9 e a M8. Lo stesso input produce lo stesso JSON byte per byte.

**Azioni.** Vocabolario chiuso, identico a M9: `controllare_verifica`, `aprire_preparazione`, `controllare_compito`, `leggere_comunicazione`, `verificare_informazione`. Per ogni azione di M9, M10 produce un'azione con:
- `target`;
- `eseguibilita` (`eseguibile` | `parzialmente_eseguibile` | `non_eseguibile`);
- `affidabilita` e `affidabilita_m9`;
- `priorita_m9`, `voce_m9`, `regola_m9`;
- `contesto` (solo campi già presenti negli elementi risolti);
- `gaps`;
- `provenienza` (voce M9, evidenze M8 con flag `risolta`, elemento M7).

Priorità, affidabilità ed eseguibilità sono campi distinti.

**Contesto per azione.**
- `controllare_verifica`: data, fonte (origine, docente, stato M6), testo della verifica riportato così com'è, collegamenti M5 della verifica esposti da M9, comunicazioni collegate solo tramite collegamenti M5 risolti. Gli **argomenti** vengono **solo** dal testo della verifica e dai collegamenti M5: mai dalle lezioni recenti.
  - `collegamenti_m5_copertura` dice come leggere `collegamenti_m5`. M9 espone i collegamenti M5 di una verifica (segnale S6) **solo se generano un'azione**.
    - `esposti_da_m9`: M9 pubblica S6 per la verifica. La lista è quella di M8 per quella verifica, compresi i collegamenti `non_determinabile` e `conflitto`. Una lista vuota in questo caso vorrebbe dire che M8 non ne ha.
    - `non_esposti_da_m9`: M9 non pubblica S6 per la verifica, quindi `collegamenti_m5: []` **non** significa che non esistano collegamenti. Significa solo che non sono esposti a questo livello. Lo dice la `nota_copertura`, e lo stesso vale per `comunicazioni_collegate: []`.

    Esempio: "Verifica su Ariosto" ha un collegamento M5 `non_determinabile` con l'annuncio "Ariosto", ma risulta `non_esposti_da_m9`. L'elenco completo è in `school_links` / `school_link_explain`.
  - Il campo non è un gap e non cambia né l'eseguibilità né l'affidabilità.
- `aprire_preparazione`: segue solo il collegamento citato da M9, con relazione e stato (`probabile`/`proposto` restano tali; nota "non confermato").
- `controllare_compito`: scadenza, origine, stato Classroom e della consegna. Per gli scaduti la formula è "non consegnato secondo Classroom".
- `leggere_comunicazione`: data, testo disponibile, classe e stato M6 così come sono, caso speciale.
- `verificare_informazione`: cosa non è determinabile (dal **codice** della regola M9: P1b data della verifica, P2 collegamento in conflitto, P6 affidabilità), l'oggetto coinvolto, i motivi di affidabilità M8 dei segnali coinvolti. L'incertezza non viene risolta.

**Gap e loro effetto.**

| Gap | Effetto sull'eseguibilità |
|---|---|
| `evidenza_non_risolvibile` | non_eseguibile |
| `data_non_utilizzabile` | non_eseguibile |
| `argomenti_non_disponibili` | parzialmente_eseguibile |
| `stato_consegna_non_disponibile` (compiti di agenda ClasseViva) | parzialmente_eseguibile |
| `affidabilita_non_determinabile` | parzialmente_eseguibile |
| `collegamento_non_determinabile` | nessuno, solo dichiarato |
| `collegamento_in_conflitto` | nessuno, solo dichiarato |
| `contenuto_parziale` (`testo_troncato=true`) | nessuno: l'oggetto resta identificato e l'azione può restare eseguibile |

Ogni gap deriva da un dato M9 o M8.

**Affidabilità (monotona).** Affidabilità dell'azione = la peggiore tra quella M9 e il tetto dell'eseguibilità (eseguibile → alta, parziale → media, non eseguibile → non_determinabile). Non supera mai M9; `non_determinabile` resta tale.

**Storico.** Le azioni nascono solo dalle voci M9, quindi dall'anno operativo: lo storico non genera mai azioni.

**Cosa M10 non fa.** Eseguire azioni, scrivere su Registry, Classroom, Gmail o Notion, automazioni esterne, piani di studio, durate, giudizi, voti, diagnosi, nuove evidenze o nuove azioni.

**Cosa Claude non deve inventare.** Argomenti non presenti nel testo o nei collegamenti; che un collegamento probabile sia certo; che un compito "non consegnato secondo Classroom" non sia stato fatto; il contenuto mancante di un testo troncato; la soluzione di un'incertezza segnalata da `verificare_informazione`.


## 14. Contesto di studio per materia (M11 v1) — `cloud.school.study_context/1`
Vale solo per M11. Non cambia M2–M10: i loro contratti restano byte-identici. Nessuna tabella, migrazione o sync.

**Natura.** `school_study_context.py`, tool `school_study_context_v1(materia, dettaglio=false, soglia_ore=48)`. M11 è un contesto derivato in sola lettura e **non è uno Study Assistant**: non insegna, non spiega, non pianifica, non assegna tempi, non diagnostica, non giudica.

**Decisioni vincolanti (2026-10-06).**
- **D-M11-1:** materia obbligatoria; nessuna panoramica.
- **D-M11-2:** input = contratti pubblici di M10 (`school_action.build(materia, dettaglio=true)`, che include M9 transitivamente) e di M8 (`school_status.build(materia, dettaglio=true)`: lezioni S4, copertura, storico, ambiguità). Nessuna lettura diretta di M1–M7 o M9, nessun ricalcolo.
- **D-M11-3:** obiettivi 1:1 con le azioni M10 `controllare_verifica` / `controllare_compito`. Le lezioni sono contesto della **materia**: nessun legame lezione ↔ obiettivo viene mai dedotto.
- **D-M11-4:** usi formali chiusi, descritti più sotto.
- **D-M11-5:** materiali **solo** dai collegamenti M5 esposti da M10 (`aprire_preparazione`). I materiali M4/`school_materials` (attribuiti per corso, non per materia) non sono usati. Nessun materiale è dichiarato utile o bastante.
- **D-M11-6:** `data` e `priorita_m9` riportate come ricevute; nessun calcolo di giorni o di priorità.

**Stati del dato (chiusi).**

| Stato | Significato |
|---|---|
| `dato_disponibile` | presente in una fonte letta (uid + hash) |
| `dato_rilevante` | legato all'obiettivo da una relazione strutturale già esistente: stesso oggetto, oppure collegamento M5 citato da M10. Le lezioni non lo sono mai. |
| `dato_sufficiente` | **solo** come esito `soddisfatto` di un uso formale, mai "per studiare" |
| `non_determinabile` | fonte letta ma relazione o attributo indecidibile (M5 `non_determinabile`, copertura `non_esposti_da_m9`, elemento non risolto, U4) |
| `non_disponibile` | con motivo distinto: `fonte_non_letta`, `non_presente_nella_fonte` (testo o argomento vuoto) oppure `vuoto_valido` (fonte letta, 0 elementi) |

**Usi formali.**

| Uso | Soddisfatto | Non soddisfatto | Non determinabile |
|---|---|---|---|
| U1 `identificare_obiettivo` | azione M10 eseguibile o parziale con data | — | negli altri casi |
| U2 `testo_dichiarato_disponibile` | testo presente | testo vuoto | elemento non risolto |
| U3 `materiale_collegato_identificabile` | almeno una preparazione M10 | collegamenti esposti e nessuno presente | solo `non_determinabile`/conflitti, oppure `non_esposti_da_m9` |
| U4 `argomenti_dell_obiettivo` | — | — | **sempre** in v1: M11 non valuta se il testo descrive argomenti |

**Struttura.** `stato_input` (stato M9 ricevuto e `obiettivi`: presenti | nessuno_nel_perimetro | non_determinabile), poi `obiettivi[]`, `lezioni`, `comunicazioni.da_leggere`, `da_verificare`, `materiali`, `lacune_informative`, `ambiguita`, `informazione_insufficiente`, `affidabilita_minima`, `storico`, `politica`, `provenance`.

Ogni obiettivo contiene: `testo_dichiarato`, `collegamenti_m5` con copertura, `preparazione`, `comunicazioni_collegate`, `usi`, `lacune_m10`, `ambiguita` e `provenienza` (azione M10, voce M9, evidenze M8, elemento M7).

**Informazione insufficiente.** Sono frasi fisse in `informazione_insufficiente` (codice + ambito + uid). Codici:
- `argomenti_obiettivo` (sempre, per ogni obiettivo);
- `materiale_collegato`;
- `materiali_necessari`;
- `obiettivo_non_identificabile`;
- `azione_non_completa`;
- `fonte_non_letta`;
- `collegamenti_non_esposti`;
- `lezioni_senza_argomento`;
- `obiettivi_non_determinabili`.

**Affidabilità (monotona).**
- Obiettivo = azione M10.
- Preparazione = azione `aprire_preparazione`.
- Uso = minimo delle fonti usate.
- Lezioni = S4, `bassa` per una lezione con discordanza M2.

Non supera mai la fonte e non si aggrega in indici di preparazione, prontezza o rendimento. Sui dati reali, U3 per la verifica di matematica è `soddisfatto` con affidabilità `media` (collegamento M5 probabile); per la verifica su Ariosto è `non_determinabile` (copertura `non_esposti_da_m9`).

**Temporalità.** Gli obiettivi vengono da M10 (2026-27) e le lezioni da M8 S4 (2026-27). Lo storico compare solo come conteggi separati: non crea obiettivi, preparazioni o priorità. Il tool legge l'orologio una sola volta.

**Cosa M11 non deriva mai.** Argomenti da date o titoli delle lezioni; legami lezione ↔ verifica; ambito di una verifica; materiali utili o bastanti; durata o ordine di studio; stato di preparazione; mancanze dello studente; rendimento; date da riferimenti relativi; attribuzione di materiali di corsi condivisi; contenuto di allegati.


## 15. Study Assistant v1 — orchestratore/guardrail (M12) — `cloud.school.study_assistant/1`
Vale solo per M12. Non cambia M1–M11: i loro contratti restano byte-identici. Nessuna tabella, migrazione o sync.

**Natura.** `school_study_assistant.py`, tool `school_study_assistant_v1(materia, obiettivo=None, modalita=None, dettaglio=false, soglia_ore=48)`. M12 è un **orchestratore e guardrail**, non un generatore didattico. Stabilisce:
- quali modalità di assistenza sono ammesse;
- con quali contenuti verificati;
- con quale affidabilità e quali limiti;
- con quali frasi obbligatorie;
- con quali azioni ammesse e vietate.

Il testo finale lo scrive Claude, dentro questi vincoli. Il tool legge l'orologio una sola volta; lo stesso input produce lo stesso JSON byte per byte; nessuna scrittura.

**Decisioni D-M12-1 … D-M12-7 (2026-10-06).**
- **D-M12-1:** identità e nomi come sopra.
- **D-M12-2:** input = **solo** il contratto pubblico di M11 (`school_study_context.build`); nessuna lettura di M1–M10, nessun ricalcolo.
- **D-M12-3:** materia obbligatoria; `obiettivo` facoltativo, solo uid **esatto** di un obiettivo M11. Un uid inesistente dà `obiettivo_non_trovato`, con l'elenco degli obiettivi esistenti e nessuna ricerca approssimata. Nessuna panoramica.
- **D-M12-4:** cinque modalità, elenco chiuso (tabella più sotto).
- **D-M12-5:** contenuti solo da elementi M11 con stato `dato_disponibile` / `dato_rilevante` / `dato_sufficiente`, copiati verbatim. Il non determinabile sta solo nei limiti. Lezioni e obiettivi mai nella stessa modalità; nessuno storico.
- **D-M12-6:** guardrail chiusi.
  - Azioni ammesse: `riportare_fatto`, `citare_fonte`, `riformulare_senza_aggiungere`, `elencare`, `indicare_limite`.
  - Azioni vietate (sempre): `spiegare_argomento`, `ricavare_argomenti`, `collegare_lezione_obiettivo`, `stimare_tempo`, `proporre_piano`, `valutare_preparazione`, `dichiarare_materiale_necessario`, `risolvere_incertezza`, `inventare_contenuto`, `usare_storico`, `creare_esercizi_o_quiz`, `riassumere_argomenti_non_presenti`.
  - Frasi obbligatorie fisse per modalità.
- **D-M12-7 (opzione A):** il materiale collegato è `{uid, tipo, data, stato_collegamento, affidabilità}` con il limite esplicito "titolo non disponibile a questo livello". M11 non viene modificato.

**Le cinque modalità.**

| Modalità | Requisito M11 | Contenuti | Disponibilità |
|---|---|---|---|
| `quadro_obiettivo` | U1 soddisfatto | obiettivo: tipo, data, `priorita_m9`, eseguibilità, testo dichiarato verbatim, fonte | `limitata` se un'azione M10 è parziale, il testo è troncato o un obiettivo non è identificabile |
| `materiale_collegato` | U3 soddisfatto | materiale {uid, tipo, data, stato del collegamento, relazione, `titolo: null`} | sempre `limitata` (titolo assente); `non_disponibile` per U3 non soddisfatto o non determinabile (es. Ariosto) |
| `rassegna_lezioni` | lezioni disponibili con almeno un argomento | solo le lezioni con argomento, verbatim, con la frase di non-legame agli obiettivi | `limitata` se alcune lezioni non hanno argomento (escluse e contate); `non_disponibile` se nessun argomento, fonte non letta o vuota. Mai argomenti ricavati da verifiche, titoli, date o vicinanza temporale. |
| `comunicazioni_da_leggere` | almeno una comunicazione da leggere | uid, data, testo, classe e stato M6 | `limitata` se un testo è troncato |
| `chiarimento_limiti` | **sempre** disponibile | nessun contenuto; i limiti, `da_verificare`, gli usi non soddisfatti e l'informazione insufficiente stanno in `limiti` | sempre `disponibile` |

Modalità didattiche (spiegazioni, esercizi, quiz, riassunti, piani, tempi, valutazioni) e argomenti indicati dall'utente sono fuori da v1.

**Richiesta.**
- `modalita` assente: disponibilità delle 5 modalità, in ordine fisso, senza sceglierne una "migliore".
- `modalita` richiesta e disponibile: `ammessa`, solo quella modalità.
- `modalita` richiesta ma non disponibile: `non_disponibile`, con motivo e sole alternative disponibili. **Nessun contenuto di ripiego.**
- `modalita` sconosciuta: errore.

Una modalità `non_disponibile` non ha mai contenuti né affidabilità.

**Provenienza e hash.**
- Obiettivi e lezioni portano l'uid e l'hash di M11.
- Per **materiali** e **comunicazioni** M11 espone l'uid ma non l'hash. M12 lo dichiara (`hash: null`, `hash_esposto_da_m11: false`, frase `hash_non_esposto`) e dà la provenienza:
  - materiale: obiettivo + hash dell'obiettivo, collegamento, azione M10;
  - comunicazione: azione M10.

  Nessun hash viene inventato.

**Affidabilità.** Modalità = minimo dei contenuti e dei requisiti M11 usati: mai sopra M11, monotona. `chiarimento_limiti` non ha affidabilità propria (riporta limiti).

**Informazione insufficiente.** Le frasi di M11, più i codici M12: `modalita_non_disponibile`, `obiettivo_non_trovato`, `nessun_obiettivo_nel_perimetro`, `obiettivi_non_determinabili`, `materiale_senza_titolo`.

**Cosa Claude non deve fare con M12.** Usare modalità non disponibili; aggiungere contenuti non presenti; spiegare argomenti, creare esercizi o quiz, proporre piani o tempi, valutare la preparazione; collegare lezioni e obiettivi; omettere le frasi obbligatorie.


## 16. Sessione di studio (M13 v1) — `cloud.school.study_session/1`
Vale solo per M13. Non cambia M1–M12: i loro contratti restano byte-identici. Nessuna tabella, migrazione o sync.

**Natura.** `school_study_session.py`, tool `school_study_session_v1(materia, modalita, contenuto, obiettivo=None, dettaglio=false)`. M13 è il primo layer che abilita l'assistenza didattica, ma resta un **orchestratore e guardrail**: stabilisce su quale contenuto si lavora, con quale provenienza, entro quali azioni e con quali frasi obbligatorie. La risposta didattica la scrive Claude. Il tool legge l'orologio una sola volta e lo passa a M12; lo stesso input produce lo stesso JSON byte per byte; nessuna scrittura.

**Architettura.** M11 = contesto scolastico; M12 = cosa è lecito usare e quali limiti valgono; M13 = sessione di studio; Claude = generazione finale. M13 legge **solo** il contratto pubblico di M12 e il contenuto fornito nella richiesta. Nessuna lettura di M1–M11, nessuna modifica a M11/M12, nessuna acquisizione automatica da Classroom o Drive.

**Tre fonti sempre separate.**
- `contenuto_scolastico`: testo di un elemento **esposto da M12** e richiamato esplicitamente per uid (`riferimenti_m12`), con uid, hash M12 (quando M12 lo espone), tipo e affidabilità. Testi scolastici utilizzabili: testo dichiarato di un obiettivo, argomento di una lezione, testo di una comunicazione. Un materiale non ha testo a questo livello, quindi dà `riferimento_senza_testo`.
- `contenuto_fornito_utente`: testo della richiesta (anche l'eventuale `risposta_utente` per `correggi`), **non verificato** dalle fonti. Un'`origine_dichiarata` dall'utente resta una dichiarazione e non diventa mai contenuto scolastico.
- `conoscenza_generale`: ciò che Claude aggiunge. Va sempre etichettato e **mai** attribuito a ClasseViva, Classroom, M11 o M12. Non compare mai nel contenuto di M13: è solo un'etichetta obbligatoria per la risposta.

Ogni parte di contenuto porta `sha256` del testo esatto, lunghezza, anteprima (≤ 160 caratteri) e verifica. Il testo completo compare solo con `dettaglio`. Se il testo cambia, cambia lo `sha256`.

**Input `contenuto`.** Testo, oppure un oggetto con soli `testo`, `origine_dichiarata`, `riferimenti_m12` (uid esatti), `risposta_utente`. Campi diversi vengono rifiutati: per esempio un URL, perché nessuna acquisizione è ammessa.

**Modalità (elenco chiuso).**

| Modalità | Azioni ammesse sul contenuto fornito | Divieto specifico (in aggiunta ai comuni) |
|---|---|---|
| `spiega` | spiegare, chiarire con conoscenza generale etichettata, esempi etichettati | — |
| `interroga` | domande sul contenuto | `presentare_domande_come_programma` |
| `esercitati` | esercizi sul contenuto, soluzioni etichettate | `presentare_esercizi_come_richiesti_dalla_scuola` |
| `correggi` | analizzare la `risposta_utente` (obbligatoria) e spiegare gli errori; senza contenuto di riferimento c'è la frase "correzione su conoscenza generale" | `giudizio_globale_dalla_correzione` |
| `ripassa` | organizzare concetti, punti chiave, relazioni e formule, domande di ripasso | `chiamare_programma_completo` |
| `prepara_obiettivo` | usa il contesto M12 dell'obiettivo; con contenuto lavora su quello | `dichiarare_coincidenza_contenuto_programma` |

Per `prepara_obiettivo`:
- `obiettivo` è obbligatorio: senza, l'esito è `obiettivo_richiesto` e gli obiettivi esistenti sono elencati, mai scelti.
- Gli argomenti dell'obiettivo non sono determinati (limite e frase obbligatoria).
- Senza contenuto l'esito è `solo_contesto` e le azioni sono limitate al contesto M12.

**Divieti comuni (sempre).** `assegnare_voti`, `diagnosticare`, `valutare_preparazione_globale`, `dichiarare_pronto_o_indietro`, `stimare_tempo`, `proporre_piano_automatico`, `scegliere_cosa_studiare`, `dichiarare_programma_verifica`, `attribuire_conoscenza_generale_alla_scuola`, `acquisire_materiali`, `inferire_dati_scolastici`, `inventare_argomenti_obiettivo`, `inventare_contenuto_scolastico`.

I divieti di M12 restano validi per il **contesto scolastico**; le azioni didattiche di M13 valgono **solo** sul contenuto fornito.

**Esiti.**

| Esito | Quando | Azioni consentite |
|---|---|---|
| `ammessa` | condizioni della modalità rispettate | azioni della modalità |
| `solo_contesto` | `prepara_obiettivo` senza contenuto | solo contesto M12 |
| `contenuto_insufficiente` | contenuto o risposta mancante, oppure riferimenti non risolti | solo `chiedere_contenuto_all_utente` e `indicare_limite` |
| `obiettivo_richiesto` | `prepara_obiettivo` senza obiettivo | come sopra |
| `obiettivo_non_trovato` | uid non esatto | come sopra, nessuna ricerca approssimata |

Ogni esito diverso da `ammessa` porta le frasi di `informazione_insufficiente`.

**Lessico.** Sono vietate le parole di giudizio, tempo e piano (per esempio "sei pronto", "sei indietro", "voto stimato", "piano di studio", "programma completo della verifica"). I verbi di studio sono ammessi, perché M13 abilita lo studio sul contenuto fornito.

**Cosa Claude non deve fare con M13.** Inventare contenuto scolastico o argomenti di una verifica; presentare domande o esercizi come programma o come richiesti dalla scuola; presentare conoscenza generale come proveniente dalla scuola; trasformare una correzione in un giudizio sulla preparazione; dare voti, tempi o piani; scegliere cosa studiare; omettere le frasi obbligatorie e le etichette di provenienza.


## 17. Registro valutativo e presenze (M14 v1) — `cloud.school.records/1`
Vale solo per M14. Non cambia M1–M13: i loro contratti restano byte-identici. Nessuna tabella, migrazione o sync.

**Natura.** `school_records.py`, tool `school_records_v1(materia?, periodo?, dettaglio=false, soglia_ore=48)`. Fatti e statistiche **descrittive** su voti e assenze, accanto ai layer di contesto (non sopra M8). Nessun giudizio, rendimento, previsione o confronto.

Riusa la normalizzazione esistente (`cvv_registry.normalized` → `normalize_grade` / `normalize_absence`; interpretazioni solo da tabelle verificate) e la freschezza M3. Il tool legge l'orologio una sola volta; lo stesso input produce lo stesso JSON byte per byte; nessuna scrittura.

**Voti.**
- Ogni voto è riportato verbatim: `displayValue`, `decimalValue`, `weightFactor` come `peso_raw` (mai inventato), componente, periodo, annullato, uid, hash e versione del Registry; con `dettaglio` anche il record grezzo.
- Inclusione dichiarata per ogni voto: `incluso`, `escluso_annullato` (`canceled` vero), `escluso_non_numerico` (`decimalValue` assente o non numerico).

**Medie: politiche nominate, elenco chiuso e versionato.**

| Politica | Formula | Non calcolabile se |
|---|---|---|
| `media_pesata_weightfactor` (primaria proposta) | somma(decimalValue·w) / somma(w), con w = `weightFactor` se numerico e ≥ 0, altrimenti 1 (peso **assunto**, elencato in `pesi_assunti`) | somma(w) = 0 oppure nessun voto incluso |
| `media_aritmetica_semplice` | somma(decimalValue) / n | nessun voto incluso |

- Ogni media porta `valore` (2 decimali), `valore_esatto`, `uid_usati`, `n`.
- `corrispondenza_classeviva` vale **`non_verificata`**: la media non va mai presentata come "la tua media" né come media ufficiale.
- Nuove politiche si aggiungono senza riscrivere i dati (i grezzi sono conservati).

**Stati distinti per ogni ambito** (complessivo, per materia, per periodo):

| Stato | Significato |
|---|---|
| `fonte_non_letta` | campi `null`, mai 0 |
| `nessun_voto` | fonte letta, 0 voti |
| `media_non_calcolabile` | ci sono voti, ma nessuno incluso, oppure somma dei pesi = 0 |
| `media_calcolabile` | media disponibile |

**Ambiti e grafici.**
- Le 8 materie sono sempre elencate; compaiono anche altre materie ClasseViva con voti (per esempio religione).
- I voti con materia non riconosciuta sono elencati in `non_attribuiti` e contano solo nel complessivo.
- Distribuzione per fasce fisse `[k,k+1)` (ultima `[9,10]`), calcolata solo sui voti inclusi.
- Serie temporale con la media progressiva per ogni politica (per i grafici dell'andamento).

**Assenze.**
- Eventi verbatim (codice, data, ora, ore perse solo per i codici verificati, giustificazione).
- Tipo solo dalla tabella verificata (`ABR0` entrata in ritardo, `ABA0` assenza); altrimenti `Da verificare`.
- Conteggi per tipo e per mese.
- Le assenze non hanno materia e non vengono attribuite.
- Stati: `non_letto` (eventi e conteggi `null`), `vuoto_valido`, `presente`.

**Temporalità.** Anno operativo = anno scolastico di `now` (2026-27). Gli altri anni compaiono solo come conteggi in `storico`.

**Cosa Claude non deve fare con M14.** Presentare una politica come la media ufficiale dello studente; trasformare un dato mancante in zero; dedurre pesi, tipi di valutazione o materie; parlare di rendimento, tendenze, previsioni o giudizi; attribuire assenze a materie.


## 18. Catalogo materiali (M15 v1) — `cloud.school.materials_catalog/1`
Vale solo per M15. Non cambia M1–M14: i loro contratti restano byte-identici. Nessuna tabella, migrazione, sync o download.

**Natura.** `school_materials_catalog.py`, tool `school_materials_catalog_v1(materia?, obiettivo?, perimetro="operativo", limite=50, pagina=1, dettaglio=false, soglia_ore=48)`. Il catalogo **identifica** e **associa** i materiali citati dalle fonti, senza acquisirli: un riferimento a un file non è il file, e un compito con allegato non dice che quel materiale serva o basti. Il tool legge l'orologio una sola volta; lo stesso input produce lo stesso JSON byte per byte; nessuna scrittura e nessuna rete.

**Fonti (sola lettura, nessuna logica nuova).**
- Allegati di `material`, `course_work` e `announcement` Classroom, tramite la normalizzazione esistente (`cls_registry`: tipo, id, titolo, url, share_mode).
- Allegati delle circolari ClasseViva (`fileName`, `attachNum`).
- Corso → materia: solo conferme di `school_manual.json` e regola **per elemento** di M5 (`item_subject`). Le proposte di corso (`cls_subjects`) valgono solo come `possibile`.
- Associazioni a verifiche e lezioni: solo collegamenti M5 esistenti (collegamenti, conflitti, non determinabili), con il loro stato.

**Due assi separati per ogni riferimento.**
- `stato_materiale`: `identificato_non_acquisito`, oppure `non_disponibile` (elemento non pubblicato o scomparso dalla fonte, riferimento senza identificativo). `acquisito` e `fornito_utente` sono **riservati a M19** e qui non vengono mai prodotti.
- `attribuzione`:
  - `determinata`: corso monomateria confermato, oppure topic che nomina una sola materia;
  - `possibile`: corso condiviso senza topic, oppure corso proposto non confermato; con i candidati;
  - `non_determinabile`;
  - `generale`: allegati delle circolari, mai di una materia.

  L'**appartenenza al corso** (confermato per, proposto per, condiviso) è riportata a parte. La semantica "per corso" di M4/`school_materials` non diventa mai un'attribuzione per elemento. *Ambiguo* = attribuzione `possibile` o `non_determinabile`.

**Riferimento.** Per ogni riferimento:
- id deterministico `uid_padre#tipo:identificativo`;
- tipo di risorsa (drive_file, youtube, link, form, allegato_classeviva…);
- titolo e url verbatim; id Drive;
- padre: uid, tipo, titolo o testo (≤ 160 caratteri), data, anno, hash e versione del Registry, stato nella fonte, corso, topic;
- associazioni M5.

La forma compatta (default) riduce padre e associazioni; `dettaglio=true` restituisce il riferimento completo.

**Filtri e perimetro.**
- `materia`: riferimenti determinati per quella materia, oppure possibili con quella materia tra i candidati. Gli allegati delle circolari compaiono solo senza filtro materia.
- `obiettivo`: solo riferimenti collegati via M5 all'uid **esatto**.
- `perimetro`: `operativo` (anno scolastico di `now`) **oppure** `storico`, mai mescolati; l'altro perimetro compare solo come conteggio.
- Ordine: data del padre decrescente, poi id.
- Paginazione deterministica (`limite` oltre 200 viene normalizzato a 200 e il valore richiesto resta dichiarato).

**Cosa Claude non deve fare con M15.** Dire che un materiale serve, basta o copre il programma; trattare un riferimento come contenuto letto; attribuire a una materia un materiale `possibile` o un allegato di circolare; presentare un'associazione M5 come certa; mescolare operativo e storico.

## 19. Modello di vista della dashboard (M16 v1) — `cloud.school.dashboard/1`
Vale solo per M16. Non cambia M1–M15: i loro contratti restano byte-identici. Nessuna tabella, migrazione, sync, rete o scrittura (Notion compreso).

**Natura.** `school_dashboard.py`, tool `school_dashboard_v1(pagina="home", materia?, soglia_ore=48)`: restituisce **una pagina** della UI Cloud Scuola. Il contenuto è calcolato nel backend; Notion (M17) e qualsiasi altra superficie ne saranno solo una proiezione. M16 **seleziona, ordina e formatta** contratti già verificati, senza logica scolastica nuova:
- M3: `registry.freshness` e `registry.events`;
- M8 `school_status`;
- M9 `school_priority`;
- M10 `school_action`;
- M12 `school_study_assistant`;
- M14 `school_records`;
- M15 `school_materials_catalog`.

Tutti i layer ricevono lo stesso `now`, letto una sola volta dal tool. Lo stesso input produce lo stesso JSON byte per byte.

**Pagine (vocabolario chiuso).** `home`, `attenzione`, `materie` (8 schede), `materia` (richiede `materia`), `verifiche`, `compiti`, `voti`, `assenze`, `studio`, `materiali`, `storico`, `qualita_dati`. Ogni risposta contiene `navigazione` con l'elenco delle pagine.

**Blocco.** Ogni blocco ha `{id, titolo, stato, affidabilita, provenienza{contratto, versione, uid?}, dati, nota?}`. Gli stati sono:
- `dati`;
- `vuoto_valido`: fonte letta, nessun elemento;
- `non_letto`: fonte mai letta, oppure segnale nullo in tutte le materie;
- `non_disponibile`: il dato non esiste a quel livello, per esempio le assenze per materia;
- `non_attivo`: layer futuro (M18 dichiarazioni, M19 store, M21 profilo, M22 piano); non viene mai simulato.

L'affidabilità è quella della fonte, mai alzata.

**Grafici.** I grafici sono serie dichiarative: `{id, titolo, tipo ∈ linea|barre|barre_impilate|calendario|indicatore, asse_x, asse_y, serie[{nome, punti[{x, y, uid}]}], provenienza, formula?, valore_verificato?}`.
- Un grafico è emesso solo se ha almeno un punto con `y` non null.
- Un null non diventa mai 0: il punto viene omesso. Uno 0 reale, cioè un conteggio, resta 0.
- Le medie portano `formula` e `valore_verificato: false`: sono politiche M14 nominate, con corrispondenza ClasseViva non verificata, e non sono mai "la media" dello studente.

**Contenuto delle pagine.**
- `home`: oggi, materie per priorità (M9), verifiche e scadenze future (P1/P3a M9), azioni disponibili (M10), dati incerti, novità, dove studiare, sintesi qualità.
- `attenzione`: voci M9 per materia, con le azioni M10 collegate.
- `materia`: scheda, priorità e azioni, segnali M8 S1/S2/S3/S4/S7 con evidenze, materiali M15, voti M14, studio M12, blocchi `non_attivo`, qualità (copertura, gap, ambiguità, freschezza).
- `verifiche`: S1 `presente` per materia, poi gli indizi non datati separati e il calendario.
- `compiti`: S2/S3.
- `voti` / `assenze`: M14, con grafici solo se esistono voti o assenze.
- `studio`: obiettivi M12 e strumenti M13.
- `materiali`: conteggi M15, per materia, collegati alle verifiche tramite M5.
- `storico`: soli conteggi degli anni precedenti, mai mescolati alle altre pagine.
- `qualita_dati`: freschezza per kind, copertura/gap/ambiguità/regole non valutate per materia, gap M14/M15.

**Novità.** Eventi `registry.events` degli ultimi 7 giorni rispetto a `now`. Il confronto avviene tra istanti, perché il Registry salva in UTC. I `create` del **primo** run commit di un kind (la prima acquisizione) non contano come novità: sono contati in `prima_lettura_esclusa`. Vengono mostrati al massimo 15 eventi, con il totale.

**Cosa Claude non deve fare con M16.** Calcolare in Notion o nella UI indicatori che il modello non fornisce; disegnare null come zero; presentare una media come ufficiale; mescolare storico e anno operativo; riempire i blocchi `non_attivo`; giudicare lo studente.

## 20. Proiezione Notion della dashboard (M17 v1) — `cloud.school.notion_projection/1`

> **RIMOSSA il 2026-10-24.** Notion non fa parte di CUORE: moduli `notion_*`, strumenti MCP `school_notion_*` e
> `cv_sync_plan`, stato Notion del job e comando `notion-verified` sono stati eliminati; i contenuti caricati in Notion
> sono stati rimossi. La sezione resta solo come storia.
> La dashboard di CUORE è `app/` (feed `cuore.school.feed/1`).
Vale solo per M17. Non cambia M1–M16 né `notion_projection.py`, che viene riusato senza modifiche per 📝 Verifiche e 📚 Compiti. **Notion non è la fonte di verità**: è una vista del backend e non contiene logica critica (nessuna formula).

**Natura.** `notion_dashboard.py`, tool `school_notion_plan_v1(stato_notion, dettaglio=false, op_ids?, soglia_ore=48)`.
- Il modulo è **puro**: calcola un **piano** di operazioni per il connettore Notion e non scrive nulla. L'agente esegue le operazioni solo dopo conferma, in ordine, rileggendo lo stato prima del passo successivo.
- Ogni operazione indica lo strumento del connettore (`notion-create-pages`, `notion-update-page`, `notion-create-database`, `notion-create-view`) e i suoi argomenti esatti.
- Il tool legge l'orologio una sola volta. Lo stesso input produce lo stesso piano, byte per byte.

**Struttura proiettata.**
- **Hub "Cloud Scuola"**, sotto la pagina esistente 🎓 Scuola, che resta intatta salvo l'aggiunta della pagina figlia.
- **Pagine generate da M16**: HOME, ATTENZIONE, MATERIE (con 8 pagine "Materia · …"), VERIFICHE, COMPITI, VOTI, ASSENZE, STUDIO, MATERIALI, STORICO, QUALITÀ DATI.
- **Pagina utente "✍️ Note personali"**: creata vuota una volta e **mai** più toccata.
- **Database esistenti riusati**: 📝 Verifiche e 📚 Compiti (regole `notion_projection`: Nome e Stato sono dell'utente dopo la creazione; Fatto non viene mai sovrascritto), 🕘 Assenze (grezzo, interpretato e versione, come le righe esistenti; Note mai scritte).
- **Database nuovi nello hub**:
  - 🧮 Voti: M14, anno operativo; Valore vuoto se il voto non è numerico, mai 0;
  - 📎 Materiali: M15, perimetro operativo; Materia valorizzata solo se l'attribuzione è `determinata`;
  - 📈 Indicatori: una riga per punto dei grafici M16; i punti null non hanno riga.

**Grafici.** Ogni grafico M16 numerico diventa una vista `chart` di 📈 Indicatori: `FILTER Grafico; FILTER Stato = Attuale; GROUP BY X; CHART … AGGREGATE sum ON Valore [STACK BY Serie]`. Ogni (X, Serie) ha **una sola** riga, quindi la somma restituisce il valore del backend: Notion non calcola nulla. Il grafico calendario diventa la vista "Calendario verifiche (Cloud Scuola)" di 📝 Verifiche. Si creano viste solo per i grafici che esistono, cioè che hanno punti reali.

**Idempotenza.**
- **Pagine**: contengono un'impronta `cs1:<16 hex>` calcolata da contenuto, figli e versioni. Si aggiornano solo se l'impronta cambia; i figli (pagine e database, anche dell'utente) restano sempre nel nuovo contenuto.
- **Righe**: chiave e `Hash`. L'hash diverso produce un update dei soli campi gestiti; una versione di interpretazione diversa produce un `reinterpret`, cioè solo i campi interpretati.
- **Viste**: identificate per nome e solo create.
- **Convergenza**: il piano converge a un punto fisso (`anteprima_convergenza`). Le operazioni che dipendono da id non ancora esistenti sono `bloccate` fino al passo successivo.

**Impronta cs2 (M17.1, proiezione r2).** L'impronta di una pagina dipende **solo** dal contenuto strutturale (corpo, figli, versione del modello dashboard).
- La data `dati al …` e la versione di proiezione restano visibili nel piè di pagina ma **non** entrano nell'impronta.
- Anche la data del blocco "Oggi" e le età in ore misurate a `now` (`eta_ore`, mostrate come "età in ore (al calcolo)") sono neutralizzate nell'impronta. La data di "Oggi" è mostrata come "data di riferimento del calcolo", cioè la data dell'ultimo contenuto scritto.
- I valori derivati dal tempo che hanno un significato (giorni mancanti, "tra N giorni", dato scaduto sì/no, finestra delle novità) **sono contenuto**: se cambiano, la pagina si aggiorna.
- Una pagina che non cambia non viene riscritta solo perché è cambiato il giorno.
- Le pagine già scritte con impronta `cs1` (M17 r1) sono riconosciute ricalcolando l'impronta legacy con la data e le versioni lette dal loro piè di pagina. Se il contenuto è identico, non vengono riscritte, quindi non serve una migrazione.

**M17.2 (proiezione r3).**
- **ID per il connettore:** ogni riferimento a pagina o database negli argomenti delle operazioni (`page_id`, `parent.page_id`, `database_id`) è un ID Notion con trattini, ottenuto con `notion_id(url|id)`, mai un URL.
- **Niente link automatici:** i token che Notion trasforma da solo in link (`cloud.school.*` e `…drive_file:<id>`/`file:…`) sono scritti in `codice`. Il testo non cambia e i veri URL restano link.
- **Impronta invariata:** l'impronta è calcolata sul corpo semplice (testo r2). Una modifica di sola formattazione non riscrive le pagine già proiettate; la nuova resa arriva alla prima modifica reale del contenuto.

**M17.3 (proiezione r4): lettura normalizzata.** Rileggendo, Notion aggiunge link automatici e escape Markdown (`drive_[file:x](file:x)`, `4\^SC`, `cvv\:lesson\:1`). `notion_text` riporta ogni testo letto alla forma canonica scritta dal backend: chiavi e hash si confrontano sempre in forma canonica, e ciò che Cloud scrive non cambia. Se una chiave letta non coincide con nessuna desiderata ma è "quasi uguale" (stesse lettere e cifre), il piano **blocca** creazione e marcatura per entrambe e la segnala in `collisioni` e `problemi_sicurezza`: niente duplicati, niente righe marcate per errore.

**M17.4 (proiezione r5): associazioni e chiavi.**
- "Associazioni M5" dei Materiali segue lo schema reale M15: `relazione (stato): uid[, uid…] [regola R]`. Il campo `con` è una lista `[{uid, tipo}]` (conflitti) o un solo oggetto `{uid, tipo, data, materia}`; per quest'ultimo si aggiungono data e materia se presenti. Si scrivono solo valori presenti; nessuna associazione = proprietà vuota.
- Nessun `None` testuale nei campi calcolati: `check_plan_safe` blocca ogni testo con un `None` isolato. Il punto senza x di un grafico ha X e chiave `valore`.

**M17.5 (proiezione r6): opzioni delle select aperte.**
- Notion rifiuta un valore select che non è già un'opzione e i nomi di opzione con la virgola. Le select dichiarate senza opzioni nello schema (`OPEN_SELECTS`, oggi Pagina, Grafico, Serie, X di 📈 Indicatori) ricevono le opzioni con l'operazione `ensure_options` (`notion-update-data-source`, `ALTER COLUMN … SET SELECT(…)`), eseguita **prima** delle righe.
- L'istruzione contiene sempre le opzioni **lette** più quelle mancanti: un'opzione non viene mai rimossa. Se le opzioni del database non sono state lette, l'operazione non viene emessa e `check_plan_safe` blocca le righe ("opzioni … non lette"): nessuna riscrittura alla cieca.
- Nei valori delle select aperte la virgola diventa punto e virgola (`[0,1)` → `[0;1)`, notazione italiana degli intervalli). Il valore calcolato da M16 non cambia; cambiano solo l'etichetta Notion e la chiave della riga.

**M17.6 (proiezione r7): link http in lettura.** `notion_text` normalizza anche `[x](http://x)` e `[x](https://x)`: Notion collega da solo `cloud.school` nella proprietà Contratto degli Indicatori. Solo lettura: ciò che si scrive non cambia, e i link veri (testo diverso dalla destinazione) restano.

**M17.7 (proiezione r8, `notion_design.py` DESIGN_REV 4): dashboard progettata e grafici nativi incorporati.**
- *Presentazione.* Il corpo visibile di ogni pagina è scritto da `notion_design.py` (hero, indicatori in colonne, callout colorati, schede materia, elenco "In arrivo", navigazione "Sezioni" con `<mention url/>`); i dati restano quelli di M16. Il contenuto completo per blocco (stato, fonte, tabelle) resta sotto il toggle "Dati completi e provenienza (tecnico)" solo per `oggi`, `qualita`, `per_periodo`, `freschezza`, `incertezze`, `regole_non_valutate`. Nessuna tabella serie/x/y in pagina. ✍️ Note personali non compare nella navigazione. Formato del connettore: `<toggle>` e `<mention url/>` (non i legacy `<details>`/`<mention-page>`); nessun attributo `{color}` sui titoli.
- *Grafici incorporati.* `ND.embed_specs(docs)` definisce per pagina gli slot di viste collegate ai database sincronizzati: hub (Voti linea, Assenze ciambella), VOTI (Media generale da 📈 Indicatori, andamento, media per materia), ASSENZE (calendario, ciambella per tipo), VERIFICHE (calendario, elenco da oggi), COMPITI (elenco), MATERIALI (barre per materia), Materia · X (linea filtrata, **solo** materie con voti). Nuova operazione `create_embed` (`notion-create-view` con `parent_page_id`, `show_data_source_title: false`): emessa solo per gli slot mancanti; in quel passo la pagina **non** si riscrive, si riscrive al passo successivo dopo la rilettura.
- *Collocazione.* Una vista collegata si legge come `<database url="BLOCCO" data-source-url="collection://…" inline="true">`; il corpo la ricolloca con il **tag esatto** (stesso blocco). Abbinamento agli slot per data source nell'ordine di lettura; le viste in più (dell'utente) restano tra i figli e non vengono toccate. I figli restano in fondo sotto "### Tutte le pagine", fuori dall'impronta.
- *Medie.* La "Media generale" è il valore del backend (Indicatori). Le altre viste calcolano media aritmetica o conteggio in Notion, e la didascalia lo dichiara; non sono la media pesata M14.
- *Sicurezza.* `check_plan_safe` ammette `create_embed` solo con tipo ammesso, data source letta, genitore hub o pagina generata e argomenti noti. Il ciclo automatico (`notion_autorun.ALLOWED_OPS`) lo ammette alle stesse condizioni.

**Limiti noti (valutati, nessuna correzione possibile o desiderabile).**
- *Countdown nelle pagine.* "giorni mancanti" e "tra N giorni" sono contenuto (regola M17.1): al cambio di giorno le pagine che li mostrano vengono riscritte (circa 6 pagine al giorno con i dati attuali). Neutralizzarli nell'impronta lascerebbe in pagina un conteggio non aggiornato rispetto al giorno corrente; la riscrittura è idempotente nello stesso giorno e verificata. Diverso è il superamento della soglia di freschezza (48 ore senza sincronizzazione): "stale" e l'affidabilità cambiano davvero e riscrivono quasi tutte le pagine; si evita sincronizzando il Registry, non nascondendolo.
- *Link automatico su `cloud.school` nelle proprietà.* Nelle proprietà di testo non esiste una forma "codice" che impedisca il link senza cambiare il valore scritto. Il testo memorizzato è corretto; il link è solo visivo, e la lettura lo normalizza (M17.6). Chiavi, hash e idempotenza non dipendono da Contratto.
- *Gruppi vuoti nei grafici.* Vale anche per le viste r8 raggruppate per Materia o Tipo (select). Le viste `chart` raggruppano per la select X, che contiene le opzioni di tutti i grafici; Notion crea la vista con `hideEmptyGroups: false` e il DSL del connettore (`create_view`/`update_view`) non ha una direttiva per cambiarlo. Si imposta a mano in Notion ("Nascondi gruppi vuoti"); il dato non cambia.

**Mai.**
- Cancellare o archiviare.
- Usare `allow_deleting_content`.
- Scrivere `Note`.
- Riscrivere Nome o Stato utente.
- Sovrascrivere una pagina con lo stesso titolo ma senza impronta: è un `conflitto`.
- Toccare righe con Fonte diversa (es. Manuale).
- Trasformare null in 0.

Le righe scomparse ricevono solo lo Stato "Non più …". `check_plan_safe` verifica tutto questo prima di qualunque scrittura.

**`stato_notion`** (letto dall'agente in sola lettura): `{hub: null | {page_id, impronta, figli}, pagine: {chiave: {page_id, impronta, figli}}, database: {chiave: {database_url, data_source, viste, righe, opzioni?}}}`. Gli helper `parse_page(testo_fetch)` e `rows_from_sql(db, righe)` ne costruiscono le parti. `opzioni` (M17.5) vale solo per i database con select aperte: `{proprietà: [nomi delle opzioni lette]}`, dalla fetch del data source. Se manca, le opzioni sono considerate **non lette** (nessun `ensure_options`, righe bloccate); `{}` o liste vuote significano "lette, nessuna opzione".


## 21. Materiali acquisiti e studio sui materiali (M19 + M20) — `cloud.school.material_store/1`, `cloud.school.study_material/1`
Non cambia M1–M17. Il Registry resta la fonte di verità dei dati scolastici; questo livello aggiunge solo il **testo** dei materiali e i metadati del file.

**Perché.** Il catalogo M15 identifica i materiali, ma l'assistente non ne vedeva il contenuto. Il connettore Google Drive già autorizzato (account della scuola) legge i file Drive allegati a Classroom (`get_file_metadata`, `read_file_content`). Nessuna nuova autorizzazione.

**Acquisizione — `school_material_acquire_v1(riferimento, testo, metadati_drive)`.**
- L'agente legge il file con il connettore e passa qui testo e metadati. Si accetta solo un riferimento del catalogo M15 (perimetro operativo) di tipo `drive_file`, con `stato_materiale = identificato_non_acquisito` e `metadati_drive.id` uguale al `drive_file_id` del catalogo. Formati: quelli che il connettore rende in testo (PDF, documenti Google/Office/OpenDocument, PNG/JPEG).
- Rifiuti (nessuna scrittura): `non_nel_catalogo`, `non_acquisibile`, `non_disponibile`, `id_drive_diverso`, `formato_non_leggibile`, `testo_vuoto`, `testo_troppo_lungo`. Un titolo Drive diverso da quello citato è un avviso (`titolo_diverso`), non un rifiuto.
- Archivio: `~/Library/Application Support/Cloud/school/materials.sqlite3`, separato dal Registry, permessi 0600, **append-only** (UPDATE e DELETE bloccati da trigger). Chiave stabile = id del riferimento M15. Stesso sha256 e stessa `modifiedTime` = `invariato`; altrimenti nuova versione numerata. L'originale resta su Drive (id e link conservati).
- Ogni versione registra: sha256 e lunghezza del testo, titolo, mime, proprietario, data di modifica e dimensione su Drive, titolo citato, uid del padre, versione del catalogo, data di acquisizione, metodo `connettore_google_drive`.

**Stato — `school_materials_status_v1(materia?, obiettivo?, riferimento?)`.** Per ogni riferimento: `acquisito` (ultima versione e numero di versioni), `da_acquisire` (con il `fileId` da leggere) o `non_acquisibile` (link, YouTube, allegati ClasseViva, elementi non disponibili). Con `obiettivo` restano solo i materiali collegati via M5 a quell'uid. Sola lettura: senza archivio non crea nulla.

**Studio — `school_study_material_v1(materia, modalita, riferimento, obiettivo?, risposta_utente?)`.**
- Riusa M13 senza modificarlo (stesse modalità, azioni ammesse e vietate, esiti) sul testo dell'ultima versione acquisita. Il contenuto ha provenienza `materiale_scolastico_acquisito` (non "fornito dall'utente"); `risposta_utente` resta contenuto dell'utente; la conoscenza generale resta etichettata. Il testo completo è restituito, con lo stesso sha256 della versione.
- Materia rispettata: attribuzione `determinata` sulla stessa materia = `confermata`; `possibile` con la materia tra i candidati = ammessa con la frase "attribuzione possibile"; altrimenti `materia_non_corrispondente`.
- Con `obiettivo` il legame è **solo** quello M5 (relazione e stato) oppure `nessuna_associazione_m5`: mai "programma della verifica".
- Frasi aggiunte: `materiale` (titolo, corso, autore, data di acquisizione), `acquisito_non_studiato`, `attribuzione_possibile`, `relazione_obiettivo`. Esiti propri: `materiale_non_acquisito`, `riferimento_non_nel_catalogo`, `materia_non_corrispondente`.

**Limiti.** Il testo è quello restituito dal connettore e trasmesso dall'agente: formule e impaginazione possono essere imperfette (PDF di matematica), e per documenti molto lunghi la trasmissione costa contesto. Il catalogo M15, la dashboard M16 e la proiezione Notion continuano a mostrare `identificato_non_acquisito`: lo stato di acquisizione si legge con `school_materials_status_v1`.
