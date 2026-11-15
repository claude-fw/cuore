# Recupero all'uso del job scuola (E1, 01/11/2026)

Stato: **implementato, testato e collegato** (02/11, LaunchAgent separato; attivazione del titolare). Codice: `mcp/classeviva/school_recovery.py`; test:
`mcp/classeviva/tests/test_school_recovery.py` (13, dati e file sintetici). LaunchAgent, plist, pianificazione, job e
Registry **non toccati**.

## Problema
Dal 25/10 15 giri su 21 non sono riusciti: con il Mac in stop a batteria il giro avanza solo nei risvegli in background
e scade (`scadenza_tempo_reale`); con il Mac ibernato gli orari si perdono.

## Cosa decide (`decide`, funzione pura)
Un recupero si fa solo se: nessun giro è in corso; l'ultimo orario passato da almeno 20 minuti non è coperto da un giro
OK (anche in ritardo); l'ultimo giro non è fallito per l'autorizzazione (un altro giro non la ripara); il prossimo orario
non è entro 30 minuti; il Mac è **in uso** (inattività dell'utente ≤ 300 s, letta da `HIDIdleTime` di `ioreg`: nei
risvegli in background non c'è input); per quell'orario non c'è già stato un recupero; l'ultimo recupero è di almeno 30
minuti fa. Dopo un lungo periodo spento gli orari scoperti (nelle 48 h) si **accorpano in un solo giro**. Ogni «no» ha un
motivo fisso: `giro_in_corso`, `ultimo_orario_coperto`, `serve_intervento`, `prossimo_orario_vicino`, `mac_non_in_uso`,
`gia_recuperato`, `recupero_recente`.

`record` aggiorna la memoria dei recuperi (ultimi 20) per evitare doppie esecuzioni; dove salvarla lo decide chi collega
il modulo.

## Riga di comando (solo diagnostica)
`/usr/bin/python3 scuola/mcp/classeviva/school_recovery.py [--log …] [--plist …] [--stato …] [--inattivita-s N]` stampa
la decisione per lo stato attuale, legge log e plist in sola lettura e non avvia né scrive nulla.

## Collegamento (decisione C-04 S1, approvata il 02/11)
- LaunchAgent **separato** `~/Library/LaunchAgents/cloud.scuola.recupero.plist`: ogni 600 s (`StartInterval`, non
  all'avvio) esegue `school_recovery.py --esegui --stato ~/Library/Logs/Cloud/school-recovery-state.json`; errori in
  `~/Library/Logs/Cloud/school-recovery.err.log`. Il job originale `cloud.scuola.job` e il suo plist **non cambiano**.
- `--esegui`: se la decisione è «sì» salva prima la memoria (al massimo un recupero per orario), poi esegue
  `school_job.py run --commit --notify` al suo posto; il lock `school-job.lock` impedisce due giri insieme.
- Attivazione (la lancia il titolare): `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/cloud.scuola.recupero.plist`.
- Ritorno indietro: `launchctl bootout gui/$(id -u)/cloud.scuola.recupero`, poi il plist nel Cestino (`trash`).
- Verifica: dopo un giro saltato, al primo uso del Mac compare nel log `school-job.jsonl` un giro in più e la memoria
  dei recuperi ha una riga.
