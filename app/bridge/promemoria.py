"""CUORE · promemoria sul Mac (D5/E4): SPENTO per difetto.

    python3 -B app/bridge/promemoria.py prova  [opzioni]   stampa cosa notificherebbe ora (nessuna notifica, nessuno stato)
    python3 -B app/bridge/promemoria.py esegui [opzioni]   notifica i promemoria nuovi (LaunchAgent cloud.cuore.promemoria)

- Impostazioni: UN solo file JSON (`--config`, predefinito `~/Library/Application Support/CUORE/promemoria.json`),
  scritto dalle Impostazioni dell'app tramite il ponte (`scritture.py`, operazione `promemoria_imposta`) o a mano.
  Senza file, con `"attivo": false` o con un file non valido nulla viene mostrato. Ogni promemoria si accende a parte e
  sceglie i propri momenti (mattina 07:15, pomeriggio 15:00, sera 19:00: gli orari del LaunchAgent); quelli non
  elencati restano spenti. Modello: `app/launchd/promemoria.esempio.json`.
- Momento di un'esecuzione: l'ultimo dei tre orari gia' passato oggi (un orario perso con il Mac in stop vale al
  risveglio); prima delle 07:15 nessun promemoria. Un promemoria parte solo nei momenti scelti.
- Fonti, tutte in sola lettura: feed gia' calcolati dal ponte (`cuore_school.build_feed`, `cuore_money.build_feed`),
  l'archivio personale School (compiti fatti, impegni E4; `--school-personale`) e gli appunti Finance
  (`--finance-appunti`, `finance_appunti.py`). Nessuna rete propria (il feed School blocca la rete durante la lettura).
- Ogni promemoria ha una chiave stabile e non si ripete: le chiavi gia' mostrate stanno in un file di stato (`--stato`,
  predefinito `~/Library/Application Support/CUORE/promemoria-stato.json`, mai in iCloud o nel repository; 0700/0600).
  Su un elemento (verifica, compito, voto, budget...): una volta sola. Giustificazioni: due volte a settimana (da
  lunedi' e da giovedi') finche' restano. Situazioni (dati vecchi, da classificare): al massimo una volta a settimana.
  Voti e comunicazioni nuovi: alla prima lettura (o alla riaccensione) lo storico e' registrato senza notifiche.
- Consegna: notifica del Mac (`osascript`, testo passato come argomento, mai dentro lo script). Testi riservati per
  difetto (`"riservato": true` nel file; `--riservato`/`--no-riservato` lo sostituiscono): niente importi, materie, date
  di assenze o testi personali. Promemoria dello stesso tipo uniti in una notifica. Una chiave e' segnata «mostrata»
  solo se la notifica e' partita.
- Uscita: solo conteggi ed esiti (mai testi o importi), tranne `prova`, che stampa i testi a terminale.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import finance_appunti  # noqa: E402
import school_personale  # noqa: E402

REPO_ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
CONFIG_DEFAULT = "~/Library/Application Support/CUORE/promemoria.json"
STATE_DEFAULT = "~/Library/Application Support/CUORE/promemoria-stato.json"
SCHOOL_PERSONALE = "~/Library/Application Support/CUORE/school/personale.jsonl"     # come serve.py
FINANCE_APPUNTI = "~/Library/Application Support/CUORE/finance/appunti.jsonl"
ZONE = "Europe/Rome"
STATE_SCHEMA = "cuore.promemoria.stato/1"
SETTINGS_SCHEMA = "cuore.promemoria.impostazioni/1"
KEEP_DAYS = 400                                   # chiavi piu' vecchie tolte dallo stato (nessuna torna valida dopo)
MOMENTI = {"mattina": "07:15", "pomeriggio": "15:00", "sera": "19:00"}     # = StartCalendarInterval del plist
ALL = tuple(MOMENTI)
SERA = ("sera",)
# chiave: area, titolo per le Impostazioni, momenti predefiniti, momenti possibili, acceso nel modello, proposta nuova
REMINDERS = {
    "sch_verifica": ("school", "Verifica domani o tra pochi giorni", ALL, ALL, True, False),
    "sch_compiti": ("school", "Compito per domani non segnato fatto", ALL, ALL, True, False),
    "sch_assenze": ("school", "Giustificazioni da fare (lunedì e giovedì)", ALL, ALL, True, False),
    "sch_dati": ("school", "Dati School non aggiornati", ALL, ALL, True, False),
    "sch_voti": ("school", "Voto nuovo", ALL, ALL, True, False),
    "sch_comunicazioni": ("school", "Comunicazione o circolare nuova", ALL, ALL, True, False),
    "sch_impegni": ("school", "Impegni personali di domani", SERA, ALL, True, False),
    "sch_compiti_sera": ("school", "La sera: compiti per domani ancora da segnare fatti", SERA, SERA, False, True),
    "fin_budget": ("finance", "Budget del mese quasi finito o superato", ALL, ALL, True, False),
    "fin_dati": ("finance", "Dati Finance vecchi", ALL, ALL, True, False),
    "fin_classificare": ("finance", "Movimenti da classificare", ALL, ALL, True, False),
    "fin_contanti": ("finance", "Segna le spese in contanti", SERA, SERA, True, False),
    "fin_appunti": ("finance", "Appunti con promemoria", ALL, ALL, True, False),
    "fin_settimana": ("finance", "Domenica sera: guarda le spese della settimana", SERA, SERA, False, True),
}
KINDS = tuple(REMINDERS)
BASELINE = ("sch_voti", "sch_comunicazioni")     # alla prima lettura lo storico non si notifica
FEED_KINDS = {"school": ("sch_verifica", "sch_compiti", "sch_assenze", "sch_dati", "sch_voti", "sch_comunicazioni",
                         "sch_compiti_sera"), "money": ("fin_budget", "fin_dati", "fin_classificare")}
THRESHOLDS = {"verifica_giorni": [1, 3], "school_dati_ore": 24, "budget_percento": 85, "finance_dati_giorni": 7}
GROUPS = {"needs": "Necessità", "wants": "Desideri", "savings": "Risparmio", "unassigned": "Senza gruppo"}
DONE_STATES = ("consegnato", "restituito")       # stato_operativo Classroom: compito gia' consegnato
COMMS = {"notice": "Circolare nuova", "announcement": "Annuncio nuovo su Classroom"}     # kind del Registry


class ConfigNonValida(ValueError):
    pass


# ------------------------------------------------------------------------------------------------ configurazione
def load_config(path):
    """(configurazione, None) se acceso; (None, motivo) se spento. Mai eccezioni."""
    if not os.path.isfile(path):
        return None, "spento: nessun file di configurazione"
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return parse_config(raw)
    except (OSError, ValueError) as e:
        return None, "spento: configurazione non valida (%s)" % (e if isinstance(e, ConfigNonValida) else type(e).__name__)


def _bool(v, name):
    if not isinstance(v, bool):
        raise ConfigNonValida("%s: true o false" % name)
    return v


def _moments(k, v):
    allowed = REMINDERS[k][3]
    if not isinstance(v, list) or not v or len(set(v)) != len(v) or not all(m in allowed for m in v):
        raise ConfigNonValida("%s.momenti: elenco non vuoto fra %s" % (k, ", ".join(allowed)))
    return [m for m in ALL if m in v]


def normalize(raw):
    """Impostazioni complete e in forma canonica (anche da spente) o ConfigNonValida. Un promemoria vale `true|false`
    (momenti predefiniti) oppure `{"attivo": bool, "momenti": [...]}`; quelli non elencati sono spenti."""
    if not isinstance(raw, dict) or set(raw) - {"attivo", "riservato", "promemoria", "soglie", "nota"}:
        raise ConfigNonValida("campi ammessi: attivo, riservato, promemoria, soglie, nota")
    on = raw.get("promemoria", {})
    if not isinstance(on, dict) or set(on) - set(KINDS):
        raise ConfigNonValida("promemoria: chiavi ammesse %s" % ", ".join(KINDS))
    rems = {}
    for k in KINDS:
        v = on.get(k, False)
        if isinstance(v, bool):
            v = {"attivo": v}
        if not isinstance(v, dict) or set(v) - {"attivo", "momenti"}:
            raise ConfigNonValida("%s: true|false oppure {attivo, momenti}" % k)
        rems[k] = {"attivo": _bool(v.get("attivo", False), k + ".attivo"),
                   "momenti": _moments(k, v["momenti"]) if "momenti" in v else list(REMINDERS[k][2])}
    th = dict(THRESHOLDS, **(raw.get("soglie") or {}))
    if set(th) != set(THRESHOLDS):
        raise ConfigNonValida("soglie ammesse: %s" % ", ".join(THRESHOLDS))
    days = th["verifica_giorni"]
    if not isinstance(days, list) or not days or not all(isinstance(d, int) and not isinstance(d, bool) and 1 <= d <= 14 for d in days):
        raise ConfigNonValida("verifica_giorni: elenco di giorni da 1 a 14")
    for k, lo, hi in (("school_dati_ore", 6, 24 * 14), ("budget_percento", 50, 99), ("finance_dati_giorni", 2, 90)):
        if not isinstance(th[k], int) or isinstance(th[k], bool) or not lo <= th[k] <= hi:
            raise ConfigNonValida("%s: intero da %d a %d" % (k, lo, hi))
    nota = raw.get("nota")
    if nota is not None and (not isinstance(nota, str) or len(nota) > 1000):
        raise ConfigNonValida("nota: testo fino a 1000 caratteri")
    out = {"attivo": _bool(raw.get("attivo", False), "attivo"), "riservato": _bool(raw.get("riservato", True), "riservato"),
           "promemoria": rems, "soglie": th}
    return dict(out, nota=nota) if nota is not None else out


def _engine(n):
    return {"on": {k: v["attivo"] for k, v in n["promemoria"].items()},
            "momenti": {k: v["momenti"] for k, v in n["promemoria"].items()},
            "soglie": n["soglie"], "riservato": n["riservato"]}


def parse_config(raw):
    n = normalize(raw)
    if not n["attivo"]:
        return None, "spento: \"attivo\" non e' true"
    return _engine(n), None


def model_settings():
    """Impostazioni di partenza quando il file non c'e': tutto spento, in generale e uno per uno (decisione del titolare
    del 05/11: ogni promemoria si accende a parte dall'app; accendere l'interruttore generale non ne accende nessuno).
    I promemoria accesi nel modello (`acceso nel modello`) valgono solo per `promemoria.esempio.json`, copiato a mano."""
    return normalize({"attivo": False})


def default_config():
    """Tutti i promemoria accesi con le soglie predefinite: solo per `prova` senza file (anteprima, nessuna notifica)."""
    return _engine(normalize({"attivo": True, "promemoria": {k: True for k in KINDS}}))


# ------------------------------------------------------------------------------------------------ decisione (pura)
def _ddmm(iso):
    return "%s/%s" % (iso[8:10], iso[5:7])


def _subject(subjects, key):
    name = subjects.get(key) if isinstance(key, str) else None
    return name.capitalize() if isinstance(name, str) and name.isupper() else name


def _when(days):
    return "domani" if days == 1 else "tra %d giorni" % days


def _block(feed, page, block_id, empty_ok=False):
    """Dati di un blocco validato; `empty_ok` = anche un blocco vuoto ma valido (lettura riuscita senza elementi)."""
    doc = ((feed or {}).get("pagine") or {}).get(page) or {}
    b = next((x for x in doc.get("blocchi", []) if isinstance(x, dict) and x.get("id") == block_id), None)
    ok = ("dati", "vuoto_valido") if empty_ok else ("dati",)
    return b.get("dati") if b and b.get("stato") in ok else None


def half_week(day):
    """Meta' settimana del giorno: da lunedi' (lun-mer) o da giovedi' (gio-dom)."""
    y, w, d = day.isocalendar()
    return "%d-W%02d-%s" % (y, w, "lun" if d <= 3 else "gio")


def _grades(feed):
    """[(uid, valore | None)] di tutti i voti del feed (serie dei voti inclusi + esclusi non annullati); None = non letto."""
    data = _block(feed, "voti", "complessivo", empty_ok=True)
    if not isinstance(data, dict):
        return None
    out = [(g.get("uid"), g.get("valore")) for g in data.get("serie") or [] if isinstance(g, dict)]
    for why, uids in (data.get("esclusi") or {}).items():
        if why != "escluso_annullato" and isinstance(uids, list):
            out += [(u, None) for u in uids]
    return [(u, v) for u, v in out if isinstance(u, str) and u]


def _comms(feed):
    """[(fonte, uid, kind)] delle comunicazioni create di recente (blocco «novita'» della Home); None = non letto."""
    data = _block(feed, "home", "novita", empty_ok=True)
    if not isinstance(data, dict):
        return None
    return [(r.get("fonte"), r.get("uid_fonte"), r.get("kind")) for r in data.get("ultimi") or []
            if isinstance(r, dict) and r.get("evento") == "create" and r.get("kind") in COMMS and r.get("uid_fonte")]


def baseline_ready(feed, kind):
    """La fonte di un promemoria «nuovo» e' stata letta (anche vuota): si puo' registrare lo storico."""
    return (_grades if kind == "sch_voti" else _comms)(feed) is not None


def _parse_dt(v):
    try:
        t = dt.datetime.fromisoformat(v)
        return t if t.tzinfo else None
    except (TypeError, ValueError):
        return None


def _rem(key, kind, group, text, safe, plural, detail=None):
    return {"chiave": key, "tipo": kind, "gruppo": group, "testo": text, "riservato": safe, "plurale": plural,
            "dettaglio": detail}


def school_reminders(feed, cfg, today, done, now):
    """Promemoria School dal feed `cuore.school.feed/1` (solo pagine validate) e dai compiti segnati fatti."""
    on, th, out = cfg["on"], cfg["soglie"], []
    subjects = (feed or {}).get("materie") or {}
    tomorrow = (today + dt.timedelta(days=1)).isoformat()
    if on["sch_verifica"]:
        for v in _block(feed, "verifiche", "verifiche_future") or []:
            try:
                days = (dt.date.fromisoformat(v.get("data")) - today).days
            except (TypeError, ValueError):
                continue
            if days in th["verifica_giorni"] and v.get("uid"):
                name = _subject(subjects, v.get("materia"))
                text = ("Verifica di %s %s" % (name, _when(days))) if name else "Verifica %s" % _when(days)
                out.append(_rem("school:verifica:%s:%d" % (v["uid"], days), "sch_verifica", "verifica:%d" % days, text,
                                "%s c'è una verifica" % _when(days).capitalize(),
                                "%s ci sono {n} verifiche" % _when(days).capitalize(), name))
    if on["sch_compiti"] or on["sch_compiti_sera"]:
        for c in _block(feed, "compiti", "compiti_aperti") or []:
            uid = c.get("uid")
            if c.get("scadenza") != tomorrow or not uid or uid in done or c.get("stato_operativo") in DONE_STATES:
                continue
            name = _subject(subjects, c.get("materia"))
            text = ("Compito di %s per domani non segnato fatto" % name) if name else "Compito per domani non segnato fatto"
            if on["sch_compiti"]:
                out.append(_rem("school:compito:%s:%s" % (uid, tomorrow), "sch_compiti", "compito", text,
                                "Un compito per domani non è segnato fatto", "{n} compiti per domani non sono segnati fatti", name))
            if on["sch_compiti_sera"]:
                out.append(_rem("school:compito-sera:%s:%s" % (uid, tomorrow), "sch_compiti_sera", "compito-sera", text,
                                "Hai ancora un compito per domani da segnare fatto",
                                "Hai ancora {n} compiti per domani da segnare fatti", name))
    if on["sch_assenze"]:
        half = half_week(today)
        for a in _block(feed, "assenze", "eventi") or []:
            if a.get("uid") and a.get("stato_giustificazione") == "Da giustificare" and a.get("giustificata") is not True:
                label = "%s del %s" % (a.get("tipo") or "Assenza", _ddmm(a["data"])) if isinstance(a.get("data"), str) else "Assenza"
                out.append(_rem("school:assenza:%s:%s" % (a["uid"], half), "sch_assenze", "assenza", "%s da giustificare" % label,
                                "C'è una giustificazione da fare", "Ci sono {n} giustificazioni da fare", label))
    if on["sch_voti"]:
        for uid, value in _grades(feed) or []:
            shown = ("%.2f" % value).rstrip("0").rstrip(".").replace(".", ",") if isinstance(value, (int, float)) else None
            out.append(_rem("school:voto:%s" % uid, "sch_voti", "voto", "Voto nuovo: %s" % shown if shown else "Voto nuovo",
                            "È arrivato un voto nuovo", "Sono arrivati {n} voti nuovi", shown))
    if on["sch_comunicazioni"]:
        for source, uid, kind in _comms(feed) or []:
            out.append(_rem("school:comunicazione:%s:%s" % (source, uid), "sch_comunicazioni", "comunicazione", COMMS[kind],
                            "C'è una comunicazione nuova", "Ci sono {n} comunicazioni nuove", COMMS[kind]))
    if on["sch_dati"]:
        last = _school_last_ok(feed)
        if last is not None:
            hours = int((now - last).total_seconds() // 3600)
            if hours > th["school_dati_ore"]:
                text = "Dati School non aggiornati da %d ore (ultimo giro riuscito: %s)" % (
                    hours, last.astimezone(now.tzinfo).strftime("%d/%m %H:%M"))
                out.append(_rem("school:dati-vecchi:%s" % last.isoformat(), "sch_dati", "school-dati", text, text, text))
    return out


def _school_last_ok(feed):
    """Ultimo giro riuscito del job School (`automazione.job.ultimo_commit_ok`); senza, la lettura piu' vecchia fra le
    fonti (`freschezza`). Nessuna delle due = None (mai un'eta' inventata)."""
    job = ((feed or {}).get("automazione") or {}).get("job") or {}
    last = _parse_dt(job.get("ultimo_commit_ok"))
    if last is not None:
        return last
    fr = (feed or {}).get("freschezza") or {}
    dates = [_parse_dt(s.get("aggiornato_al")) for s in (fr.get("per_fonte") or {}).values() if isinstance(s, dict)]
    dates = [d for d in dates if d is not None]
    return min(dates) if fr.get("disponibile") and dates else None


def _op(feed, name):
    env = ((feed or {}).get("operazioni") or {}).get(name) or {}
    return env.get("data") if env.get("ok") and (feed or {}).get("sola_lettura_verificata") else None


def _money(minor, currency):
    s = "%d,%02d" % divmod(abs(minor), 100)
    return ("-" if minor < 0 else "") + s + (" €" if currency == "EUR" else " " + currency)


def finance_reminders(feed, cfg, today):
    """Promemoria Finance dal feed `cuore.money.feed/1` (solo operazioni riuscite e validate)."""
    on, th, out = cfg["on"], cfg["soglie"], []
    week = "%d-W%02d" % today.isocalendar()[:2]
    if on["fin_budget"]:
        data = _op(feed, "budgets") or {}
        names = {c.get("id"): c.get("name") for c in (_op(feed, "categories") or {}).get("items", [])}
        pct = th["budget_percento"]
        for cur in data.get("currencies") or []:
            for line in cur.get("lines") or []:
                budget, spent = line.get("budgetMinor"), line.get("spentMinor")
                if not isinstance(budget, int) or not isinstance(spent, int) or budget <= 0:
                    continue
                over = line.get("overBudget") is True or spent > budget
                if not over and spent * 100 < pct * budget:
                    continue
                scope, target = line.get("scope"), line.get("target")
                label = "totale" if scope == "total" else GROUPS.get(target, target) if scope == "budget_group" \
                    else names.get(target) or "di una categoria"
                money = "%s su %s" % (_money(spent, cur.get("currency")), _money(budget, cur.get("currency")))
                key = "finance:budget:%s:%s:%s:%s" % (data.get("month"), scope, target or "totale", cur.get("currency"))
                if over:
                    out.append(_rem(key + ":superato", "fin_budget", "budget-superato", "Budget %s superato: %s" % (label, money),
                                    "Un budget del mese è stato superato", "{n} budget del mese sono stati superati", label))
                else:
                    out.append(_rem(key + ":soglia", "fin_budget", "budget-soglia",
                                    "Budget %s al %d%%: %s" % (label, spent * 100 // budget, money),
                                    "Un budget del mese ha passato l'%d%%" % pct, "{n} budget del mese hanno passato l'%d%%" % pct,
                                    label))
    if on["fin_dati"] and (feed or {}).get("sola_lettura_verificata"):
        last = ((feed or {}).get("aggiornamento") or {}).get("ultimo_movimento")
        try:
            days = (today - dt.date.fromisoformat(last)).days
        except (TypeError, ValueError):
            days = None
        if days is not None and days > th["finance_dati_giorni"]:
            text = "Dati Finance fermi al %s (%d giorni fa): servono estratti nuovi" % (_ddmm(last), days)
            out.append(_rem("finance:dati-vecchi:%s:%s" % (last, week), "fin_dati", "finance-dati", text, text, text))
    if on["fin_classificare"]:
        n = ((_op(feed, "data_status") or {}).get("openReviewItems") or {}).get("classification_needed")
        if isinstance(n, int) and not isinstance(n, bool) and n > 0:
            out.append(_rem("finance:da-classificare:%s" % week, "fin_classificare", "classificare",
                            "%d moviment%s da classificare" % (n, "o" if n == 1 else "i"),
                            "Ci sono movimenti da classificare", "Ci sono movimenti da classificare"))
    return out


def due_at(when):
    """Istante (ora italiana) da cui un appunto va ricordato; solo data = inizio del giorno. None = non valido."""
    from zoneinfo import ZoneInfo
    try:
        t = dt.datetime.fromisoformat(when)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=ZoneInfo(ZONE))


def personal_reminders(cfg, now, impegni, appunti):
    """Promemoria senza feed: impegni personali School di domani (E4), spese in contanti la sera, appunti Finance con
    promemoria arrivato, controllo della settimana la domenica. `impegni`/`appunti` None = archivio non letto."""
    on, out, today = cfg["on"], [], now.date()
    tomorrow = (today + dt.timedelta(days=1)).isoformat()
    if on["sch_impegni"]:
        for e in impegni or []:
            if e.get("data") == tomorrow and e.get("id"):
                out.append(_rem("school:impegno:%s:%s" % (e["id"], tomorrow), "sch_impegni", "impegno",
                                "Domani: %s" % e.get("testo"), "Domani hai un impegno personale",
                                "Domani hai {n} impegni personali", e.get("testo")))
    if on["fin_contanti"]:
        text = "Segna le spese in contanti di oggi"
        out.append(_rem("finance:contanti:%s" % today.isoformat(), "fin_contanti", "contanti", text, text, text))
    if on["fin_settimana"] and today.weekday() == 6:
        text = "Dai un'occhiata alle spese della settimana"
        out.append(_rem("finance:settimana:%d-W%02d" % today.isocalendar()[:2], "fin_settimana", "settimana", text, text, text))
    if on["fin_appunti"]:
        for a in appunti or []:
            when = due_at(a.get("quando")) if a.get("stato") == "aperto" and a.get("promemoria") is True else None
            if when is not None and when <= now and a.get("id"):
                out.append(_rem("finance:appunto:%s:%s" % (a["id"], a["quando"]), "fin_appunti", "appunto", a.get("testo"),
                                "Hai un appunto da ricordare", "Hai {n} appunti da ricordare", a.get("testo")))
    return out


def group(reminders, reserved=True):
    """Notifiche da mandare: promemoria dello stesso gruppo uniti; [(titolo, testo, [chiavi])]."""
    out, order = {}, []
    for r in reminders:
        if r["gruppo"] not in out:
            order.append(r["gruppo"])
        out.setdefault(r["gruppo"], []).append(r)
    notes = []
    for g in order:
        rs = out[g]
        title = "CUORE · " + ("School" if rs[0]["tipo"].startswith("sch_") else "Finance")
        if len(rs) == 1:
            text = rs[0]["riservato"] if reserved else rs[0]["testo"]
        else:
            text = rs[0]["plurale"].format(n=len(rs))
            details = [r["dettaglio"] for r in rs if r.get("dettaglio")]
            if not reserved and details:
                text += ": " + ", ".join(dict.fromkeys(details))
        notes.append((title, text, [r["chiave"] for r in rs]))
    return notes


# ------------------------------------------------------------------------------------------------ stato e consegna
def _inside(child, parent):
    rel = os.path.relpath(child, parent)
    return rel == "." or not (rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel))


def outside_cloud_and_repo(path, ext, label):
    """Percorso assoluto `ext`, mai in iCloud o nel repository (ne' direttamente ne' tramite collegamenti)."""
    if not isinstance(path, str) or not os.path.isabs(path) or not path.endswith(ext):
        raise ConfigNonValida("%s: percorso assoluto di un file %s" % (label, ext))
    lib = os.path.join(os.path.expanduser("~"), "Library")
    real = os.path.realpath(path)
    for f in (os.path.join(lib, "Mobile Documents"), os.path.join(lib, "CloudStorage"), REPO_ROOT):
        if _inside(real, os.path.realpath(f)) or _inside(path, f):
            raise ConfigNonValida("%s in iCloud o nel repository: rifiutato" % label)
    return path


def check_state_path(path):
    """Il file di stato non va mai in iCloud o nel repository."""
    return outside_cloud_and_repo(path, ".json", "--stato")


def load_state(path):
    """(chiavi gia' mostrate, prime letture registrate per i promemoria «nuovo»)."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        ok = isinstance(raw, dict) and raw.get("schema") == STATE_SCHEMA
        shown, bases = (raw.get("mostrati"), raw.get("basi")) if ok else (None, None)
        clean = lambda d: {k: v for k, v in d.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(d, dict) else {}
        return clean(shown), {k: v for k, v in clean(bases).items() if k in BASELINE}
    except (OSError, ValueError):
        return {}, {}                             # stato assente o illeggibile: si riparte (al peggio un promemoria in piu')


def _atomic_json(path, doc):
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".promemoria-", suffix=".json", dir=d)      # creato 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def save_state(path, shown, now, bases=None):
    limit = (now - dt.timedelta(days=KEEP_DAYS)).isoformat()
    keep = {k: v for k, v in shown.items() if v >= limit}
    _atomic_json(path, {"schema": STATE_SCHEMA, "mostrati": dict(sorted(keep.items())), "basi": dict(sorted((bases or {}).items()))})


# ------------------------------------------------------------------------------------------------ impostazioni (ponte)
def read_settings(path):
    """(impostazioni canoniche, "presente"|"assente", errore | None). File non valido: il modello e l'errore."""
    if not os.path.lexists(path):
        return model_settings(), "assente", None
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            raise ConfigNonValida("non e' un file normale")
        with open(path, encoding="utf-8") as f:
            return normalize(json.load(f)), "presente", None
    except (OSError, ValueError) as e:
        return model_settings(), "non_valido", str(e) if isinstance(e, ConfigNonValida) else type(e).__name__


def write_settings(path, settings):
    """Scrittura atomica (file temporaneo 0600 + rename) delle impostazioni gia' validate."""
    _atomic_json(path, normalize(settings))


def update_settings(path, change, now=None):
    """Applica un cambiamento parziale `{attivo?, riservato?, promemoria?: {chiave: {attivo?, momenti?}}, soglie?}` e
    restituisce lo stato nuovo. File esistente non valido: rifiutato (si corregge a mano o va nel Cestino)."""
    if not isinstance(change, dict) or not change or set(change) - {"attivo", "riservato", "promemoria", "soglie"}:
        raise ConfigNonValida("modifica: almeno uno fra attivo, riservato, promemoria, soglie")
    cur, state, err = read_settings(path)
    if state == "non_valido":
        raise ConfigNonValida("impostazioni attuali non valide (%s): non modificate" % err)
    new = json.loads(json.dumps(cur))
    for k in ("attivo", "riservato"):
        if k in change:
            new[k] = change[k]
    rems = change.get("promemoria", {})
    if not isinstance(rems, dict) or set(rems) - set(KINDS):
        raise ConfigNonValida("promemoria: chiavi ammesse %s" % ", ".join(KINDS))
    for k, v in rems.items():
        if isinstance(v, bool):
            v = {"attivo": v}
        if not isinstance(v, dict) or not v or set(v) - {"attivo", "momenti"}:
            raise ConfigNonValida("%s: true|false oppure {attivo?, momenti?}" % k)
        new["promemoria"][k].update(v)
    if "soglie" in change:
        if not isinstance(change["soglie"], dict):
            raise ConfigNonValida("soglie: oggetto")
        new["soglie"].update(change["soglie"])
    write_settings(path, new)
    return settings_status(path, now)


def _at(day, moment):
    from zoneinfo import ZoneInfo
    h, m = MOMENTI[moment].split(":")
    return dt.datetime.combine(day, dt.time(int(h), int(m)), ZoneInfo(ZONE))


def next_check(kind, moments, now):
    """Prossimo orario dopo `now` in cui il promemoria puo' partire (parte solo se c'e' qualcosa da ricordare)."""
    for i in range(9):
        day = now.date() + dt.timedelta(days=i)
        for m in moments if kind != "fin_settimana" or day.weekday() == 6 else ():
            if _at(day, m) > now:
                return _at(day, m)
    return None


def settings_status(path, now=None):
    """Documento per le Impostazioni dell'app: ogni promemoria acceso/spento, momenti e prossimo controllo. Legge solo
    il file delle impostazioni (nessuna fonte di dati, nessuna scrittura)."""
    now = now or _now()
    settings, state, err = read_settings(path)
    rows = []
    for k, (area, title, _d, allowed, _m, proposal) in REMINDERS.items():
        r = settings["promemoria"][k]
        on = settings["attivo"] and state == "presente" and r["attivo"]
        nxt = next_check(k, r["momenti"], now) if on else None
        rows.append({"chiave": k, "area": area, "titolo": title, "acceso": r["attivo"], "momenti": r["momenti"],
                     "momenti_possibili": list(allowed), "proposta": proposal,
                     "prossimo_controllo": nxt.isoformat() if nxt else None})
    doc = {"schema": SETTINGS_SCHEMA, "file": state, "attivo": settings["attivo"] and state == "presente",
           "riservato": settings["riservato"], "momenti": dict(MOMENTI), "soglie": settings["soglie"], "promemoria": rows,
           "generato": now.isoformat(),
           "automazione": "orari del LaunchAgent cloud.cuore.promemoria; se e' caricato lo sa solo launchctl"}
    return dict(doc, errore=err) if err else doc


def appunto_send(settings, quando, now):
    """Quando partira' (o e' partito) il promemoria di un appunto: primo orario utile dalla sua data. None = motore o
    promemoria degli appunti spenti."""
    r = settings["promemoria"]["fin_appunti"]
    when = due_at(quando)
    if not (settings["attivo"] and r["attivo"]) or when is None:
        return None
    for i in range(2):
        for m in r["momenti"]:
            t = _at(when.date() + dt.timedelta(days=i), m)
            if t >= when:
                return t
    return None


SCRIPT = ("on run argv", "display notification (item 2 of argv) with title (item 1 of argv)", "end run")


def notify_mac(title, text, run=subprocess.run):
    """Notifica del Mac; i testi passano come argomenti (nessuna iniezione nello script). True se e' partita."""
    cmd = ["/usr/bin/osascript"]
    for line in SCRIPT:
        cmd += ["-e", line]
    try:
        return run(cmd + [title, text], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ------------------------------------------------------------------------------------------------ fonti reali
def real_school():
    import cuore_school
    return cuore_school.build_feed()


def real_money():
    import cuore_money
    return cuore_money.build_feed(cuore_money.db_path())


def personal(path):
    """Stato dell'archivio personale School (sola lettura). None = archivio illeggibile."""
    try:
        return school_personale.stato(path)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def notes(path):
    """Appunti Finance (sola lettura). None = archivio illeggibile."""
    try:
        return finance_appunti.stato(path)["appunti"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _source(feed, kind):
    if feed is None:
        return "non richiesta"
    ok = feed.get("sola_lettura_verificata") and (feed.get("pagine") if kind == "school" else feed.get("operazioni"))
    return "letta" if ok else "non disponibile (%d errori)" % len(feed.get("errori") or [])


def moment_of(now):
    """Momento dell'esecuzione: l'ultimo orario gia' passato oggi; None prima del primo."""
    t = now.strftime("%H:%M")
    return next((m for m in reversed(ALL) if MOMENTI[m] <= t), None)


def evaluate(cfg, now, school_fn, money_fn, personale, appunti=None):
    """(promemoria, fonti, fonti «nuovo» lette). Contano solo i promemoria accesi E previsti in questo momento; le
    fonti si leggono solo se servono."""
    today, moment = now.date(), moment_of(now)
    active = dict(cfg, on={k: cfg["on"][k] and moment in cfg["momenti"][k] for k in KINDS})
    on = active["on"]
    school = school_fn() if any(on[k] for k in FEED_KINDS["school"]) else None
    money = money_fn() if any(on[k] for k in FEED_KINDS["money"]) else None
    need_pers = on["sch_compiti"] or on["sch_compiti_sera"] or on["sch_impegni"]
    pers = personal(personale) if need_pers else {}
    appunti_list = notes(appunti) if on["fin_appunti"] and appunti else None
    done = set((pers or {}).get("fatti") or ())
    rems = (school_reminders(school, active, today, done, now) if school is not None else []) + \
           (finance_reminders(money, active, today) if money is not None else []) + \
        personal_reminders(active, now, (pers or {}).get("impegni"), appunti_list)
    sources = {"momento": moment or "nessuno (prima delle %s)" % MOMENTI["mattina"],
               "school": _source(school, "school"), "finance": _source(money, "money"),
               "personale_school": "non richiesto" if not need_pers else
               "illeggibile (si avvisa comunque)" if pers is None else "letto",
               "appunti_finance": "non richiesti" if not on["fin_appunti"] else
               "illeggibili" if appunti_list is None else "letti (%d)" % len(appunti_list)}
    ready = {k for k in BASELINE if on[k] and school is not None and baseline_ready(school, k)}
    return rems, sources, ready


def plan(rems, shown, bases, ready, now):
    """(da notificare, registrati senza notifica, prime letture nuove). Un promemoria «nuovo» (voti, comunicazioni) senza
    prima lettura registra lo storico senza notifiche; spegnerlo cancella la prima lettura (alla riaccensione si
    riparte dallo stato di allora, senza valanghe)."""
    new = [r for r in rems if r["chiave"] not in shown]
    fresh = {k: now.isoformat() for k in ready if k not in bases}
    silent = [r for r in new if r["tipo"] in BASELINE and r["tipo"] not in bases]
    return [r for r in new if r not in silent], silent, fresh


# ------------------------------------------------------------------------------------------------ comandi
def _now(today=None, hour=None):
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(ZONE)
    t = dt.time.fromisoformat(hour) if hour else None
    if today:
        return dt.datetime.combine(dt.date.fromisoformat(today), t or dt.time(12, 0), zone)
    now = dt.datetime.now(zone).replace(microsecond=0)
    return now.replace(hour=t.hour, minute=t.minute, second=0) if t else now


def _feed_file(path):
    def read():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return read


def run(argv=None, school_fn=None, money_fn=None, notify=notify_mac, out=print):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("comando", choices=("prova", "esegui"))
    ap.add_argument("--config", default=CONFIG_DEFAULT, help="file JSON delle impostazioni (assente = spento)")
    ap.add_argument("--stato", default=STATE_DEFAULT, help="file dei promemoria gia' mostrati (fuori da iCloud)")
    ap.add_argument("--school-personale", default=SCHOOL_PERSONALE, help="archivio personale School (sola lettura)")
    ap.add_argument("--finance-appunti", default=FINANCE_APPUNTI, help="appunti Finance (sola lettura)")
    ap.add_argument("--riservato", action=argparse.BooleanOptionalAction, default=None,
                    help="testi senza importi ne' dettagli; predefinito: \"riservato\" del file (vero se manca)")
    ap.add_argument("--oggi", help="data AAAA-MM-GG al posto di oggi (prove)")
    ap.add_argument("--ora", help="ora HH:MM al posto di adesso (prove; con --oggi predefinita 12:00)")
    ap.add_argument("--feed-school", help="feed School gia' calcolato, da file JSON (prove su dati sintetici)")
    ap.add_argument("--feed-money", help="feed Money gia' calcolato, da file JSON (prove su dati sintetici)")
    a = ap.parse_args(argv)
    school_fn = school_fn or (_feed_file(a.feed_school) if a.feed_school else real_school)
    money_fn = money_fn or (_feed_file(a.feed_money) if a.feed_money else real_money)
    cfg, why = load_config(os.path.expanduser(a.config))
    try:
        state_path = check_state_path(os.path.expanduser(a.stato))
    except ConfigNonValida as e:
        out("Promemoria NON eseguiti: %s" % e)
        return 2
    now = _now(a.oggi, a.ora)
    sources = (os.path.expanduser(a.school_personale), os.path.expanduser(a.finance_appunti))
    if a.comando == "esegui":
        if cfg is None:
            out("Promemoria %s." % why)
            return 0
        reserved = cfg["riservato"] if a.riservato is None else a.riservato
        rems, info, ready = evaluate(cfg, now, school_fn, money_fn, *sources)
        shown, bases = load_state(state_path)
        dropped = [k for k in bases if not cfg["on"][k]]
        for k in dropped:
            del bases[k]
        new, silent, fresh = plan(rems, shown, bases, ready, now)
        shown.update({r["chiave"]: now.isoformat() for r in silent})
        bases.update(fresh)
        notes_, sent = group(new, reserved), 0
        for title, text, keys in notes_:
            if notify(title, text):
                sent += 1
                shown.update({k: now.isoformat() for k in keys})
        if sent or silent or fresh or dropped:
            save_state(state_path, shown, now, bases)
        out(json.dumps({"promemoria": len(rems), "nuovi": len(new), "prima_lettura": len(silent), "notifiche": sent,
                        "fonti": info}, ensure_ascii=False))
        return 0 if sent == len(notes_) else 1
    preview = cfg is None
    cfg = cfg or default_config()
    reserved = cfg["riservato"] if a.riservato is None else a.riservato
    rems, info, ready = evaluate(cfg, now, school_fn, money_fn, *sources)
    shown, bases = load_state(state_path)
    new, silent, _fresh = plan(rems, shown, {k: v for k, v in bases.items() if cfg["on"][k]}, ready, now)
    out("PROVA dei promemoria di CUORE per il %s alle %s (nessuna notifica, nessuno stato scritto)"
        % (now.strftime("%d/%m/%Y"), now.strftime("%H:%M")))
    chosen = [] if preview else [k for k in KINDS if cfg["on"][k]]
    out("Stato: %s" % ("%s; anteprima con tutti i promemoria accesi e le soglie predefinite" % why if preview else
                       "acceso (%s)" % (", ".join(chosen) or "nessun promemoria scelto")))
    out("Fonti: " + "; ".join("%s %s" % kv for kv in info.items()))
    out("Testi: %s" % ("riservati (senza importi ne' dettagli)" if reserved else "con i dettagli"))
    if silent:
        out("Prima lettura di voti/comunicazioni: %d elementi registrati come gia' visti, senza notifica" % len(silent))
    notes_ = group(new, reserved)
    out("Notifiche che partirebbero: %d" % len(notes_))
    for title, text, keys in notes_:
        out("  - %s: %s  [%d promemoria]" % (title, text, len(keys)))
    old = [r for r in rems if r["chiave"] in shown]
    if old:
        out("Gia' mostrati (non si ripetono): %d" % len(old))
    return 0


if __name__ == "__main__":
    sys.exit(run())
