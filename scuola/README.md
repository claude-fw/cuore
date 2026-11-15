# CUORE School — modulo Scuola

Legge **ClasseViva** e **Google Classroom** in sola lettura, conserva tutto in un Registry SQLite locale e prepara il
feed School per l'app (`cuore.school.feed/1`) e gli strumenti per gli assistenti (server MCP `classeviva`).
Motore, dati e credenziali separati da Finance. Nessuna scrittura sulle fonti; Notion è uscito da CUORE il 24/10.

## Struttura
| Percorso | Ruolo |
|---|---|
| `mcp/classeviva/` | motore School: client delle fonti (`cvv_*`, `cls_*`), Registry (`registry.py`, `cvv_registry.py`, `cls_registry.py`), sincronizzazione (`cvv_sync.py`), contesto e dashboard (`school_context*.py`, `school_dashboard.py`, `school_status.py`, …), server MCP (`server.py`), job pianificato (`school_job.py`) |
| `mcp/classeviva/launchd/` | modello del LaunchAgent (`cloud.scuola.job.plist.template`) e script di gestione (`school_job_launchd.sh`) |
| `mcp/classeviva/tests/` | test del motore (fixture sintetiche) |
| `mcp/_common/` | parti comuni a più moduli: server MCP minimo su stdio (`cloud_mcp.py`), orari del calendario e giri dal log (`cuore_automation.py`) |
| `mcp/classroom_probe/` | sonda manuale dell'API Classroom e preparazione del Portachiavi (`setup_keychain.sh`) |
| `docs/` | documenti del modulo (sotto) |

## Dati (fuori dal repository e fuori da iCloud)
- Registry: `~/Library/Application Support/Cloud/school/registry.sqlite3`; stato del job nella cartella `job/` accanto.
- Log del job: `~/Library/Logs/Cloud/school-job.jsonl` (eventi), `school-job.err.log` (solo errori imprevisti).
- Credenziali: Portachiavi di macOS. Mai nel repository, nei log o nei test.

## Job pianificato
- LaunchAgent `cloud.scuola.job` (`~/Library/LaunchAgents/cloud.scuola.job.plist`): 06:45, 14:30, 20:00 e all'accesso,
  `--commit --notify`, priorità bassa. Punta al **percorso assoluto** di questo repository: non spostarlo.
- Codici di uscita: 0 OK, 1 PARZIALE, 2 FALLITO, 3 AUTORIZZAZIONE, 4 OCCUPATO.
- Con il Mac in stop a batteria il giro avanza solo nei brevi risvegli in background e può finire PARZIALE
  (`scadenza_tempo_reale`). 
- Invio del feed School al cloud (E3, `mcp/classeviva/school_cloud_push.py`): **pronto in locale, spento**; si attiva
  dopo l'account Cloudflare. Solo dopo un giro `--commit` e solo se esiste `cuore-cloud.json` nella cartella `job/`
  (`{"url": "https://…/api/school", "token_file": "/percorso/assoluto"}`, permessi 600; file del token con
  `client_id`/`client_secret` del token di servizio `mac-school`, permessi 600, fuori dal repository e da iCloud).
  Senza configurazione non fa nulla; un errore finisce nel log (`invio_cloud`) e non cambia mai esito o codice d'uscita.
  Invia solo il feed minimizzato (campi in `../cloud/src/school-feed-campi.json`: niente pagine non mostrate, testi per
  gli agenti, nome del Registry, elenchi di uid della provenienza, tipi d'eccezione). Prova senza invio:
  `/usr/bin/python3 mcp/classeviva/school_cloud_push.py prova`.
- Recupero all'uso dei giri mancati: logica e test pronti, non collegata ([docs/recupero-all-uso.md](docs/recupero-all-uso.md)).
- Installazione, sospensione e comportamento: [docs/automazione-scuola.md](docs/automazione-scuola.md).
  Modificare orari o LaunchAgent richiede l'autorizzazione del titolare.

## Server MCP `classeviva`
`mcp/classeviva/server.py` (stdio), registrato in Claude Desktop e in Claude Code con il percorso assoluto. Solo
lettura; ClasseViva e Classroom restano fonti separate.

## Documenti (`docs/`)
| File | Contenuto |
|---|---|
| `architettura-scuola.md` | strati L1–L3 e decisioni (03/10; le parti su Notion sono storiche) |
| `automazione-scuola.md` | job e LaunchAgent |
| `registro-scuola.md`, `DATA_CONTRACT.md` | Registry e contratto dei dati |
| `classeviva.md`, `classroom-probe.md` | fonti |
| `recupero-all-uso.md` | recupero dei giri mancati quando il Mac torna in uso (E1, non collegato) |
| `agente-materia-template.md`, `formazione-agenti-minima.md` | agenti di materia |

## Test
```
cd scuola/mcp/classeviva && /usr/bin/python3 -m unittest discover -s tests
cd scuola/mcp/classroom_probe && /usr/bin/python3 -m unittest test_classroom_probe
```
Usare `/usr/bin/python3` (lo stesso interprete del LaunchAgent). I test lavorano su fixture sintetiche e cartelle
temporanee.
