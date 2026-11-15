"""Registro unico degli eventi di CUORE, prima versione: vista CALCOLATA in sola lettura, contratto `cuore.events/1`.

Nessun nuovo database o file persistente: gli eventi si ricostruiscono a ogni lettura dalle fonti che esistono gia',
senza modificarle, e una seconda lettura a parita' di fonti e di `now` da' lo stesso risultato (ordine e identificativi
deterministici).

Fonti (adattatori):
  School  - log JSONL del job (`school-job.jsonl` e ruotati, letti da cuore_automation): giri e categorie di anomalia;
          - calendario del LaunchAgent + log (cuore_automation.history): orari mancati o non verificabili;
          - `state.json` del job: problemi aperti e ultima notifica inviata per problema;
          - Registry, tabella `runs` (copia in memoria, immutable): esecuzioni per fonte e tipo.
  Finance - solo METADATI gia' esposti dalla consultazione v1 (`data_status` nel feed Money): ultima importazione riuscita
            e anomalie per conto. Nessun movimento, importo o estratto.
          - stato della sincronizzazione bancaria (`cuore.bank.sync_status/1`, scritto da finance/scripts/bank-sync.ts):
            ultimo tentativo per conto ed esito, consenso e sua scadenza. Solo conti, date, esiti e conteggi.
  Core    - registro delle azioni (`cuore_actions.py`, facoltativo, solo se indicato): stato di ogni azione proposta,
            decisa o eseguita. Nessun motivo testuale. Un registro con la catena interrotta non e' una fonte: dichiarato.

Ogni evento: id stabile (hash di tipo + chiave naturale della fonte), area, tipo, natura, quando (istante dell'evento,
UTC) e acquisito (quando la fonte lo ha registrato, se distinto; altrimenti null), esito, gravita' (info|avviso|errore),
fonte {origine, riferimento}, qualita' (valido|incompleto|timestamp_incoerente) e un dettaglio a vocabolario fisso.
Natura: fatto_osservato (registrato dalla fonte) | stato_corrente (vale adesso) | inferenza (dedotto, es. orario senza
avvio) | anomalia | non_verificato. Un campo non disponibile e' null: mai un valore inventato. Nessun testo libero,
errore, uid scolastico o chiave completa esce da qui.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cuore_school as CS  # noqa: E402   (percorsi reali e sys.path del motore)
import cuore_automation as AUTO  # noqa: E402
import registry as R  # noqa: E402

SCHEMA = "cuore.events/1"
DAYS = 7                       # finestra predefinita
FUTURE_TOLERANCE = dt.timedelta(minutes=5)
NATURE = ("fatto_osservato", "stato_corrente", "inferenza", "anomalia", "non_verificato")
SEVERITY = ("info", "avviso", "errore")
RUN_SEVERITY = {"ok": "info", "occupato": "info", "in_corso": "info", "parziale": "avviso", "interrotto": "avviso",
                "incompleto": "avviso", "sconosciuto": "avviso", "fallito": "errore", "autorizzazione": "errore"}
PROBLEM_SEVERITY = {"autorizzazione": "errore", "keychain": "errore", "fallito": "errore"}
QUALITY = ("valido", "incompleto", "timestamp_incoerente")
SOURCE_STATES = ("ok", "con_righe_illeggibili", "assente", "illeggibile", "non_configurata", "non_collegata")
_PROBLEM = ("tipo_problema", "fonte", "giri")
# tipo -> (area, nature ammesse, chiavi ammesse nel dettaglio, forma del riferimento). Tutto cio' che non e' qui e'
# rifiutato dal validatore: il dettaglio e' a vocabolario chiuso (privacy), mai testo libero della fonte.
_ISO = r"\d{4}-\d\d-\d\dT[0-9:.]+\+00:00"
TYPES = {
    "giro_job": ("school", ("fatto_osservato", "inferenza"), ("modo", "exit_code", "durata_lavoro_s", "durata_reale_s",
                                                             "fonti", "tentativi", "tipi_oltre_soglia"), r"avvio " + _ISO),
    "anomalia_giro": ("school", ("anomalia",), ("categoria",), r"fine del giro " + _ISO),
    "orario_mancato": ("school", ("inferenza",), ("causa",), r"orario " + _ISO),
    "orario_non_verificabile": ("school", ("non_verificato",), ("motivo",), r"orario " + _ISO),
    "problema_aperto": ("school", ("stato_corrente",), _PROBLEM, r"state\.json#problemi/[0-9a-f]{8}"),
    "notifica_inviata": ("school", ("fatto_osservato",), _PROBLEM, r"state\.json#problemi/[0-9a-f]{8}"),
    "chiusura_in_attesa": ("school", ("stato_corrente",), ("tipo_problema", "fonte"),
                           r"state\.json#risolti_in_attesa/[0-9a-f]{8}"),
    "run_registro": ("school", ("fatto_osservato",), ("fonte", "tipo_dato", "modo", "durata_ms", "letti", "conteggi",
                                                     "legacy"), r"run_id [\w.-]{1,80}"),
    "importazione_riuscita": ("finance", ("fatto_osservato",), ("conto",), r"conto [\w.-]{1,60}"),
    "anomalia_dati": ("finance", ("anomalia",), ("conto", "codice", "periodo", "data"), r"conto [\w.-]{1,60}|generale"),
    "sincronizzazione_bancaria": ("finance", ("fatto_osservato",), ("conto", "banca", "errore", "database", "nuovi",
                                                                     "aggiornati", "riprova_dopo_ore"), r"banca-stato\.json#[\w.-]{1,60}"),
    "consenso_bancario": ("finance", ("stato_corrente",), ("conto", "banca", "giorni_rimasti", "scadenza"),
                          r"banca-stato\.json#[\w.-]{1,60}"),
    "azione": ("core", ("fatto_osservato",), ("tipo_azione", "area_azione", "passi", "richiesta_da"),
               r"azioni\.jsonl#act-[0-9a-f]{12}"),
}
ACTION_SEVERITY = {"richiesta": "avviso", "fallita": "errore"}
BANK_STATUS_SCHEMA = "cuore.bank.sync_status/1"
BANK_STATUS_DEFAULT = os.path.join("~", "Library", "Application Support", "CUORE", "banca-stato.json")
CONSENT_WARN_DAYS = 14                                       # come finance/src/sync/connections.ts
SYNC_SEVERITY = {"importata": "info", "nessun_dato_nuovo": "info", "interrotta": "avviso", "rifiutata": "errore",
                 "non_eseguita": "avviso"}
CONSENT_SEVERITY = {"valido": "info", "in_scadenza": "avviso", "scaduto": "errore", "assente": "avviso"}      # richiesta = decisione del titolare in sospeso
_DATE = re.compile(r"^\d{4}-\d\d-\d\d$")
_PERIOD = re.compile(r"^\d{4}-\d\d-\d\d\.\.\d{4}-\d\d-\d\d$")
# valori testuali del dettaglio: brevi, una riga, senza indirizzi, URL o numeri lunghi (uid, IBAN, numeri di conto)
_UNSAFE = re.compile(r"[\n\r@]|://|\d{5,}")
UTC = dt.timezone.utc


def _h(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:16]


def _utc(v):
    """Istante con fuso -> datetime UTC; altrimenti None (mai un fuso inventato)."""
    if isinstance(v, dt.datetime):
        return v.astimezone(UTC) if v.tzinfo else None
    if not isinstance(v, str):
        return None
    try:
        t = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.astimezone(UTC) if t.tzinfo else None


def _iso(t):
    return t.isoformat() if t else None


def event(area, tipo, natura, key, quando, origine, riferimento, esito=None, gravita="info", acquisito=None,
          qualita="valido", dettaglio=None):
    assert natura in NATURE and gravita in SEVERITY
    return {"id": "evt:%s" % _h(area, tipo, key), "area": area, "tipo": tipo, "natura": natura, "quando": _iso(quando),
            "acquisito": _iso(acquisito), "esito": esito, "gravita": gravita,
            "fonte": {"origine": origine, "riferimento": riferimento}, "qualita": qualita, "dettaglio": dettaglio or {}}


class _Diag:
    def __init__(self):
        self.fonti, self.non_validi, self.duplicati, self.timestamp_incoerenti = {}, 0, 0, 0

    def source(self, name, stato, letti=0, scartati=0, nota=None):
        d = {"stato": stato, "letti": letti, "scartati": scartati}
        if nota:
            d["nota"] = nota
        self.fonti[name] = d


def _check_time(t, now, diag):
    """'valido' | 'timestamp_incoerente' (nel futuro oltre la tolleranza). Il conteggio in diagnostica si fa dopo la
    deduplica (merge): un evento letto due volte non vale due incoerenze."""
    if t is not None and t > now + FUTURE_TOLERANCE:
        return "timestamp_incoerente"
    return "valido"


# ------------------------------------------------------------------------------------------------ School
def school_log_events(log_path, now, diag):
    """Giri del job dal log (fatti osservati: il job registra avvio e fine) e categorie di anomalia per giro."""
    out = []
    if not log_path or not any(os.path.isfile(p) for p in [log_path] + ["%s.%d" % (log_path, i) for i in range(1, 6)]):
        diag.source("school.log_job", "assente")
        return out
    log = AUTO.read_log(log_path)
    runs = AUTO.build_runs(log["events"], now)
    diag.non_validi += log["bad_lines"]
    for r in runs:
        q = _check_time(r["avvio"], now, diag)
        if r["fine"] is not None and r["fine"] < r["avvio"]:
            q = "timestamp_incoerente"
        elif r["esito"] == "incompleto":
            q = "incompleto"
        wall = round((r["fine"] - r["avvio"]).total_seconds()) if r["fine"] and q != "timestamp_incoerente" else None
        out.append(event("school", "giro_job", "inferenza" if r["esito"] in ("in_corso", "incompleto") else "fatto_osservato",
                         ["giro", _iso(r["avvio"])], r["avvio"], "school-job.jsonl", "avvio %s" % _iso(r["avvio"]),
                         esito=r["esito"], gravita=RUN_SEVERITY.get(r["esito"], "avviso"), acquisito=r["fine"], qualita=q,
                         dettaglio={"modo": r["modo"], "exit_code": r["exit_code"], "durata_lavoro_s": r["durata_s"],
                                    "durata_reale_s": wall, "fonti": dict(sorted(r["fonti"].items())),
                                    "tentativi": r["tentativi"], "tipi_oltre_soglia": r["oltre_soglia"]}))
        for cat in r["anomalie"]:
            out.append(event("school", "anomalia_giro", "anomalia", ["anomalia", _iso(r["avvio"]), cat], r["fine"] or r["avvio"],
                             "school-job.jsonl", "fine del giro %s" % _iso(r["avvio"]), esito=cat, gravita="info",
                             dettaglio={"categoria": cat}))
    diag.source("school.log_job", "ok" if not log["bad_lines"] else "con_righe_illeggibili", letti=len(log["events"]),
                scartati=log["bad_lines"])
    return out


def school_schedule_events(log_path, plist_path, now, diag):
    """Orari del calendario senza un avvio: inferenza («mancato») o dato non verificabile (log che non copre l'orario)."""
    out = []
    if not plist_path:
        diag.source("school.calendario", "non_configurata")
        return out
    h = AUTO.history(log_path, plist_path, now)
    if not h.get("disponibile"):
        diag.source("school.calendario", "illeggibile", nota=h.get("motivo"))
        return out
    if h["calendario"]["stato"] != "ok":
        diag.source("school.calendario", h["calendario"]["stato"])
        return out
    n = 0
    for o in h.get("orari") or []:
        t = _utc(o.get("orario"))
        if o["stato"] == "mancato":
            n += 1
            out.append(event("school", "orario_mancato", "inferenza", ["orario", _iso(t)], t, "calendario + school-job.jsonl",
                             "orario %s" % _iso(t), esito="mancato", gravita="info" if o.get("sovrapposto") else "avviso",
                             dettaglio={"causa": "giro precedente ancora in corso" if o.get("sovrapposto")
                                        else "nessun avvio (Mac spento o in stop, o automazione non attiva)"}))
        elif o["stato"] == "non_verificabile":
            n += 1
            out.append(event("school", "orario_non_verificabile", "non_verificato", ["orario", _iso(t)], t,
                             "calendario + school-job.jsonl", "orario %s" % _iso(t), esito="non_verificabile", gravita="info",
                             dettaglio={"motivo": o.get("motivo")}))
    diag.source("school.calendario", "ok", letti=n)
    return out


def school_state_events(state_dir, now, diag):
    """Problemi aperti del job (stato corrente) e ultima notifica inviata per problema (fatto osservato)."""
    out = []
    p = os.path.join(state_dir, "state.json") if state_dir else None
    if not p or not os.path.isfile(p):
        diag.source("school.stato_job", "assente")
        return out
    try:
        with open(p, encoding="utf-8") as f:
            st = json.load(f)
        if not isinstance(st, dict):
            raise ValueError("non oggetto")
    except (OSError, ValueError) as e:
        diag.source("school.stato_job", "illeggibile", nota=type(e).__name__)
        return out
    raw = st.get("problemi") if isinstance(st.get("problemi"), dict) else {}
    letti = scartati = 0
    for key, v in sorted(raw.items()):
        if not isinstance(v, dict):
            scartati += 1
            continue
        letti += 1
        ref = "state.json#problemi/%s" % _h(key)[:8]             # mai la chiave completa (puo' contenere uid)
        kind = key.split(":")[0] if key.split(":")[0] in CS.PROBLEM_KINDS else "altro"
        fonte = key.split(":")[1] if ":" in key and key.split(":")[1] in ("classeviva", "classroom") else None
        dal, last = _utc(v.get("dal")), _utc(v.get("ultima_notifica"))
        det = {"tipo_problema": kind, "fonte": fonte, "giri": v.get("giri") if isinstance(v.get("giri"), int) else None}
        out.append(event("school", "problema_aperto", "stato_corrente", ["problema", _h(key), _iso(dal)], dal, "state.json", ref,
                         esito="in_osservazione" if v.get("attesa_conferma") else ("notificato" if last else "aperto"),
                         gravita="info" if v.get("attesa_conferma") else PROBLEM_SEVERITY.get(kind, "avviso"),
                         qualita="incompleto" if dal is None else _check_time(dal, now, diag), dettaglio=det))
        if last is not None:
            out.append(event("school", "notifica_inviata", "fatto_osservato", ["notifica", _h(key), _iso(last)], last, "state.json",
                             ref, esito="inviata", gravita="info", qualita=_check_time(last, now, diag), dettaglio=det))
    # chiusure gia' decise ma non ancora annunciate (il job le annuncia quando la fonte torna senza errori): stato
    # corrente senza un istante proprio (il job non lo registra); `acquisito` = fine dell'ultimo giro che ha scritto lo stato
    pending = st.get("risolti_in_attesa") if isinstance(st.get("risolti_in_attesa"), dict) else {}
    last_end = _utc((st.get("ultimo") or {}).get("fine")) if isinstance(st.get("ultimo"), dict) else None
    for key in sorted(pending):
        kind = key.split(":")[0] if key.split(":")[0] in CS.PROBLEM_KINDS else "altro"
        fonte = key.split(":")[1] if ":" in key and key.split(":")[1] in ("classeviva", "classroom") else None
        letti += 1
        out.append(event("school", "chiusura_in_attesa", "stato_corrente", ["chiusura", _h(key)], None, "state.json",
                         "state.json#risolti_in_attesa/%s" % _h(key)[:8], esito="in_attesa", gravita="info",
                         acquisito=last_end, dettaglio={"tipo_problema": kind, "fonte": fonte}))
    diag.source("school.stato_job", "ok", letti=letti, scartati=scartati)
    return out


def school_registry_events(registry_path, now, diag):
    """Esecuzioni del Registry per fonte e tipo (fatti osservati), da una copia in memoria: il file non cambia."""
    out = []
    if not registry_path or not os.path.isfile(registry_path):
        diag.source("school.registry_runs", "assente")
        return out
    try:
        reg = R.Registry.from_snapshot(registry_path)
        try:
            runs = reg.runs()
        finally:
            reg.close()
    except Exception as e:      # noqa: BLE001 - fonte illeggibile: dichiarata, mai un esito
        diag.source("school.registry_runs", "illeggibile", nota=type(e).__name__)
        return out
    scartati = 0
    for r in runs:
        start, end = _utc(r.get("started_at")), _utc(r.get("finished_at"))
        if not r.get("run_id") or start is None:
            scartati += 1
            continue
        q = _check_time(start, now, diag)
        if end is not None and end < start:
            q = "timestamp_incoerente"
        elif end is None:
            q = "incompleto"
        try:
            counts = json.loads(r["counts_json"]) if r.get("counts_json") else None
        except ValueError:
            counts = None
        ok = r.get("status") == "ok"
        out.append(event("school", "run_registro", "fatto_osservato", ["run", r["run_id"]], start, "registry.runs",
                         "run_id %s" % r["run_id"], esito=r.get("outcome") or r.get("status"),
                         gravita="info" if ok and r.get("outcome") == "ok" else "avviso", acquisito=end, qualita=q,
                         dettaglio={"fonte": r.get("source"), "tipo_dato": r.get("kind"), "modo": r.get("mode"),
                                    "durata_ms": r.get("duration_ms"), "letti": r.get("items_seen"),
                                    "conteggi": {k: v for k, v in (counts or {}).items() if isinstance(v, int)},
                                    "legacy": bool(r.get("legacy"))}))
    diag.non_validi += scartati
    diag.source("school.registry_runs", "ok", letti=len(runs), scartati=scartati)
    return out


# ------------------------------------------------------------------------------------------------ Finance (metadati)
def finance_events(money_feed, now, diag):
    """Dal feed Money gia' calcolato (consultazione v1, sola lettura): ultima importazione riuscita per conto (fatto
    osservato) e anomalie per conto (anomalia, stato corrente). Nessun movimento, importo o estratto."""
    out = []
    op = ((money_feed or {}).get("operazioni") or {}).get("data_status") if isinstance(money_feed, dict) else None
    if not isinstance(op, dict) or not op.get("ok") or not isinstance(op.get("data"), dict):
        diag.source("finance.data_status", "non_collegata" if money_feed is None else "illeggibile")   # None = scelta del chiamante
        return out
    data, scartati = op["data"], 0
    for a in data.get("accounts") or []:
        acc = a.get("accountId") if isinstance(a, dict) else None
        if not isinstance(acc, str):
            scartati += 1
            continue
        t = _utc(a.get("lastImportAt"))
        if t is not None:
            out.append(event("finance", "importazione_riuscita", "fatto_osservato", ["importazione", acc, _iso(t)], t,
                             # lastImportAt e' l'ultima importazione RIUSCITA; lastImportStatus e' l'ultima esecuzione (puo'
                             # essere un'altra, rifiutata: e' l'anomalia last_import_rejected, con il suo istante)
                             "consultazione v1 data_status", "conto %s" % acc, esito="committed",
                             gravita="info", qualita=_check_time(t, now, diag), dettaglio={"conto": acc}))
    for an in data.get("anomalies") or []:
        if not isinstance(an, dict) or not isinstance(an.get("code"), str):
            scartati += 1
            continue
        acc = an.get("accountId") if isinstance(an.get("accountId"), str) else None
        sev = {"error": "errore", "warning": "avviso"}.get(an.get("severity"), "info")
        detail = an.get("detail") if isinstance(an.get("detail"), dict) else {}
        # istante solo se il motore lo dichiara (es. `last_import_rejected`: detail.at = fine dell'importazione rifiutata)
        t = _utc(detail.get("at"))
        # discriminanti non sensibili dichiarati dal motore: periodo (differenza di riconciliazione) e data (saldi in
        # conflitto). Entrano nella chiave: due periodi dello stesso conto sono due anomalie, non un duplicato.
        # Importi (differenceMinor), giorni e messaggio restano fuori.
        period = detail.get("period") if isinstance(detail.get("period"), str) and _PERIOD.match(detail["period"]) else None
        day = detail.get("date") if isinstance(detail.get("date"), str) and _DATE.match(detail["date"]) else None
        det = {"conto": acc, "codice": an["code"]}
        det.update({k: v for k, v in (("periodo", period), ("data", day)) if v})
        out.append(event("finance", "anomalia_dati", "anomalia", ["anomalia", acc, an["code"], _iso(t)] + [x for kv in det.items() if kv[0] in ("periodo", "data") for x in kv], t,
                         "consultazione v1 data_status", "conto %s" % acc if acc else "generale", esito=an["code"], gravita=sev,
                         qualita=_check_time(t, now, diag) if t else "incompleto", dettaglio=det))
    diag.non_validi += scartati
    diag.source("finance.data_status", "ok", letti=len(data.get("accounts") or []) + len(data.get("anomalies") or []),
                scartati=scartati)
    return out


def bank_sync_events(path, now, diag):
    """Sincronizzazione bancaria: ultimo tentativo per conto (fatto osservato) e consenso (stato corrente, RICALCOLATO
    adesso dalla scadenza: lo stato scritto nel file puo' essere vecchio). File assente = nessun collegamento configurato."""
    out = []
    if not path or not os.path.isfile(path):
        diag.source("finance.sincronizzazione", "non_configurata")
        return out
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        if not isinstance(doc, dict) or doc.get("schema") != BANK_STATUS_SCHEMA or not isinstance(doc.get("connessioni"), list):
            raise ValueError("schema")
    except (OSError, ValueError) as e:
        diag.source("finance.sincronizzazione", "illeggibile", nota=type(e).__name__)
        return out
    written, scartati = _utc(doc.get("generato")), 0
    for c in doc["connessioni"]:
        conto = c.get("conto") if isinstance(c, dict) else None
        if not isinstance(conto, str) or not re.fullmatch(r"[\w.-]{1,60}", conto):
            scartati += 1
            continue
        bank = c.get("banca") if isinstance(c.get("banca"), dict) else {}
        banca = bank.get("nome") if isinstance(bank.get("nome"), str) and len(bank["nome"]) <= 60 else None
        ref = "banca-stato.json#%s" % conto
        last = c.get("ultimo_tentativo") if isinstance(c.get("ultimo_tentativo"), dict) else None
        if last:
            t = _utc(last.get("at"))
            esito = last.get("esito") if last.get("esito") in SYNC_SEVERITY else "non_eseguita"
            err = last.get("errore") if isinstance(last.get("errore"), str) and re.fullmatch(r"[a-z_]{1,40}", last["errore"]) else None
            num = lambda k: last.get(k) if isinstance(last.get(k), int) and not isinstance(last.get(k), bool) else None  # noqa: E731
            out.append(event("finance", "sincronizzazione_bancaria", "fatto_osservato", ["sync", conto, _iso(t)], t,
                             "banca-stato.json", ref, esito=esito,
                             gravita="errore" if err == "consent_expired" else SYNC_SEVERITY[esito],
                             qualita=_check_time(t, now, diag) if t else "incompleto",
                             dettaglio={"conto": conto, "banca": banca, "errore": err,
                                        "database": doc.get("database") if doc.get("database") in ("copia", "reale") else None,
                                        "nuovi": num("nuovi"), "aggiornati": num("aggiornati"),
                                        "riprova_dopo_ore": num("riprova_dopo_ore")}))
        cons = c.get("consenso") if isinstance(c.get("consenso"), dict) else {}
        exp = cons.get("scade_il") if isinstance(cons.get("scade_il"), str) and re.fullmatch(r"\d{4}-\d\d-\d\d", cons["scade_il"]) else None
        if exp:
            days = (dt.date.fromisoformat(exp) - now.date()).days
            stato = "scaduto" if days <= 0 else "in_scadenza" if days < CONSENT_WARN_DAYS else "valido"
        else:
            days, stato = None, "assente"
        out.append(event("finance", "consenso_bancario", "stato_corrente", ["consenso", conto, exp], None, "banca-stato.json",
                         ref, esito=stato, gravita=CONSENT_SEVERITY[stato], acquisito=written,
                         dettaglio={"conto": conto, "banca": banca, "giorni_rimasti": max(days, 0) if days is not None else None,
                                    "scadenza": exp}))
    diag.non_validi += scartati
    diag.source("finance.sincronizzazione", "ok", letti=len(doc["connessioni"]), scartati=scartati)
    return out


# ------------------------------------------------------------------------------------------------ Core (azioni)
def core_action_events(actions_path, now, diag):
    """Un evento per azione: l'ultimo passaggio registrato (fatto osservato), con il numero di passaggi. Solo vocabolario
    chiuso: il motivo scritto nella richiesta resta nel registro delle azioni."""
    out = []
    if not actions_path:
        diag.source("core.azioni", "non_configurata")
        return out
    if not os.path.isfile(actions_path):
        diag.source("core.azioni", "assente")
        return out
    import cuore_actions as A
    try:
        doc = A.read(actions_path)
    except (OSError, ValueError) as e:
        diag.source("core.azioni", "illeggibile", nota=type(e).__name__)
        return out
    if not doc["integro"]:
        diag.non_validi += len(doc["problemi"])
        diag.source("core.azioni", "illeggibile", scartati=len(doc["problemi"]), nota="catena interrotta o passaggi non ammessi")
        return out
    for a in doc["azioni"]:
        t = _utc(a["ultimo"])
        out.append(event("core", "azione", "fatto_osservato", ["azione", a["id"], a["stato"]], t, "azioni.jsonl",
                         "azioni.jsonl#%s" % a["id"], esito=a["stato"], gravita=ACTION_SEVERITY.get(a["stato"], "info"),
                         qualita=_check_time(t, now, diag),
                         dettaglio={"tipo_azione": a["tipo"], "area_azione": a["area"], "passi": len(a["passi"]),
                                    "richiesta_da": a["richiesta_da"]}))
    diag.source("core.azioni", "ok", letti=len(doc["azioni"]))
    return out


# ------------------------------------------------------------------------------------------------ registro
def merge(events, now, days, diag):
    """Deduplica per id (la prima occorrenza in ordine canonico vince, deterministica), finestra temporale, ordine."""
    start = now - dt.timedelta(days=days)
    seen = {}
    for e in sorted(events, key=lambda e: json.dumps(e, sort_keys=True, ensure_ascii=False)):
        if e["id"] in seen:
            diag.duplicati += 1
            continue
        seen[e["id"]] = e
    diag.timestamp_incoerenti = sum(1 for e in seen.values() if e["qualita"] == "timestamp_incoerente")
    keep = [e for e in seen.values() if e["quando"] is None or _utc(e["quando"]) >= start]
    timed = sorted((e for e in keep if e["quando"] is not None), key=lambda e: (_utc(e["quando"]), e["id"]), reverse=True)
    return timed + sorted((e for e in keep if e["quando"] is None), key=lambda e: e["id"])    # piu' recenti prima; senza istante in fondo


def build(now=None, days=DAYS, log_path=None, plist_path=None, state_dir=None, registry_path=None, money_feed=None,
          real=False, actions_path=None, bank_status_path=None):
    """Registro degli eventi. Con `real=True` usa i percorsi reali di CUORE (stessi del ponte); altrimenti solo i
    percorsi passati (test). `money_feed`: feed Money gia' calcolato (nessuna nuova lettura Finance da qui).
    `actions_path`: registro delle azioni, solo se indicato (nessun percorso reale predefinito)."""
    now = (now or dt.datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    if real:
        registry_path = registry_path or R.default_path()
        lp, pp = CS.automation_paths(None)
        log_path, plist_path = log_path or lp, plist_path or pp
        state_dir = state_dir or CS.job_dir(None)
        bank_status_path = bank_status_path or os.path.expanduser(BANK_STATUS_DEFAULT)
    diag = _Diag()
    ev = []
    for name, produce in (("school.log_job", lambda: school_log_events(log_path, now, diag)),
                          ("school.calendario", lambda: school_schedule_events(log_path, plist_path, now, diag)),
                          ("school.stato_job", lambda: school_state_events(state_dir, now, diag)),
                          ("school.registry_runs", lambda: school_registry_events(registry_path, now, diag)),
                          ("finance.data_status", lambda: finance_events(money_feed, now, diag)),
                          ("finance.sincronizzazione", lambda: bank_sync_events(bank_status_path, now, diag)),
                          ("core.azioni", lambda: core_action_events(actions_path, now, diag))):
        got = produce()
        diag.fonti[name]["eventi_prodotti"] = len(got)        # prima di deduplica e finestra: quanto ha dato ogni fonte
        ev += got
    out = merge(ev, now, days, diag)
    counts = {}
    for e in out:
        k = "%s/%s" % (e["area"], e["natura"])
        counts[k] = counts.get(k, 0) + 1
    return {"schema": SCHEMA, "generato": _iso(now), "finestra_giorni": days, "eventi": out,
            "conteggi": dict(sorted(counts.items())), "fonti": dict(sorted(diag.fonti.items())),
            "diagnostica": {"duplicati_scartati": diag.duplicati, "record_non_validi": diag.non_validi,
                            "timestamp_incoerenti": diag.timestamp_incoerenti},
            "nota": "vista calcolata in sola lettura: nessun dato scritto, nessun evento inventato"}


def _unsafe_values(v, depth=0):
    """Valori del dettaglio non ammessi: testo lungo o multiriga, indirizzi, URL, numeri lunghi; strutture profonde."""
    if isinstance(v, dict):
        return depth > 1 or any(_unsafe_values(k, depth + 1) or _unsafe_values(x, depth + 1) for k, x in v.items())
    if isinstance(v, list):
        return depth > 1 or any(_unsafe_values(x, depth + 1) for x in v)
    if isinstance(v, str):
        return len(v) > 120 or bool(_UNSAFE.search(v))
    return not (v is None or isinstance(v, (bool, int, float)))


EVENT_KEYS = {"id", "area", "tipo", "natura", "quando", "acquisito", "esito", "gravita", "fonte", "qualita", "dettaglio"}


def validate(doc):
    """Problemi di un documento `cuore.events/1` (vuoto = conforme): struttura, vocabolario chiuso per tipo (area,
    natura, chiavi del dettaglio, forma del riferimento), privacy dei valori, ordine, conteggi e diagnostica coerenti."""
    p = []
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return ["schema non supportato"]
    evs = doc.get("eventi")
    if not isinstance(evs, list):
        return ["eventi: lista attesa"]
    ids, counts, last, untimed = set(), {}, None, False
    for i, e in enumerate(evs):
        w = "eventi[%d]" % i
        if not isinstance(e, dict) or set(e) != EVENT_KEYS:
            p.append(w + ": campi diversi da quelli del contratto")
            continue
        if not isinstance(e["id"], str) or not re.match(r"^evt:[0-9a-f]{16}$", e["id"]):
            p.append(w + ": id non nel formato evt:<16 esadecimali>")
        if e["id"] in ids:
            p.append(w + ": id duplicato")
        ids.add(e["id"])
        spec = TYPES.get(e["tipo"])
        if spec is None:
            p.append(w + ": tipo non ammesso")
        else:
            area, natures, keys, ref = spec
            if e["area"] != area or e["natura"] not in natures:
                p.append(w + ": area o natura non ammesse per il tipo")
            if not isinstance(e["dettaglio"], dict) or set(e["dettaglio"]) - set(keys):
                p.append(w + ": chiavi del dettaglio fuori vocabolario")
            fonte = e["fonte"] if isinstance(e["fonte"], dict) else {}
            if set(fonte) != {"origine", "riferimento"} or not fonte.get("origine") \
                    or not isinstance(fonte.get("riferimento"), str) or not re.fullmatch(ref, fonte["riferimento"]):
                p.append(w + ": fonte o riferimento non nella forma del tipo")
        if e["natura"] not in NATURE or e["gravita"] not in SEVERITY or e["qualita"] not in QUALITY:
            p.append(w + ": natura, gravita' o qualita' non ammesse")
        if _unsafe_values(e["dettaglio"]) or _unsafe_values(e["esito"]):
            p.append(w + ": valori non ammessi nel dettaglio o nell'esito (testo libero, indirizzi, numeri lunghi)")
        for f in ("quando", "acquisito"):
            if e[f] is not None and (not isinstance(e[f], str) or not e[f].endswith("+00:00") or _utc(e[f]) is None):
                p.append(w + ": %s non e' un istante UTC" % f)
        t = _utc(e["quando"]) if e["quando"] is not None else None
        if t is None:
            untimed = True
        elif untimed or (last is not None and t > last):
            p.append(w + ": ordine non rispettato (piu' recenti prima, senza istante in fondo)")
        last = t if t is not None else last
        k = "%s/%s" % (e.get("area"), e.get("natura"))
        counts[k] = counts.get(k, 0) + 1
    if doc.get("conteggi") != dict(sorted(counts.items())):
        p.append("conteggi non coerenti con gli eventi")
    fonti = doc.get("fonti")
    if not isinstance(fonti, dict) or any(not isinstance(f, dict) or f.get("stato") not in SOURCE_STATES for f in fonti.values()):
        p.append("fonti: stato non ammesso")
    dg = doc.get("diagnostica")
    if not isinstance(dg, dict) or set(dg) != {"duplicati_scartati", "record_non_validi", "timestamp_incoerenti"} \
            or not all(isinstance(v, int) and v >= 0 for v in dg.values()):
        p.append("diagnostica: campi interi non negativi attesi")
    return p


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Registro degli eventi CUORE (sola lettura). Default: solo conteggi.")
    ap.add_argument("--giorni", type=int, default=DAYS)
    ap.add_argument("--json", action="store_true", help="documento completo (vocabolario fisso, nessun dato scolastico)")
    a = ap.parse_args()
    doc = build(days=a.giorni, real=True)
    print(json.dumps(doc if a.json else {k: doc[k] for k in ("schema", "generato", "conteggi", "fonti", "diagnostica")},
                     ensure_ascii=False, indent=1))
