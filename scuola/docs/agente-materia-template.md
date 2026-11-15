# Agente di materia: template comune

Stato: **livello dati/contesto pronto (`school_context` v2, F3-min)**. **Nessun agente è stato creato.** Meccanismo Claude (Project / Agent / Skill / MCP / file), prompt definitivo, personalità, autonomia e memoria si decidono nella Cloud Academy.

## Principio
Un **solo** codice di contesto per tutte le materie. Cambia soltanto il parametro `subject`. Materie del futuro sistema di agenti (`school_context.AGENT_SUBJECTS`, decisione dell'utente):
`italiano, latino, inglese, storia, filosofia, matematica, fisica, scienze` (8 materie, un contesto e in futuro un agente ciascuna).
**Escluse** (nessun contesto, richiesta rifiutata): `arte, motorie, civica, religione, studio`.

```
L1/L2/L3 (corpus completo)  ->  school_context(subject, ambito)  ->  futuro agente materia  ->  Claude
       sola lettura              JSON cloud.school.context/2          consumatore/interfaccia
```

## Interfaccia per l'agente (pronta)
- **Contesto di avvio:** `school_brief(subject)` (tool MCP) / `school_agent.brief(...)`. Compatto (4–10 mila caratteri per materia).
- **Dettagli su richiesta:** `school_tasks`, `school_task`, `school_announcements`, `school_materials`, sempre con lo stesso `subject`.
- **Materia:** una delle 8 chiavi (`school_subjects`); le escluse vengono rifiutate. Ogni agente usa sempre e solo la propria chiave.
- **Non accessibile per default:** storico (anni prima del 2025-26), corsi non confermati (solo nome + conteggi nel brief), corsi esclusi o del biennio, dati di altre materie, interno di L3 (`derivato`), hash completi (solo con `school_task`), registro completo, vista diagnostica `school_context`.
- **Nuova conferma manuale:** l'utente (o Claude su sua richiesta esplicita in chat) aggiunge il `course_id` alla lista `subjects.<materia>.classroom_course_ids` di `school_manual.json`; per un corso condiviso lo aggiunge sotto ogni materia. Prima si guarda `cls_course_subjects` (categoria ed evidenze). Nessun tool scrive il file.
- **Aggiornamento quando Classroom cambia:** sync Classroom → registro (`cls_registry_sync`, un tipo alla volta, `expect_create` dal dry-run). Il brief si ricalcola a ogni chiamata dal registro: nessuna cache da invalidare. Un corso nuovo compare come proposta finché non viene confermato.
- **Un livello sopra l'infrastruttura:** gli 8 agenti differiscono solo per la chiave della materia e per le istruzioni; la logica dati è unica (nessuna copia della pipeline).

## Procedura per costruire un agente di materia
1. Scegliere il meccanismo Claude (Project, Agent, Skill + MCP…): decisione della Cloud Academy.
2. Dare all'agente accesso in sola lettura ai tool `school_brief`, `school_tasks`, `school_task`, `school_announcements`, `school_materials` (e solo a quelli).
3. Istruzioni comuni (template sotto) + chiave della materia fissa; all'avvio chiamare `school_brief(<materia>)`.
4. Verifica dell'agente: domande di prova per materia (es. "cosa devo consegnare?", "quando è la prossima verifica?"), controllo che citi i `ref`, che non usi dati di altre materie, che dichiari ciò che manca.
5. Ripetere per le 8 materie cambiando solo la chiave.

## Contesto completo (`school_context`): solo diagnostica
Documento deterministico (stesso `now` → stesso JSON), un oggetto per **una** materia, con: agenda ClasseViva (verifiche, compiti, eventi), `possibili_da_verificare` (record con materia incerta), voti per `materia_chiave`, `classroom.confermato` (corsi confermati a mano: compiti L3 con la consegna collegata, annunci, materiali, argomenti collegati), `classroom_proposto` (**sezione separata**, proposte non confermate), `ambito` (solo perimetro operativo 3ª/4ª: lo storico non è esposto agli agenti), conteggi `totale/inclusi/omessi/troncato`, citazioni (`uid` + `registry_version` + `content_hash`), sezione `da_verificare`, `regole_per_gli_agenti`. Elenco delle materie: tool `school_subjects`.

Lo apre il registro come **copia in sola lettura**: l'agente non può modificare RAW, normalizzato, registro, Notion né le fonti.

## Livello manuale (dichiarazioni dell'utente)
File `~/Library/Application Support/Cloud/school/manual/school_manual.json` (fuori da iCloud, **mai creato dal codice**, mai sovrascritto dalle sync). Oggi **non esiste**: nessuna conferma. Struttura minimale prevista:

```json
{"schema": "cloud.school.manual/1",
 "subjects": {"italiano": {"classroom_course_ids": []}, "latino": {"classroom_course_ids": []},
              "inglese": {"classroom_course_ids": []}, "storia": {"classroom_course_ids": []},
              "filosofia": {"classroom_course_ids": []}, "matematica": {"classroom_course_ids": []},
              "fisica": {"classroom_course_ids": []}, "scienze": {"classroom_course_ids": []}}}
```
Solo le 8 materie sono accettate (le altre vengono ignorate e segnalate); campi extra già supportati (es. `nota`) restano facoltativi. Un corso entra nel contesto di una materia **solo** se il suo `course_id` è nella lista di quella materia; lo stesso `course_id` sotto più materie = corso condiviso. Le proposte automatiche (`cls_subjects`, tool `cls_course_subjects`) non vengono mai scritte qui. Tutto ciò che sta nel file è una **dichiarazione** dell'utente.

## Template del prompt (BOZZA, non definitiva: da decidere nella Cloud Academy)
> Sei l'assistente di studio per la materia **{subject}** dello studente.
> Hai in sola lettura il contesto `school_context({subject})`.
> Regole: (1) non inventare voti, date, docenti, argomenti; se un dato manca dillo; (2) cita ogni dato con uid + versione + hash; (3) tratta il livello manuale come dichiarazione; (4) ClasseViva e Classroom sono fonti separate; (5) non puoi modificare registro, Notion o fonti; (6) per azioni che modificano qualcosa, proponi e aspetta conferma.
> Compiti: preparare le prossime verifiche, riassumere cosa è stato assegnato, individuare tendenze nei voti, proporre un piano di studio.

## Cosa gli agenti potranno sapere (quando i dati esistono)
Voti, verifiche, compiti, assenze, calendario, avvisi, Classroom, materiali, note, file, storico di studio, errori ricorrenti, fatti dichiarati sui docenti, in seguito trascrizioni. **Oggi** sono indicizzati: registro ClasseViva e (dopo il consenso) Classroom. Materiali/appunti/registrazioni: non ancora.

## Rischi
- Materia dedotta con confidenza bassa: l'agente deve mostrare `candidati` e non scegliere.
- Contesto grande = più token: gli agenti usano `school_brief` (compatto), non `school_context`; misura ufficiale dei token ancora da fare (Cloud Academy).

## Da fare
- Definire i livelli di autonomia per materia (solo lettura / propone / esegue): decisione dell'utente.
- Indicizzare materiali e appunti (iCloud) in un livello separato.
