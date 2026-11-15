# Cloud Finance — istruzioni condivise (CUORE)

Registro centrale di coordinamento: `~/Developer/cuore/docs/CUORE.md` (fonte unica per stato, decisioni e prossima azione CUORE; qui solo ciò che vincola questo repository).

## Requisito CUORE: due dashboard distinte
- CUORE è un'unica app (Mac, iPad, iPhone) con **due dashboard distinte**: **CUORE School** (motore Cloud School, cartella `scuola/` di questo repository) e **CUORE Money**, finanze e investimenti (questo motore). Non una dashboard generica condivisa: la schermata iniziale serve solo a scegliere, senza dati aggregati.
- **CUORE Money** ha gerarchia, metriche, grafici e flussi propri: quadro, saldi, entrate/uscite, movimenti, trasferimenti, riconciliazioni, budget, patrimonio, investimenti. Prototipo con dati dimostrativi: `~/Developer/cuore/app/index.html`.
- Mostrare solo ciò che il motore implementa: budget = gruppi di budget delle categorie, obiettivi, acquisti pianificati (nessun limite di spesa per categoria); investimenti = sezione predisposta senza valori finché posizioni, quotazioni e rendimenti non esistono qui. Se il motore aggiunge queste funzioni, aggiornare il registro centrale.
- In comune con School solo **design system** e **navigazione**. Motori, database, dati e regole restano separati: nessun database comune, nessuna metrica che mescoli scuola e denaro.
- CUORE Money legge **solo** il contratto di consultazione in sola lettura (`runQuery`, docs/consultazione.md); rispetta le sue regole (saldi solo osservati, unità minime, valute mai sommate, descrizioni mascherate).

## Protezioni
- Solo dati sintetici nel repository; i database reali vivono fuori dal repository e fuori da iCloud.
- Nessun commit, push, deploy, import, scrittura su database reali, risorsa remota, nuova automazione, spesa o API AI senza approvazione esplicita del titolare. Niente `git add -A`.
- Mai leggere, chiedere o memorizzare credenziali, token o file `.env` reali.
