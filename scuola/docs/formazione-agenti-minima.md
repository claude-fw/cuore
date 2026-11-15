# Formazione minima: dagli strumenti scolastici agli agenti di materia

Tutto qui si riferisce a ciò che esiste davvero nel progetto (stato 2026-10-03). Dettagli tecnici: `docs/architettura-scuola.md`; procedura: `docs/agente-materia-template.md`.

## 1. Che cos'è un agente (e perché non è una chat qualsiasi)
Una **chat** risponde a ciò che le scrivi, con ciò che le incolli. Un **agente** ha in più tre cose stabili:
- **istruzioni** fisse (chi è, cosa può fare, cosa non deve fare);
- un **contesto** che riceve all'avvio (qui: `school_brief(<materia>)`);
- **strumenti** che può usare da solo per recuperare altro (qui: `school_tasks`, `school_task`, `school_announcements`, `school_materials`).
L'agente di Matematica è quindi: istruzioni comuni + chiave `matematica` + quei tool. Gli 8 agenti differiscono **solo** per la chiave della materia.

## 2. Prompt, contesto, strumenti: tre cose diverse
| | Cos'è | Nel progetto |
|---|---|---|
| **Prompt / istruzioni** | regole di comportamento, scritte una volta | template in `agente-materia-template.md` + `regole` nel brief |
| **Contesto** | i dati che l'agente ha "davanti" all'inizio | `school_brief`: compiti, annunci, materiali, verifiche della materia, in forma compatta |
| **Strumenti (tool)** | azioni che l'agente può chiamare quando gli serve qualcosa | i 4 tool di recupero, tutti in sola lettura |
Più contesto ≠ agente migliore: il contesto costa a ogni messaggio. Per questo il brief è piccolo e il resto si recupera solo quando serve (**retrieval on demand**).

## 3. MCP e tool di sola lettura
**MCP** è il "connettore" con cui Claude usa programmi esterni: il nostro server `classeviva` (`mcp/classeviva/server.py`) espone i tool. Ogni tool dichiara cosa accetta (es. `subject`, `uid`) e restituisce JSON. Tutti i tool per gli agenti sono **di sola lettura**: nessuno può modificare il registro, il file delle conferme, Notion o le fonti. È una scelta di sicurezza: un agente che sbaglia non può fare danni ai dati.

## 4. Registry ≠ contesto
- Il **Registry** (`registry.sqlite3`) è la memoria completa e immutabile: tutto ciò che Classroom e ClasseViva hanno restituito, anche gli anni passati. È la **fonte di verità**.
- Il **contesto** è una **selezione** calcolata ogni volta dal Registry: solo la materia, solo i corsi confermati, solo il perimetro operativo (3ª e 4ª).
- **Notion** è una vetrina (dashboard): non è mai la fonte di verità.

## 5. Isolamento tra agenti
Ogni chiamata riguarda **una** materia. I dati arrivano solo dai corsi che **tu** hai confermato per quella materia in `school_manual.json`. Se l'agente di Matematica chiede un compito di Italiano, il tool lo rifiuta. Un corso condiviso (es. Mate & Fisica) appare a entrambe le materie con `condiviso_con`: l'agente non deve attribuirlo arbitrariamente a una sola.
Limite da conoscere: l'isolamento è garantito sui **dati**; che l'agente di Matematica chieda sempre `subject="matematica"` lo stabiliscono le sue istruzioni (e, nella Cloud Academy, la configurazione del meccanismo scelto).

## 6. Proposte e conferme
Il sistema **propone** a quale materia appartiene un corso (`cls_course_subjects`, con le evidenze), ma solo **tu** confermi, scrivendo il `course_id` in `school_manual.json`. Una proposta non è mai un fatto: nel brief i corsi proposti compaiono solo come nome e numeri.

## 7. Regole che ogni agente deve rispettare (già nel brief)
Non inventare (se un dato manca lo dice); citare i dati con il loro `ref` (`uid@versione`); i punti Classroom non sono voti; "senza scadenza" non vuol dire "da fare", "scaduto non consegnato" non vuol dire "da recuperare"; i corsi proposti non sono confermati.

## 8. Come si verifica un agente
1. Domande di prova per materia con risposta nota (es. "quali compiti ho in Fisica?", "quando è la prossima verifica di Italiano?").
2. Controllare che la risposta citi i `ref` e che i `ref` esistano (`school_task`).
3. Domande trabocchetto: dati di un'altra materia, anni passati, corsi non confermati → l'agente deve dire che non li ha.
4. Domande senza risposta nei dati → l'agente deve dichiararlo, non inventare.

## 9. Dati e permessi
- Dati personali e scolastici restano sul Mac (`~/Library/Application Support/Cloud/school/`), fuori da iCloud e dal repository.
- Credenziali solo nel Keychain; nessun agente le vede.
- Gli agenti leggono; le scritture (sync, conferme, Notion) restano decisioni tue, fatte fuori dagli agenti.
