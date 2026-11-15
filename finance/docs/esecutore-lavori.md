# Esecutore di lavori idempotente (B3, 01/11/2026)

Stato: **implementato e testato, non collegato** a nessun lavoro reale. Codice: `src/jobs/executor.ts`; test:
`test/job-executor.test.ts` (9, orologio finto e deposito in memoria).

## A cosa serve
Eseguire i lavori automatici di CUORE (sincronizzazione bancaria, classificazione, istantanea del feed, backup) in modo
affidabile ovunque giri il codice: Node sul Mac, Durable Object, consumatore di una coda (architettura §2.2). Nessuna
dipendenza dal runtime: il deposito degli stati è un'interfaccia.

## Contratto
- **Chiave** univoca scelta dal chiamante (tipo + oggetto + finestra, es. `banca:conto:2026-03-01T07`); una chiave non
  cambia tipo.
- **Stati:** `queued` → `running` → `succeeded` | `retry_wait` → … | `failed`.
- **Idempotenza:** un lavoro riuscito non si riesegue; le chiamate successive restituiscono il risultato memorizzato.
- **Doppia elaborazione:** chi esegue prende un lease (proprietario + scadenza) con un compare-and-set sulla versione
  del record; gli altri ricevono `busy`.
- **Interruzioni:** lease scaduto = tentativo interrotto (conta); il primo esecutore successivo riprende. Se l'originale
  torna, la sua chiusura fallisce (`lease_lost`) e l'esito resta quello di chi ha ripreso: gli effetti del gestore
  devono essere idempotenti (nel motore Finance lo sono già: id, impronte, `write_seq`).
- **Retry:** ogni errore non `PermanentError` è temporaneo; attese deterministiche (predefinite 30 s, 2, 8, 32 min…,
  massimo 6 h); tentativi massimi predefiniti 4; poi `failed`.
- **Riapertura** di un lavoro fallito solo a mano, con motivo (`reopenFailedJob`).
- **Storico** degli eventi nel record (ultimi 50): creato, avviato, errore temporaneo, ripresa, riuscito, fallito,
  riaperto. Testi d'errore troncati a 300 caratteri: i gestori non devono metterci dati personali.

## Limiti
- Il lease non si rinnova: va scelto più lungo della durata massima del gestore.
- Disponibile solo il deposito in memoria. Il deposito persistente (SQLite nel Durable Object, con il compare-and-set
  come `UPDATE … WHERE version = ?` nello stesso batch) si scrive con C3, insieme alla coda e ai Cron.
- Nessuna attesa attiva: chi chiama decide quando ripassare (allarme del DO, messaggio ritardato della coda, Cron).
