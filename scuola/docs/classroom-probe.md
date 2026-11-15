# Probe OAuth Google Classroom (test minimo, sola lettura)

Stato: **test reale eseguito con successo** (account `studente@scuola.example`, scope unico `classroom.courses.readonly`, token revocato a fine esecuzione).
Un primo tentativo era fallito con `invalid_client`: causa = Client Secret errato nel Keychain; corretto il secret, il probe è passato.
Conseguenze verificate: il dominio scolastico **non blocca** l'app OAuth per lo scope dei corsi; client id/secret in Keychain (`cloud.classroom-probe`) sono quelli riusati da `cls_auth.py`.
Non provato: gli altri 5 scope (coursework, submissions, materials, announcements, topics), il refresh token (il probe non lo chiede), la durata dei token in modalità Testing.

## Obiettivo
Verificare una sola cosa: l'account scolastico `studente@scuola.example` può autorizzare l'accesso
**read-only** a Google Classroom e, se sì, leggere l'**elenco dei corsi**? Nient'altro.

## Cosa fa / cosa non fa
| Fa | Non fa |
|---|---|
| Un solo scope: `https://www.googleapis.com/auth/classroom.courses.readonly` | Nessun altro scope Classroom, nessuno scope Drive |
| OAuth "app desktop" + PKCE S256 su `127.0.0.1` (porta casuale) + controllo `state` | Nessun refresh token (niente `access_type=offline`) |
| Una sola chiamata API: `GET /v1/courses` (paginata) | Nessuna scrittura su Classroom (solo GET) |
| Stampa solo `id`, `nome`, `sezione`, `stato` nel Terminale | Nessun salvataggio di dati scolastici su disco / Git |
| Revoca il token a fine esecuzione | Non tocca registro SQLite, ClasseViva, Notion, scheduler |

## Credenziali
* **Client ID e Client Secret** → Keychain macOS, servizio `cloud.classroom-probe` (account `client_id`, `client_secret`), scritti da `setup_keychain.sh` (il secret è chiesto da `security` con input nascosto: non passa da argv né da file).
* **Token** → solo in memoria del processo, mai su disco, revocato alla fine.
* `.gitignore` esclude già `client_secret*.json`, `*token*`, `.env*`: se scarichi il JSON del client da Google Cloud, **non metterlo** nel repo né in iCloud (eliminalo dopo aver copiato id e secret nel Keychain).

## File
`mcp/classroom_probe/classroom_probe.py` (probe, stdlib) · `setup_keychain.sh` · `test_classroom_probe.py` (20 test contro un "Google finto" locale).

## Test (nessuna rete reale)
`cd mcp/classroom_probe && python3 -W ignore -m unittest test_classroom_probe`
Coprono: successo con paginazione, elenco vuoto, consenso negato, `admin_policy_enforced`, `state` errato, errore del token endpoint, API 403 non abilitata, scope extra concesso, timeout, revoca fallita, nessun segreto in output, nessun file scritto, solo GET sull'API, URL di autorizzazione minimo, vettore PKCE RFC 7636, singolo scope nel sorgente.
Verifiche di mutazione: PKCE `plain`, controllo `state` disattivato, controllo scope disattivato, `access_type` aggiunto → i test falliscono come previsto.

## Esecuzione reale (a cura dell'utente: serve consenso)
1. Google Cloud Console: progetto `cloud-classroom-probe`, API *Google Classroom* abilitata, schermata consenso *Esterno* in *Testing* con l'account scolastico come utente di test, client OAuth tipo **App desktop**.
2. `cd ~/Developer/cuore/scuola/mcp/classroom_probe && ./setup_keychain.sh`
3. `python3 classroom_probe.py --check`
4. `python3 classroom_probe.py --account studente@scuola.example`
5. Esiti possibili e loro significato → vedi `HINTS` nel sorgente; l'interpretazione è sempre marcata "probabile".

## Rollback
Revoca: https://myaccount.google.com/permissions · Keychain: `security delete-generic-password -s cloud.classroom-probe -a client_id` (e `-a client_secret`) · progetto Cloud eliminabile dalla Console · codice: `git revert` del commit.
