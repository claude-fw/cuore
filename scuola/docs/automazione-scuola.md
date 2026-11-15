# Automazione della scuola (job e LaunchAgent)

Il job `school_job.py` legge ClasseViva e Google Classroom, scrive il Registry locale (SQLite) e, se configurato, invia
il feed School al cloud. Gira **solo sul Mac**: le credenziali della scuola restano nel Portachiavi e non vanno mai online.

## Quando parte
Il LaunchAgent `cloud.scuola.job` non ha orari fissi: lancia `school_job.py check` ogni 5 minuti e al risveglio del Mac.
`check` fa un giro completo (`--commit --notify`) solo se:
- dal telefono è arrivata una richiesta («Sincronizza tutto» nell'app, letta da `GET /api/school/richiesta`), oppure
- l'ultimo giro riuscito ha più di 3 ore **e** il Mac è in uso (input dell'utente negli ultimi 300 secondi, da `ioreg`).
Dopo un tentativo fallito aspetta un'ora. Nei brevi risvegli a coperchio chiuso non parte: un giro lì avanzerebbe a
scatti per ore. Senza nulla da fare esce subito, senza rete e senza log.

## Installare, sospendere, togliere
```
mcp/classeviva/launchd/school_job_launchd.sh render            # anteprima del plist con i percorsi reali
mcp/classeviva/launchd/school_job_launchd.sh install --conferma # copia in ~/Library/LaunchAgents e carica
mcp/classeviva/launchd/school_job_launchd.sh status            # caricato? ultimo esito
launchctl bootout gui/$(id -u)/cloud.scuola.job                 # sospensione senza disinstallare
mcp/classeviva/launchd/school_job_launchd.sh uninstall          # scarica e mette il plist nel Cestino
```

## Esiti e diagnostica
- Log JSON a righe con rotazione: `~/Library/Logs/Cloud/school-job.jsonl` (solo orari, esiti, conteggi); errori
  imprevisti in `school-job.err.log`; riepilogo dell'ultimo giro in `last_summary.json` nella cartella di stato.
- Esiti: `OK`, `PARZIALE` (una fonte non letta del tutto), `FALLITO`, `OCCUPATO` (un altro giro in corso: lock).
- Autorizzazione di Classroom scaduta: `python3 cls_auth.py authorize --account <account scolastico>`.
- Ogni tipo di dato è una transazione completa: un giro interrotto non lascia dati a metà.
