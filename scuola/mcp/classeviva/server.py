#!/usr/bin/env python3
"""Server MCP locale "classeviva": strumenti di SOLA LETTURA sul registro elettronico."""
from __future__ import annotations

import collections
import datetime as dt
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "_common"))

import cls_auth as cauth  # noqa: E402
import cls_client as ccl  # noqa: E402
import cls_registry as cls  # noqa: E402
import cvv_client as cv  # noqa: E402
import cvv_dryrun as dry  # noqa: E402
import cvv_sync as sync  # noqa: E402
import registry as regmod  # noqa: E402
import school_agent as sagent  # noqa: E402
import school_context as sctx  # noqa: E402
import school_context3 as sctx3  # noqa: E402
import cvv_cls_links as links  # noqa: E402
import cvv_comms as comms  # noqa: E402
import school_context4 as sctx4  # noqa: E402
import school_status as sstatus  # noqa: E402
import school_priority as spriority  # noqa: E402
import school_action as saction  # noqa: E402
import school_study_context as sstudy  # noqa: E402
import school_study_assistant as sassist  # noqa: E402
import school_study_session as ssession  # noqa: E402
import school_records as srecords  # noqa: E402
import school_materials_catalog as scatalog  # noqa: E402
import school_dashboard as sdash  # noqa: E402
import school_material_store as smstore  # noqa: E402
import cvv_subject as subj  # noqa: E402
import cvv_attribution as attr  # noqa: E402
import cvv_registry as cv_reg  # noqa: E402
from cloud_mcp import Server  # noqa: E402

INSTR = ("ClasseViva = fonte primaria dei dati scolastici (sola lettura). "
         "'kind' (verifica/compito/evento) e' DEDOTTO da codice evento e testo. "
         "La materia e' determinata solo con criteri verificabili (campo subject_criterion); se manca e' "
         "'Materia da verificare' con i candidati: non indovinarla. "
         "Strumenti cls_* = Google Classroom, fonte SEPARATA (sola lettura, registro con source=classroom): "
         "non fondere mai automaticamente i record di Classroom e di ClasseViva. "
         "Agenti di materia: contesto di avvio = school_brief(subject); dettagli con school_tasks, school_task, "
         "school_announcements, school_materials (sola lettura, solo corsi confermati della materia). "
         "school_context e' la vista DIAGNOSTICA completa, non il contesto di un agente.")

srv = Server("classeviva", "0.6.0", INSTR)
_client = None
_subjects = None


def client():
    global _client
    if _client is None:
        user, pw = cv.load_credentials()
        _client = cv.Client(user, pw)
        _client.login()
    return _client


def subjects():
    global _subjects
    if _subjects is None:
        _subjects = subj.build_subjects(client().subjects())
    return _subjects


def _d(s, default):
    if not s:
        return default
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        raise ValueError("data non valida '%s' (usa AAAA-MM-GG)" % s)


def _window(from_date, to_date, days_default):
    b = _d(from_date, dt.date.today())
    e = _d(to_date, b + dt.timedelta(days=days_default))
    if e < b:
        raise ValueError("'to_date' precede 'from_date'")
    return b, e


def _events(from_date, to_date, days_default):
    b, e = _window(from_date, to_date, days_default)
    subs = subjects()
    items = [cv.enrich_event(cv.normalize_event(x), subs) for x in client().agenda(b, e)]
    items.sort(key=lambda i: (i["start"], i["uid"]))
    return b, e, items


def _by_subject(items, kind, subject):
    sel = [i for i in items if i["kind"] == kind]
    if not subject:
        return sel, []
    return subj.subject_filter(sel, subject)


DATES = {"from_date": {"type": "string", "description": "AAAA-MM-GG (default: oggi)"},
         "to_date": {"type": "string", "description": "AAAA-MM-GG (default: from + giorni di default)"}}
SUBJ = {"subject": {"type": "string", "description": "es. filosofia. Le voci con materia da verificare "
                    "che potrebbero corrispondere sono in 'da_verificare'"}}


@srv.tool("cv_status", "Verifica login e account ClasseViva (nessun dato sensibile restituito).")
def cv_status():
    c = client()
    return {"ok": True, "nome": c.first_name, "student_id_cifre": len(c.student_id or "")}


@srv.tool("cv_agenda", "Agenda ClasseViva normalizzata (compiti, verifiche, eventi) con materia e criterio. "
          "Default: oggi -> +14 giorni.", DATES)
def cv_agenda(from_date=None, to_date=None):
    b, e, items = _events(from_date, to_date, 14)
    return {"from": str(b), "to": str(e), "count": len(items), "items": items}


@srv.tool("cv_tests", "Verifiche (dedotte) nell'intervallo, opzionalmente per materia. "
          "Default: oggi -> +30 giorni.", dict(DATES, **SUBJ))
def cv_tests(from_date=None, to_date=None, subject=None):
    b, e, items = _events(from_date, to_date, 30)
    ok, maybe = _by_subject(items, "verifica", subject)
    return {"from": str(b), "to": str(e), "subject": subject, "count": len(ok), "items": ok,
            "da_verificare": maybe}


@srv.tool("cv_homework", "Compiti (agenda, codice AGHW) nell'intervallo, opzionalmente per materia. "
          "Doppia lettura: agenda completa + endpoint filtrato per codice. Default: oggi -> +7 giorni.",
          dict(DATES, **SUBJ))
def cv_homework(from_date=None, to_date=None, subject=None):
    b, e = _window(from_date, to_date, 7)
    c, subs = client(), subjects()
    via_all = [x for x in c.agenda(b, e) if x.get("evtCode") == "AGHW"]
    notes = []
    try:
        via_code = c.agenda(b, e, code="AGHW")
    except cv.CVError as exc:
        via_code, notes = [], ["endpoint per codice AGHW non utilizzabile: %s" % exc]
    merged = {x["evtId"]: x for x in via_all}
    merged.update({x["evtId"]: x for x in via_code})
    items = [cv.enrich_event(cv.normalize_event(x), subs) for x in merged.values()]
    items.sort(key=lambda i: (i["start"], i["uid"]))
    for i in items:
        i["kind"] = "compito"
    ok, maybe = (items, []) if not subject else subj.subject_filter(items, subject)
    return {"from": str(b), "to": str(e), "subject": subject, "count": len(ok), "items": ok,
            "da_verificare": maybe,
            "fonti": {"agenda_completa_AGHW": len(via_all), "endpoint_per_codice_AGHW": len(via_code)},
            "note": notes}


@srv.tool("cv_next_test", "Prossima verifica (dedotta) di una materia nei prossimi 120 giorni.",
          {"subject": {"type": "string", "description": "es. filosofia"}}, ["subject"])
def cv_next_test(subject):
    b, e, items = _events(None, None, 120)
    ok, maybe = _by_subject(items, "verifica", subject)
    return {"subject": subject, "searched": [str(b), str(e)], "next": ok[0] if ok else None,
            "others": ok[1:5], "da_verificare": maybe}


@srv.tool("cv_subjects", "Elenco materie e docenti (con la chiave interna usata per risolvere la materia).")
def cv_subjects():
    out = [{"id": s["id"], "materia": s["desc"], "chiave": s["key"], "docenti": s["docenti_nomi"]}
           for s in subjects()]
    return {"count": len(out), "subjects": out}


@srv.tool("cv_lessons", "Lezioni svolte (materia, docente, argomento) in un intervallo. Default: ultimi 7 giorni.",
          DATES)
def cv_lessons(from_date=None, to_date=None):
    today = dt.date.today()
    b = _d(from_date, today - dt.timedelta(days=7))
    e = _d(to_date, today)
    out = [{"data": (x.get("evtDate") or "")[:10], "ora": x.get("evtHPos"), "durata": x.get("evtDuration"),
            "materia": x.get("subjectDesc"), "docente": x.get("authorName"),
            "tipo": x.get("lessonType"), "argomento": x.get("lessonArg")} for x in client().lessons(b, e)]
    return {"from": str(b), "to": str(e), "count": len(out), "lessons": out}


@srv.tool("cv_grades", "Voti, opzionalmente per materia. 'raw' = record ESATTO di ClasseViva (tutti i campi); "
          "'interpreted' = solo tabelle verificate (oggi nessuna: tipo_valutazione = Da verificare); il peso non "
          "viene mai dedotto. Se la risposta non ha la chiave attesa e' un errore (mai uno 0 silenzioso).",
          {"subject": {"type": "string"}})
def cv_grades(subject=None):
    items = [cv.normalize_grade(g) for g in client().grades()]
    items.sort(key=lambda i: (str(i["date"] or ""), i["uid"]))
    if subject:
        probe = [{"subject": i["raw"].get("subjectDesc"), "subject_candidates": [], "_i": i} for i in items]
        ok, _ = subj.subject_filter(probe, subject)
        items = [x["_i"] for x in ok]
    return {"count": len(items), "grades": items}


@srv.tool("cv_noticeboard", "Comunicazioni in bacheca. 'raw' = record ESATTO di ClasseViva; il testo NON viene letto "
          "(la lettura potrebbe segnare la comunicazione come letta). Categoria: 'Da classificare' se non fornita.",
          {"limit": {"type": "integer", "description": "default: tutte"}})
def cv_noticeboard(limit=None):
    items = [cv.normalize_notice(n) for n in client().noticeboard()]
    items.sort(key=lambda i: (i["date"] or "", i["uid"]), reverse=True)
    if limit:
        items = items[: int(limit)]
    return {"count": len(items), "items": items}


@srv.tool("cv_absences", "Assenze, ritardi e uscite anticipate. 'raw' = valori ESATTI di ClasseViva (id, codice, data, "
          "ora, valore, ore di assenza, giustificata, stato giustificazione web, motivo); 'interpreted' = solo tabelle verificate sulla UI web (ABR0, ABA0, webJustifStatus 0/1, ore perse), altrimenti 'Da verificare'; "
          "'derived' = nome. L'hash copre solo i dati grezzi.")
def cv_absences():
    items = [cv.normalize_absence(e) for e in client().absences()]
    items.sort(key=lambda i: (i["date"] or "", i["uid"]))
    return {"count": len(items), "events": items}


def _agenda_in_range(c, b, e):
    """Agenda (tutti i codici + AGHW) in una finestra limitata all'anno scolastico. Se ClasseViva
    rifiuta ancora l'intervallo, usa quello che indica nel messaggio e riprova una volta."""
    ys, ye = cv.school_year_range(dt.date.today())
    b, e = max(b, ys), min(e, ye)
    for attempt in (1, 2):
        try:
            return b, e, c.agenda(b, e), c.agenda(b, e, code="AGHW")
        except cv.CVError as exc:
            rng = cv.parse_valid_range(exc)
            if attempt == 2 or not rng:
                raise
            b, e = max(b, rng[0]), min(e, rng[1])


@srv.tool("cv_diagnostics",
          "Diagnostica di sola lettura: per ogni endpoint dice se la chiave attesa e' presente e quanti "
          "elementi ci sono, cosi' si distingue 'zero risultati' da 'endpoint non disponibile'. "
          "Include conteggio per codice evento, confronto compiti e coppie docente->materia dalle lezioni.")
def cv_diagnostics():
    c = client()
    today = dt.date.today()
    res = {"data": str(today), "endpoint": {}}

    def probe(name, fn):
        try:
            res["endpoint"][name] = {"ok": True, "elementi": len(fn())}
        except cv.CVError as exc:
            res["endpoint"][name] = {"ok": False, "errore": str(exc)[:200]}
    probe("subjects", c.subjects)
    probe("grades", c.grades)
    probe("absences/details", c.absences)
    probe("noticeboard", c.noticeboard)
    try:
        res["periodi"] = [{"periodo": p.get("periodDesc"), "inizio": p.get("dateStart"), "fine": p.get("dateEnd"),
                           "finale": p.get("isFinal")} for p in c.periods()]
    except cv.CVError as exc:
        res["periodi"] = "errore: %s" % str(exc)[:160]
    b, e = today - dt.timedelta(days=45), today + dt.timedelta(days=90)
    try:
        b, e, allev, by_code = _agenda_in_range(c, b, e)
        in_all = [x for x in allev if x.get("evtCode") == "AGHW"]
        res["agenda"] = {"finestra": [str(b), str(e)], "totale": len(allev),
                         "per_codice": dict(collections.Counter(x.get("evtCode") for x in allev)),
                         "con_subjectDesc": sum(1 for x in allev if x.get("subjectDesc"))}
        res["compiti"] = {"agenda_completa_AGHW": len(in_all), "endpoint_per_codice_AGHW": len(by_code),
                          "coerenti": len(in_all) == len(by_code)}
    except cv.CVError as exc:
        res["agenda"] = {"errore": str(exc)[:240]}
        res["compiti"] = {"errore": str(exc)[:240]}
    try:
        les = c.lessons(max(today - dt.timedelta(days=14), cv.school_year_range(today)[0]), today)
        pairs = collections.Counter((x.get("authorName"), x.get("subjectDesc")) for x in les)
        res["lezioni"] = {"ultimi_14_giorni": len(les),
                          "coppie_docente_materia": [{"docente": a, "materia": m, "n": n} for (a, m), n in sorted(pairs.items(), key=str)]}
    except cv.CVError as exc:
        res["lezioni"] = {"errore": str(exc)[:200]}
    return res


def _registry_fetchers():
    """Letture per il registro (le stesse per dry-run e sync): kind -> (funzione di lettura, finestra | None)."""
    c = client()
    ys, ye = cv.school_year_range(dt.date.today())
    return {"agenda_event": (lambda: _agenda_in_range(c, ys, ye)[2], (str(ys), str(ye))),
            "grade": (c.grades, None), "absence": (c.absences, None), "notice": (c.noticeboard, None),
            # lezioni: dall'inizio dell'anno a oggi (opt-in, non nei default); finestra = stessa, nessun "missing" nel futuro
            "lesson": (lambda: c.lessons(ys, min(ye, dt.date.today())), (str(ys), str(min(ye, dt.date.today()))))}


@srv.tool("cv_registry_dryrun",
          "DRY-RUN live del registro locale Scuola: legge ClasseViva (sola lettura) e mostra cosa farebbe il registro "
          "(create/update/unchanged/reinterpret/missing/anomalie) SENZA scrivere nulla (nemmeno nel SQLite reale). "
          "Il risultato ha due sezioni separate: RISULTATO_REALE e SIMULAZIONE_NON_ACCADUTA (scenari finti su una copia).",
          {"kinds": {"type": "array", "items": {"type": "string", "enum": ["agenda_event", "grade", "absence", "notice", "lesson"]},
                     "description": "default: tutti"},
           "simulate": {"type": "boolean", "description": "default true: aggiunge la simulazione (NON accaduta)"},
           "second_read": {"type": "boolean", "description": "default false: rilegge una seconda volta dal vivo"}})
def cv_registry_dryrun(kinds=None, simulate=True, second_read=False):
    return dry.run(_registry_fetchers(), kinds=kinds, simulate=simulate, second_read=second_read)


@srv.tool("cv_registry_sync",
          "Sincronizzazione MANUALE ClasseViva -> registro locale Scuola. DEFAULT = simulazione: scrive nel registro "
          "reale SOLO con commit=true (booleano). Non scrive su ClasseViva o Classroom. Non cancella mai.",
          {"kinds": {"type": "array", "items": {"type": "string", "enum": ["agenda_event", "grade", "absence", "notice", "lesson"]},
                     "description": "default: tutti"},
           "commit": {"type": "boolean", "description": "default false. true = scrive nel registro reale"},
           "expect_create": {"type": "integer",
                             "description": "con commit=true: scrive solo se i record da creare sono esattamente questi"},
           "migrate": {"type": "boolean", "description": "default false. true = consente la migrazione di schema del registro (snapshot dedicato e verificato prima); senza, un registro piu' vecchio rifiuta il commit"},
           "prune_backups": {"type": "boolean", "description": "default false. true = dopo un commit riuscito elimina gli snapshot oltre la retention (ultimi 10 e 30 giorni; 'premigrazione' mai); senza, la retention e' solo un piano"},
           })
def cv_registry_sync(kinds=None, commit=False, expect_create=None, migrate=False, prune_backups=False):
    return sync.run(_registry_fetchers(), kinds=kinds, commit=commit, expect_create=expect_create,
                    migrate=migrate, prune_backups_=prune_backups)


@srv.tool("cv_attribution_report",
          "REPORT DI ATTRIBUZIONE MATERIA (M2, SOLA LETTURA) per lezioni e agenda ClasseViva: per ogni record materia dichiarata "
          "(materia_api), materia derivata, criterio, confidenza, evidenze, conflitti e casi da_verificare. Legge i record dal "
          "registro locale (copia in sola lettura) e l'elenco docenti-materie da ClasseViva /subjects. Non scrive nulla "
          "(ne' registro, ne' file) e non cambia il significato di cv_registry_dryrun. Il subjectDesc originale "
          "non viene mai modificato; l'evidenza lezione->evento e' solo corroborativa.",
          {"dettaglio": {"type": "boolean", "description": "default false: aggiunge l'elenco di TUTTI i record con evidenze"}})
def cv_attribution_report(dettaglio=False):
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s)" % path)
    before = dry._fs_state(path)
    reg = regmod.Registry.from_snapshot(path)
    try:
        def raws(kind):
            return [reg.get_record(cv_reg.SOURCE, kind, r["source_uid"])["raw"]
                    for r in reg.list_records(source=cv_reg.SOURCE, kind=kind, status=regmod.PRESENT)]
        lessons, events = raws("lesson"), raws("agenda_event")
    finally:
        reg.close()
    rep = attr.build_report(lessons, events, subjects(), dettaglio=bool(dettaglio))
    rep["scritture"] = {"sqlite_reale": False, "classeviva": False, "classroom": False}
    rep["verifica_nessuna_scrittura"] = {"file_modificati": before != dry._fs_state(path)}
    rep["fonte"] = {"registro": path, "lezioni_nel_registro": len(lessons), "eventi_nel_registro": len(events)}
    return rep


def _cls_fetchers():
    """Letture Classroom per il registro: client nuovo a ogni chiamata (cache corsi per esecuzione)."""
    return ccl.make_fetchers(ccl.ClassroomClient(cauth.token_provider()))


@srv.tool("cls_status",
          "Stato dell'autorizzazione Google Classroom (sola lettura): controlla il Keychain (client id/secret e refresh "
          "token presenti?) e, con online=true, prova davvero il rinnovo del token mostrando gli scope attivi. "
          "Non legge dati di Classroom e non mostra mai token o secret.",
          {"online": {"type": "boolean", "description": "default false: nessuna chiamata di rete"}})
def cls_status(online=False):
    return cauth.status(online=bool(online), out=lambda _s: None)


@srv.tool("cls_registry_dryrun",
          "DRY-RUN live Google Classroom -> registro locale (source=classroom, separato da ClasseViva): legge Classroom "
          "(SOLA LETTURA, solo GET) e mostra cosa farebbe il registro SENZA scrivere nulla. Stesse due sezioni di "
          "cv_registry_dryrun: RISULTATO_REALE e SIMULAZIONE_NON_ACCADUTA. Richiede l'autorizzazione (cls_status).",
          {"kinds": {"type": "array", "items": {"type": "string", "enum": list(cls.ALL_KINDS)}, "description": "default: tutti"},
           "simulate": {"type": "boolean", "description": "default true"},
           "second_read": {"type": "boolean", "description": "default false: rilegge una seconda volta dal vivo"}})
def cls_registry_dryrun(kinds=None, simulate=True, second_read=False):
    return dry.run(_cls_fetchers(), kinds=kinds, simulate=simulate, second_read=second_read, adapter=cls)


@srv.tool("cls_registry_sync",
          "Sincronizzazione MANUALE Google Classroom -> registro locale (source=classroom). DEFAULT = simulazione: scrive "
          "nel registro reale SOLO con commit=true (booleano). Non scrive su Classroom o ClasseViva. Non cancella mai.",
          {"kinds": {"type": "array", "items": {"type": "string", "enum": list(cls.ALL_KINDS)}, "description": "default: tutti"},
           "commit": {"type": "boolean", "description": "default false. true = scrive nel registro reale"},
           "expect_create": {"type": "integer",
                             "description": "con commit=true: scrive solo se i record da creare sono esattamente questi"},
           "migrate": {"type": "boolean", "description": "default false. true = consente la migrazione di schema del registro (snapshot dedicato e verificato prima); senza, un registro piu' vecchio rifiuta il commit"},
           "prune_backups": {"type": "boolean", "description": "default false. true = dopo un commit riuscito elimina gli snapshot oltre la retention (ultimi 10 e 30 giorni; 'premigrazione' mai); senza, la retention e' solo un piano"},
           })
def cls_registry_sync(kinds=None, commit=False, expect_create=None, migrate=False, prune_backups=False):
    return sync.run(_cls_fetchers(), kinds=kinds, commit=commit, expect_create=expect_create, adapter=cls,
                    migrate=migrate, prune_backups_=prune_backups)


@srv.tool("school_registry_migrate",
          "MIGRAZIONE DI SCHEMA ESPLICITA del registro locale (M3). NON legge ClasseViva/Classroom, NON registra run, NON "
          "tocca i dati applicativi. Con confirm=false (default) mostra solo cosa farebbe. Con confirm=true: snapshot "
          "'premigrazione' verificato, migrazione v1->v2, verifica a connessione nuova (contenuti identici, nuove colonne, "
          "trigger, integrita'). Se l'ordine fallisce, non migra.",
          {"confirm": {"type": "boolean", "description": "default false = anteprima. true = esegue la migrazione"}})
def school_registry_migrate(confirm=False):
    return sync.migrate_only(confirm=confirm)


@srv.tool("school_freshness",
          "FRESCHEZZA DEI DATI (M3, SOLA LETTURA) derivata dai run del registro locale: per ogni fonte e kind ultimo run "
          "(inizio, fine, durata, esito), ultima lettura riuscita, ultimo errore, errori consecutivi, eta' in ore e flag "
          "stale con motivo (mai_sincronizzato, nessuna_lettura_riuscita, oltre_soglia, ultimo_run_in_errore, "
          "ultima_lettura_con_rifiuti). Un dry-run NON aggiorna la freschezza. Legge una copia in sola lettura: non scrive nulla.",
          {"soglia_ore": {"type": "integer", "description": "default 48 (provvisoria)"}})
def school_freshness(soglia_ore=None):
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s)" % path)
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Europe/Rome")).replace(microsecond=0).isoformat()     # l'orologio si legge solo qui
    before = dry._fs_state(path)
    reg = regmod.Registry.from_snapshot(path)
    try:
        out = reg.freshness(now, stale_after_hours=soglia_ore)
        out["schema"] = {"su_disco": regmod.stored_version(path), "atteso": regmod.SCHEMA_VERSION,
                         "copia_in_memoria_migrata": reg.migrated_in_memory}
    finally:
        reg.close()
    out["scritture"] = {"sqlite_reale": False, "classeviva": False, "classroom": False}
    out["verifica_nessuna_scrittura"] = {"file_modificati": before != dry._fs_state(path)}
    return out


@srv.tool("school_context",
          "DIAGNOSTICA (non per gli agenti: usa school_brief). CONTESTO COMPLETO PER MATERIA (sola lettura), schema "
          "cloud.school.context/2, output grande: tutto il corpus operativo per UNA delle 8 materie (vedi school_subjects), SOLO perimetro operativo 3a/4a classe "
          "(lo storico non e' esposto). Agenda/verifiche/voti ClasseViva; Classroom da L3 (cls_derive): compiti con la "
          "consegna collegata, stato_operativo, anno e perimetro. Solo i corsi CONFERMATI in school_manual.json "
          "appartengono alla materia; classroom_proposto contiene proposte automatiche NON confermate. Letto dal "
          "registro locale (copia in sola lettura): non scrive nulla e non rilegge Classroom. Ogni elemento cita uid + versione + hash.",
          {"subject": {"type": "string", "description": "chiave o nome di una delle 8 materie (es. matematica, fisica, storia)"},
           "include_raw": {"type": "boolean", "description": "default false: aggiunge il RAW completo di ogni record"}},
          ["subject"])
def school_context(subject, include_raw=False):
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s): esegui prima cv_registry_sync" % path)
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Europe/Rome")).replace(microsecond=0)     # unico punto che legge l'orologio
    return sctx.build_from_path(path, subject, subjects(), now, include_raw=bool(include_raw))


@srv.tool("school_context_v3",
          "CONTESTO PER MATERIA v3 (M4, SOLA LETTURA, schema cloud.school.context/3; school_context v2 resta invariato). "
          "Sezioni lezioni / agenda / voti / classroom; in ogni record 'dati' (canonico, raw citato con uid+versione+hash) "
          "separati da 'interpretazione' (derivato: attribuzione di materia M2 con criterio, confidenza, evidenze, conflitti) e "
          "per ogni sezione 'freschezza' (operativo, M3: aggiornato / stale / senza_lettura, flag legacy; 'aggiornato' NON "
          "garantisce correttezza). Una materia e' assegnata a un record SOLO se M2 la determina: i casi incerti stanno in "
          "possibili_da_verificare, i conflitti restano visibili. Include warning strutturati e informazioni_mancanti. "
          "'docente' filtra lezioni ed eventi. Solo corsi Classroom CONFERMATI in school_manual.json; perimetro operativo "
          "3a/4a. Output grande: per l'avvio di un agente usa school_brief. Non scrive nulla.",
          {"subject": {"type": "string", "description": "chiave o nome di una delle 8 materie (es. matematica, fisica, storia)"},
           "include_raw": {"type": "boolean", "description": "default false: aggiunge il RAW completo di ogni record"},
           "docente": {"type": "string", "description": "facoltativo: filtra per docente (parte del nome)"}}, ["subject"])
def school_context_v3(subject, include_raw=False, docente=None):
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s): esegui prima cv_registry_sync" % path)
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Europe/Rome")).replace(microsecond=0)
    return sctx3.build_from_path(path, subject, subjects(), now, docente=docente or None, include_raw=bool(include_raw))


@srv.tool("school_overview",
          "VISIONE SINTETICA DELLO STATO SCOLASTICO (M4, SOLA LETTURA, schema cloud.school.overview/1): per le 8 materie "
          "lezioni, eventi di agenda, verifiche future e prossima verifica, compiti Classroom operativi/aperti, record "
          "da_verificare e conflitti di attribuzione (M2); freschezza per kind (M3); stato del registro; warning e "
          "informazioni_mancanti. E' un indice derivato: NON sostituisce il registro ne' i contesti per materia. Non scrive nulla.",
          {"soglia_ore": {"type": "integer", "description": "default 48 (provvisoria)"}})
def school_overview(soglia_ore=None):
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s)" % path)
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Europe/Rome")).replace(microsecond=0)
    before = dry._fs_state(path)
    out = sctx3.overview_from_path(path, subjects(), now, soglia_ore=soglia_ore)
    out["scritture"] = {"sqlite_reale": False, "classeviva": False, "classroom": False}
    out["verifica_nessuna_scrittura"] = {"file_modificati": before != dry._fs_state(path)}
    return out


@srv.tool("school_links",
          "COLLEGAMENTI E CONFLITTI ClasseViva <-> Classroom (M5, SOLA LETTURA, schema cloud.school.links/1). Interpretazioni DERIVATE e "
          "ricalcolate a ogni chiamata: il Registry resta l'unica fonte canonica e nessun record originale viene modificato. Relazioni: "
          "prepara (un elemento Classroom prepara una verifica: NON e' identita'), stesso_argomento, stessa_scadenza. Stati: probabile, "
          "proposto, conflitto (candidati o segnali in contrasto: nessuna scelta), non_determinabile (materia non determinata, tipico dei "
          "corsi condivisi senza topic); 'confermato' e' riservato e non viene mai prodotto. Ogni collegamento ha prove (codice/esito/peso), "
          "regola, materia dei due lati, freschezza e cita ENTRAMBI i lati con uid + registry_version + content_hash. Solo corsi Classroom "
          "confermati in school_manual.json. Filtri facoltativi. Non scrive nulla.",
          {"materia": {"type": "string", "description": "facoltativo: una delle 8 materie (filtra i collegamenti che la riguardano)"},
           "stato": {"type": "string", "description": "facoltativo: probabile | proposto | conflitto | non_determinabile | confermato"},
           "relazione": {"type": "string", "description": "facoltativo: prepara | stesso_argomento | stessa_scadenza"},
           "soglia_ore": {"type": "integer", "description": "default 48 (provvisoria)"}})
def school_links(materia=None, stato=None, relazione=None, soglia_ore=None):
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s)" % path)
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Europe/Rome")).replace(microsecond=0)
    before = dry._fs_state(path)
    out = links.build_from_path(path, subjects(), now, materia=materia or None, stato=stato or None,
                                relazione=relazione or None, soglia_ore=soglia_ore)
    out["scritture"] = {"sqlite_reale": False, "classeviva": False, "classroom": False}
    out["verifica_nessuna_scrittura"] = {"file_modificati": before != dry._fs_state(path)}
    return out


@srv.tool("school_link_explain",
          "SPIEGA I COLLEGAMENTI DI UN ELEMENTO (M5, SOLA LETTURA, schema cloud.school.links.explain/1): dato un uid ClasseViva "
          "(cvv:..., cvv:lesson:...), Classroom (cls:corso/id), un link_id o un conflitto_id, restituisce collegamenti, conflitti e casi "
          "non determinabili che lo riguardano, con prove, regola, criteri e freschezza. Se non c'e' nessun collegamento lo dice "
          "(risultato prudente) e distingue 'elemento considerato' da 'target non trovato'. Non scrive nulla.",
          {"target": {"type": "string", "description": "uid, link_id o conflitto_id"},
           "soglia_ore": {"type": "integer", "description": "default 48 (provvisoria)"}}, ["target"])
def school_link_explain(target, soglia_ore=None):
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s)" % path)
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Europe/Rome")).replace(microsecond=0)
    before = dry._fs_state(path)
    out = links.explain_from_path(path, subjects(), now, target, soglia_ore=soglia_ore)
    out["scritture"] = {"sqlite_reale": False, "classeviva": False, "classroom": False}
    out["verifica_nessuna_scrittura"] = {"file_modificati": before != dry._fs_state(path)}
    return out


@srv.tool("school_communications",
          "CLASSIFICAZIONE DELLE COMUNICAZIONI (M6, SOLA LETTURA, schema cloud.school.communications/1): circolari ClasseViva (bacheca) e "
          "annunci Classroom, classificati in modo DERIVATO e ricalcolato a ogni chiamata: il Registry resta l'unica fonte canonica e nessun "
          "record originale viene modificato. Classi (multi-etichetta): verifica, compito, attivita, organizzativa, avviso, materiale; "
          "attributi: ambito (generale|corso|materia), materia, date esplicite con ruolo, riferimenti relativi (rilevati, MAI risolti), "
          "validita' nativa. Stati: determinato (solo struttura nativa), probabile, proposto, conflitto (segnali in contrasto: nessuna scelta), "
          "non_determinabile (nessun indizio affidabile: es. un nome solo). I collegamenti M5 sono solo corroborazione. Default: perimetro "
          "operativo (3a/4a), circolari generali escluse dal filtro materia salvo includi_generali. Ogni voce cita uid + registry_version + "
          "content_hash e ha freschezza. dettaglio=true aggiunge prove e motivazioni. Non scrive nulla.",
          {"materia": {"type": "string", "description": "facoltativo: una delle 8 materie (solo comunicazioni con materia determinata)"},
           "classe": {"type": "string", "description": "facoltativo: verifica | compito | attivita | organizzativa | avviso | materiale (qualunque stato)"},
           "stato": {"type": "string", "description": "facoltativo: determinato | probabile | proposto | conflitto | non_determinabile"},
           "fonte": {"type": "string", "description": "facoltativo: classeviva | classroom"},
           "perimetro": {"type": "string", "description": "operativo (default, 3a/4a) | storico | tutti"},
           "dal": {"type": "string", "description": "facoltativo: data della comunicazione >= YYYY-MM-DD"},
           "al": {"type": "string", "description": "facoltativo: data della comunicazione <= YYYY-MM-DD"},
           "includi_generali": {"type": "boolean", "description": "con il filtro materia, includi anche le circolari generali (default false)"},
           "dettaglio": {"type": "boolean", "description": "true = prove e motivazioni complete (default false: compatto)"},
           "soglia_ore": {"type": "integer", "description": "default 48 (provvisoria)"}})
def school_communications(materia=None, classe=None, stato=None, fonte=None, perimetro=None, dal=None, al=None, includi_generali=False,
                          dettaglio=False, soglia_ore=None):
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s)" % path)
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Europe/Rome")).replace(microsecond=0)
    before = dry._fs_state(path)
    out = comms.build_from_path(path, subjects(), now, materia=materia or None, classe=classe or None, stato=stato or None,
                                fonte=fonte or None, perimetro=perimetro or "operativo", dal=dal or None, al=al or None,
                                includi_generali=bool(includi_generali), dettaglio=bool(dettaglio), soglia_ore=soglia_ore)
    out["scritture"] = {"sqlite_reale": False, "classeviva": False, "classroom": False}
    out["verifica_nessuna_scrittura"] = {"file_modificati": before != dry._fs_state(path)}
    return out


@srv.tool("school_communication_explain",
          "SPIEGA LA CLASSIFICAZIONE DI UNA COMUNICAZIONE (M6, SOLA LETTURA, schema cloud.school.communications.explain/1): dato un uid "
          "(cvv:<pubId> per le circolari, cls:corso/id per gli annunci) o un comm_id, restituisce la classificazione COMPLETA con prove "
          "(codice/esito/peso), regole, collegamenti M5 usati come corroborazione, conflitti, informazioni mancanti e freschezza. Considera "
          "tutti i perimetri. Se non e' trovata lo dice. Non scrive nulla.",
          {"target": {"type": "string", "description": "uid o comm_id"},
           "soglia_ore": {"type": "integer", "description": "default 48 (provvisoria)"}}, ["target"])
def school_communication_explain(target, soglia_ore=None):
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s)" % path)
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Europe/Rome")).replace(microsecond=0)
    before = dry._fs_state(path)
    out = comms.explain_from_path(path, subjects(), now, target, soglia_ore=soglia_ore)
    out["scritture"] = {"sqlite_reale": False, "classeviva": False, "classroom": False}
    out["verifica_nessuna_scrittura"] = {"file_modificati": before != dry._fs_state(path)}
    return out


@srv.tool("school_context_v4",
          "CONTESTO OPERATIVO DI UNA MATERIA (M7, SOLA LETTURA, schema cloud.school.context/4): aggregazione di M2/M4/M5/M6 per UNA "
          "delle 8 materie, senza nuove fonti, matcher o attribuzioni. Sezioni: lezioni recenti, verifiche (future + 7 giorni), compiti "
          "(aperti + scaduti da max 14 giorni), comunicazioni operative della materia, collegamenti ClasseViva-Classroom, "
          "possibili_da_verificare (MAI attribuiti), generali (circolari, non della materia). sintesi.prossimi_impegni solo con data "
          "futura utilizzabile; contatori = candidati totali prima dei limiti. Ogni elemento ha uid + hash; freshness, gap, ambiguita', "
          "provenance e regole per l'agente. Default compatto (testi <= 160 caratteri); dettaglio=true o lista di sezioni per testo "
          "completo, evidenze, hash completo e registry_version. Non scrive nulla.",
          {"materia": {"type": "string", "description": "chiave o nome di una delle 8 materie (es. matematica, fisica, storia)"},
           "dettaglio": {"type": ["boolean", "array"], "items": {"type": "string"},
                         "description": "false (default, compatto) | true (tutte le sezioni) | lista di sezioni: lezioni, verifiche, "
                                        "compiti, comunicazioni, collegamenti, possibili_da_verificare, generali"},
           "limiti": {"type": "object", "description": "facoltativo: {sezione|prossimi_impegni: intero 1..50}; oltre 50 normalizzato a 50"},
           "soglia_ore": {"type": "integer", "description": "soglia di freschezza in ore (default 48)"}}, ["materia"])
def school_context_v4(materia, dettaglio=False, limiti=None, soglia_ore=48):
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s): esegui prima cv_registry_sync" % path)
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Europe/Rome")).replace(microsecond=0)     # UNICA lettura dell'orologio: stesso now per tutto
    before = dry._fs_state(path)
    out = sctx4.build_from_path(path, materia, now, subjects=subjects(), dettaglio=dettaglio, limiti=limiti,
                                soglia_ore=48 if soglia_ore is None else soglia_ore)
    out["scritture"] = {"sqlite_reale": False, "classeviva": False, "classroom": False}
    out["verifica_nessuna_scrittura"] = {"file_modificati": before != dry._fs_state(path)}
    return out


@srv.tool("school_status_v1",
          "STATO OGGETTIVO DI UNA MATERIA (M8 v1, SOLA LETTURA, schema cloud.school.status/1): sopra il contesto M7 (candidati "
          "completi), per UNA delle 8 materie e per l'anno scolastico OPERATIVO (gli altri anni solo come 'storico' separato). "
          "Tre livelli: fatti osservabili, segnali deterministici (verifica in calendario, compiti aperti/scaduti, continuita' lezioni, "
          "argomenti recenti, preparazione collegata M5, comunicazioni, attribuzione Classroom, dati valutativi) con evidenze uid+hash, "
          "natura e affidabilita' (alta/media/bassa/non_determinabile con motivi), e valutazioni SEMPRE non_valutabile: nessun giudizio "
          "sul rendimento. Include copertura delle fonti, gap, ambiguita' e provenance. Non scrive nulla.",
          {"materia": {"type": "string", "description": "chiave o nome di una delle 8 materie (es. matematica, fisica, storia)"},
           "dettaglio": {"type": "boolean", "description": "true = aggiunge gli elementi M7 di ogni evidenza (default false)"},
           "soglia_ore": {"type": "integer", "description": "soglia di freschezza in ore (default 48)"}}, ["materia"])
def school_status_v1(materia, dettaglio=False, soglia_ore=48):
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s): esegui prima cv_registry_sync" % path)
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Europe/Rome")).replace(microsecond=0)     # UNICA lettura dell'orologio: stesso now per tutto
    before = dry._fs_state(path)
    out = sstatus.build_from_path(path, materia, now, subjects=subjects(), dettaglio=bool(dettaglio),
                                  soglia_ore=48 if soglia_ore is None else soglia_ore)
    out["scritture"] = {"sqlite_reale": False, "classeviva": False, "classroom": False}
    out["verifica_nessuna_scrittura"] = {"file_modificati": before != dry._fs_state(path)}
    return out


@srv.tool("school_priority_v1",
          "PRIORITA' OPERATIVE PRUDENTI (M9 v1, SOLA LETTURA, schema cloud.school.priority/1): sopra lo stato oggettivo M8, per UNA "
          "materia o (senza materia) per le 8 materie. Ogni voce ha regola (P1 verifica imminente, P1b indizio non datato, P3a/P3b "
          "compiti, P4 comunicazione recente; P2 aggiunge solo l'azione di preparazione), priorita' (nessuna | informativa | attenzione "
          "| alta | non_determinabile se una fonte non e' stata letta), affidabilita' separata e propagata da M8, motivo, evidenze M8 "
          "(uid+hash) e azioni minime (controllare_verifica, aprire_preparazione, controllare_compito, leggere_comunicazione, "
          "verificare_informazione). Nessun piano di studio, durata, giudizio o voto; lo storico non genera priorita'. Non scrive nulla.",
          {"materia": {"type": "string", "description": "facoltativo: una delle 8 materie; assente = panoramica delle 8"},
           "dettaglio": {"type": "boolean", "description": "true = aggiunge i segnali M8 usati e tutte le ambiguita' (default false)"},
           "soglia_ore": {"type": "integer", "description": "soglia di freschezza in ore (default 48)"}})
def school_priority_v1(materia=None, dettaglio=False, soglia_ore=48):
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s): esegui prima cv_registry_sync" % path)
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Europe/Rome")).replace(microsecond=0)     # UNICA lettura dell'orologio: stesso now per tutto
    before = dry._fs_state(path)
    out = spriority.build_from_path(path, now, subjects=subjects(), materia=materia or None, dettaglio=bool(dettaglio),
                                    soglia_ore=48 if soglia_ore is None else soglia_ore)
    out["scritture"] = {"sqlite_reale": False, "classeviva": False, "classroom": False}
    out["verifica_nessuna_scrittura"] = {"file_modificati": before != dry._fs_state(path)}
    return out


@srv.tool("school_action_v1",
          "AZIONI ASSISTIVE DISPONIBILI (M10 v1, SOLA LETTURA, schema cloud.school.action/1): sopra le priorita' M9, per UNA materia o "
          "(senza materia) per le 8. Per ogni azione M9 (controllare_verifica, aprire_preparazione, controllare_compito, "
          "leggere_comunicazione, verificare_informazione) restituisce il contesto gia' disponibile (risolto dalle evidenze M9 nel "
          "contratto M8 per uid+hash), l'eseguibilita' (eseguibile | parzialmente_eseguibile | non_eseguibile), i gap supportati dai dati, "
          "l'affidabilita' (mai sopra M9) e la provenienza fino al Registry. Priorita', affidabilita' ed eseguibilita' sono distinte. "
          "Non esegue nulla, non scrive nulla, nessun piano o giudizio.",
          {"materia": {"type": "string", "description": "facoltativo: una delle 8 materie; assente = tutte le 8"},
           "dettaglio": {"type": "boolean", "description": "true = aggiunge gli elementi M8 risolti di ogni azione (default false)"},
           "soglia_ore": {"type": "integer", "description": "soglia di freschezza in ore (default 48)"}})
def school_action_v1(materia=None, dettaglio=False, soglia_ore=48):
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s): esegui prima cv_registry_sync" % path)
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Europe/Rome")).replace(microsecond=0)     # UNICA lettura dell'orologio: stesso now per M9 e M8
    before = dry._fs_state(path)
    out = saction.build_from_path(path, now, subjects=subjects(), materia=materia or None, dettaglio=bool(dettaglio),
                                  soglia_ore=48 if soglia_ore is None else soglia_ore)
    out["scritture"] = {"sqlite_reale": False, "classeviva": False, "classroom": False}
    out["verifica_nessuna_scrittura"] = {"file_modificati": before != dry._fs_state(path)}
    return out


@srv.tool("school_study_context_v1",
          "CONTESTO DI STUDIO DI UNA MATERIA (M11 v1, SOLA LETTURA, schema cloud.school.study_context/1): NON e' uno Study Assistant. "
          "Per UNA materia (obbligatoria) raccoglie gli obiettivi (verifiche e compiti delle azioni M10, con data e priorita' M9 come "
          "ricevute), il testo dichiarato, i collegamenti M5 esposti con la loro copertura, le preparazioni collegate (mai certe), le "
          "lezioni dell'anno operativo come contesto della materia (mai legate agli obiettivi), le comunicazioni da leggere, le "
          "informazioni da verificare, le lacune e le frasi di 'informazione insufficiente'. Ogni dato e' classificato (disponibile, "
          "rilevante, sufficiente solo per un uso formale, non determinabile, non disponibile); gli argomenti di un obiettivo non sono "
          "mai determinati in v1. Nessun piano, tempo, giudizio o diagnosi. Non scrive nulla.",
          {"materia": {"type": "string", "description": "obbligatoria: una delle 8 materie (es. matematica, fisica, storia)"},
           "dettaglio": {"type": "boolean", "description": "true = aggiunge l'azione M10 completa di ogni obiettivo (default false)"},
           "soglia_ore": {"type": "integer", "description": "soglia di freschezza in ore (default 48)"}}, ["materia"])
def school_study_context_v1(materia, dettaglio=False, soglia_ore=48):
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s): esegui prima cv_registry_sync" % path)
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Europe/Rome")).replace(microsecond=0)     # UNICA lettura dell'orologio: stesso now per M10 e M8
    before = dry._fs_state(path)
    out = sstudy.build_from_path(path, materia, now, subjects=subjects(), dettaglio=bool(dettaglio),
                                 soglia_ore=48 if soglia_ore is None else soglia_ore)
    out["scritture"] = {"sqlite_reale": False, "classeviva": False, "classroom": False}
    out["verifica_nessuna_scrittura"] = {"file_modificati": before != dry._fs_state(path)}
    return out


@srv.tool("school_study_assistant_v1",
          "STUDY ASSISTANT v1 - ORCHESTRATORE/GUARDRAIL (M12, SOLA LETTURA, schema cloud.school.study_assistant/1): per UNA materia "
          "(obbligatoria) e, se indicato, un obiettivo (uid esatto di M11), stabilisce quali modalita' di assistenza sono ammesse "
          "(quadro_obiettivo, materiale_collegato, rassegna_lezioni, comunicazioni_da_leggere, chiarimento_limiti sempre disponibile), "
          "con i soli contenuti verificati di M11 (uid/hash), affidabilita' (mai sopra M11), limiti, frasi obbligatorie, azioni ammesse e "
          "azioni vietate. Non genera testo didattico: niente spiegazioni, esercizi, quiz, riassunti, piani, tempi o valutazioni. "
          "Una modalita' non disponibile non ha contenuti e indica solo le alternative. Non scrive nulla.",
          {"materia": {"type": "string", "description": "obbligatoria: una delle 8 materie (es. matematica, fisica, storia)"},
           "obiettivo": {"type": "string", "description": "facoltativo: uid ESATTO di un obiettivo M11 (es. cvv:1000101)"},
           "modalita": {"type": "string", "description": "facoltativa: quadro_obiettivo | materiale_collegato | rassegna_lezioni | "
                                                         "comunicazioni_da_leggere | chiarimento_limiti; assente = disponibilita' di tutte"},
           "dettaglio": {"type": "boolean", "description": "true = aggiunge l'elemento M11 completo di ogni contenuto (default false)"},
           "soglia_ore": {"type": "integer", "description": "soglia di freschezza in ore (default 48)"}}, ["materia"])
def school_study_assistant_v1(materia, obiettivo=None, modalita=None, dettaglio=False, soglia_ore=48):
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s): esegui prima cv_registry_sync" % path)
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Europe/Rome")).replace(microsecond=0)     # UNICA lettura dell'orologio: stesso now per M11 e sotto
    before = dry._fs_state(path)
    out = sassist.build_from_path(path, materia, now, subjects=subjects(), obiettivo=obiettivo or None, modalita=modalita or None,
                                  dettaglio=bool(dettaglio), soglia_ore=48 if soglia_ore is None else soglia_ore)
    out["scritture"] = {"sqlite_reale": False, "classeviva": False, "classroom": False}
    out["verifica_nessuna_scrittura"] = {"file_modificati": before != dry._fs_state(path)}
    return out


@srv.tool("school_study_session_v1",
          "SESSIONE DI STUDIO - ORCHESTRATORE/GUARDRAIL (M13, SOLA LETTURA, schema cloud.school.study_session/1): per UNA materia e UNA "
          "modalita' (spiega | interroga | esercitati | correggi | ripassa | prepara_obiettivo) stabilisce su quale contenuto si lavora e con "
          "quale provenienza: contenuto_fornito_utente (testo della richiesta, non verificato), contenuto_scolastico (solo riferimenti "
          "ESPLICITI a contenuti esposti da M12, per uid) e conoscenza_generale (aggiunta da Claude, da etichettare, mai attribuita alla "
          "scuola). Restituisce contesto scolastico M12, azioni ammesse/vietate, frasi obbligatorie e limiti. Non genera la risposta "
          "didattica; nessun voto, diagnosi, giudizio sulla preparazione, tempo, piano o programma inventato; nessuna acquisizione "
          "automatica di materiali. Non scrive nulla.",
          {"materia": {"type": "string", "description": "obbligatoria: una delle 8 materie"},
           "modalita": {"type": "string", "description": "spiega | interroga | esercitati | correggi | ripassa | prepara_obiettivo"},
           "contenuto": {"type": ["string", "object", "null"],
                         "description": "testo fornito dall'utente, oppure oggetto {testo?, origine_dichiarata?, riferimenti_m12?: [uid], "
                                        "risposta_utente? (per correggi)}"},
           "obiettivo": {"type": "string", "description": "facoltativo (obbligatorio per prepara_obiettivo): uid ESATTO di un obiettivo M12"},
           "dettaglio": {"type": "boolean", "description": "true = include il testo completo delle parti di contenuto (default false)"}},
          ["materia", "modalita", "contenuto"])
def school_study_session_v1(materia, modalita, contenuto, obiettivo=None, dettaglio=False):
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s): esegui prima cv_registry_sync" % path)
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Europe/Rome")).replace(microsecond=0)     # UNICA lettura dell'orologio: stesso now per M12 e sotto
    before = dry._fs_state(path)
    out = ssession.build_from_path(path, materia, modalita, contenuto, now, subjects=subjects(), obiettivo=obiettivo or None,
                                   dettaglio=bool(dettaglio))
    out["scritture"] = {"sqlite_reale": False, "classeviva": False, "classroom": False}
    out["verifica_nessuna_scrittura"] = {"file_modificati": before != dry._fs_state(path)}
    return out


@srv.tool("school_records_v1",
          "REGISTRO VALUTATIVO E PRESENZE (M14, SOLA LETTURA, schema cloud.school.records/1): fatti e statistiche DESCRITTIVE su voti e "
          "assenze dell'anno operativo (storico separato). Voti verbatim (displayValue, decimalValue, weightFactor, componente, periodo, "
          "annullato) con inclusione dichiarata; medie per POLITICA nominata con formula e uid usati (media_pesata_weightfactor primaria, "
          "media_aritmetica_semplice): corrispondenza con la media di ClasseViva NON verificata. Distingue fonte non letta, nessun voto, "
          "voti solo esclusi, media calcolabile/non calcolabile. Distribuzione e serie temporale; assenze per tipo (solo tabelle "
          "verificate) e per mese. Nessun giudizio, rendimento o previsione. Non scrive nulla.",
          {"materia": {"type": "string", "description": "facoltativa: chiave o nome di una materia ClasseViva (anche non tra le 8)"},
           "periodo": {"type": "string", "description": "facoltativo: periodDesc esatto (es. 'Primo periodo')"},
           "dettaglio": {"type": "boolean", "description": "true = include il record grezzo di ogni voto e assenza (default false)"},
           "soglia_ore": {"type": "integer", "description": "soglia di freschezza in ore (default 48)"}})
def school_records_v1(materia=None, periodo=None, dettaglio=False, soglia_ore=48):
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s): esegui prima cv_registry_sync" % path)
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Europe/Rome")).replace(microsecond=0)     # UNICA lettura dell'orologio
    before = dry._fs_state(path)
    out = srecords.build_from_path(path, now, subjects=subjects(), materia=materia or None, periodo=periodo or None,
                                   dettaglio=bool(dettaglio), soglia_ore=48 if soglia_ore is None else soglia_ore)
    out["scritture"] = {"sqlite_reale": False, "classeviva": False, "classroom": False}
    out["verifica_nessuna_scrittura"] = {"file_modificati": before != dry._fs_state(path)}
    return out


@srv.tool("school_materials_catalog_v1",
          "CATALOGO MATERIALI (M15, SOLA LETTURA, schema cloud.school.materials_catalog/1): materiali CITATI dalle fonti (allegati di "
          "materiali, compiti e annunci Classroom; allegati delle circolari ClasseViva) con titolo, link, id Drive, padre (uid, hash), "
          "stato_materiale (identificato_non_acquisito | non_disponibile: nulla e' acquisito a questo livello), attribuzione per elemento "
          "(determinata | possibile | non_determinabile | generale) separata dall'appartenenza al corso, associazioni a verifiche/lezioni "
          "solo tramite collegamenti M5 con il loro stato. Perimetro operativo (anno corrente) o storico, mai mescolati. Non scarica nulla, "
          "non legge contenuti, non dice che un materiale serve o basta. Non scrive nulla.",
          {"materia": {"type": "string", "description": "facoltativa: una delle 8 materie (determinata o possibile per quella materia)"},
           "obiettivo": {"type": "string", "description": "facoltativo: uid esatto di una verifica/lezione (solo materiali collegati via M5)"},
           "perimetro": {"type": "string", "description": "operativo (default, anno corrente) | storico"},
           "limite": {"type": "integer", "description": "riferimenti per pagina (default 50, massimo 200)"},
           "pagina": {"type": "integer", "description": "pagina (default 1)"},
           "dettaglio": {"type": "boolean", "description": "true = riferimento completo (padre, corso, criteri, associazioni)"},
           "soglia_ore": {"type": "integer", "description": "soglia di freschezza in ore (default 48)"}})
def school_materials_catalog_v1(materia=None, obiettivo=None, perimetro="operativo", limite=None, pagina=1, dettaglio=False, soglia_ore=48):
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s): esegui prima cv_registry_sync" % path)
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Europe/Rome")).replace(microsecond=0)     # UNICA lettura dell'orologio
    before = dry._fs_state(path)
    out = scatalog.build_from_path(path, now, subjects=subjects(), materia=materia or None, obiettivo=obiettivo or None,
                                   perimetro=perimetro or "operativo", limite=limite, pagina=pagina or 1, dettaglio=bool(dettaglio),
                                   soglia_ore=48 if soglia_ore is None else soglia_ore)
    out["scritture"] = {"sqlite_reale": False, "classeviva": False, "classroom": False, "drive": False}
    out["verifica_nessuna_scrittura"] = {"file_modificati": before != dry._fs_state(path)}
    return out


@srv.tool("school_dashboard_v1",
          "MODELLO DI VISTA DELLA DASHBOARD (M16, SOLA LETTURA, schema cloud.school.dashboard/1): UNA pagina della UI Cloud Scuola "
          "(home | attenzione | materie | materia | verifiche | compiti | voti | assenze | studio | materiali | storico | qualita_dati) "
          "composta solo da contratti gia' verificati (M3, M8, M9, M10, M12, M14, M15). Ogni blocco ha stato (dati | vuoto_valido | "
          "non_letto | non_disponibile | non_attivo), affidabilita' e provenienza (contratto, versione, uid); i grafici sono serie "
          "dichiarative emesse solo con punti reali (null mai 0). Medie: politiche nominate, corrispondenza ClasseViva non verificata. "
          "Storico separato dall'anno operativo. Non scrive nulla.",
          {"pagina": {"type": "string", "description": "pagina (default home)"},
           "materia": {"type": "string", "description": "solo con pagina='materia': una delle 8 materie"},
           "soglia_ore": {"type": "integer", "description": "soglia di freschezza in ore (default 48)"}})
def school_dashboard_v1(pagina="home", materia=None, soglia_ore=48):
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s): esegui prima cv_registry_sync" % path)
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Europe/Rome")).replace(microsecond=0)     # UNICA lettura dell'orologio
    before = dry._fs_state(path)
    out = sdash.build_from_path(path, now, subjects=subjects(), pagina=pagina or "home", materia=materia or None,
                                soglia_ore=48 if soglia_ore is None else soglia_ore)
    out["scritture"] = {"sqlite_reale": False, "classeviva": False, "classroom": False, "drive": False}
    out["verifica_nessuna_scrittura"] = {"file_modificati": before != dry._fs_state(path)}
    return out


def _material_call(fn, **kw):
    """Registro come copia in sola lettura + school_manual.json + now con fuso (UNICA lettura dell'orologio). Il Registry non viene
    mai scritto: l'unico file che puo' cambiare e' l'archivio dei materiali (solo school_material_acquire_v1)."""
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s): esegui prima cv_registry_sync" % path)
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Europe/Rome")).replace(microsecond=0)
    before = dry._fs_state(path)
    reg = regmod.Registry.from_snapshot(path)
    try:
        out = fn(smstore.default_store(), reg, now, manual=sctx.load_manual()["dati"], subjects=subjects(), **kw)
    finally:
        reg.close()
    out["verifica_registro_non_scritto"] = {"file_modificati": before != dry._fs_state(path)}
    return out


@srv.tool("school_material_acquire_v1",
          "ACQUISIZIONE di un materiale scolastico (M19, schema cloud.school.material_store/1). Registra nell'archivio LOCALE dei "
          "materiali (separato dal Registry, append-only) il testo di un file Drive citato da Classroom e presente nel catalogo M15. "
          "Prima leggi il file con il connettore Google Drive: get_file_metadata(fileId) e read_file_content(fileId), con il "
          "drive_file_id indicato da school_materials_status_v1; poi passa qui riferimento, testo e metadati. Rifiuta riferimenti "
          "fuori catalogo, id Drive diversi, formati non testuali. Stesso testo e stessa data di modifica = 'invariato' (nessuna "
          "scrittura). Non modifica Drive, Classroom o il Registry.",
          {"riferimento": {"type": "string", "description": "id esatto del riferimento M15 (es. cls:<corso>/<elemento>#drive_file:<id>)"},
           "testo": {"type": "string", "description": "fileContent restituito da read_file_content"},
           "metadati_drive": {"type": "object", "description": "oggetto restituito da get_file_metadata (id, title, mimeType, "
                                                               "modifiedTime, owner, fileSize)"}},
          ["riferimento", "testo", "metadati_drive"])
def school_material_acquire_v1(riferimento, testo, metadati_drive):
    return _material_call(smstore.acquire, riferimento=riferimento, testo=testo, metadati_drive=metadati_drive)


@srv.tool("school_materials_status_v1",
          "STATO DI ACQUISIZIONE dei materiali (sola lettura): per i riferimenti del catalogo M15 della materia (e, se indicato, "
          "collegati via M5 a un obiettivo/verifica) dice se il testo e' 'acquisito', 'da_acquisire' (con il fileId da leggere con "
          "il connettore Drive) o 'non_acquisibile'. Acquisito non significa studiato.",
          {"materia": {"type": "string", "description": "una delle 8 materie (facoltativa se c'e' riferimento)"},
           "obiettivo": {"type": "string", "description": "uid esatto di una verifica/obiettivo (es. cvv:1000104), facoltativo"},
           "riferimento": {"type": "string", "description": "un solo riferimento M15, facoltativo"}})
def school_materials_status_v1(materia=None, obiettivo=None, riferimento=None):
    return _material_call(smstore.status, materia=materia or None, obiettivo=obiettivo or None, riferimento=riferimento or None)


@srv.tool("school_study_material_v1",
          "STUDIO SU UN MATERIALE ACQUISITO (M20, sola lettura, schema cloud.school.study_material/1): sessione M13 (spiega, interroga, "
          "esercitati, correggi, ripassa, prepara_obiettivo) sul testo acquisito di un materiale della materia, con provenienza "
          "'materiale_scolastico_acquisito' (titolo, corso, autore, file Drive, versione, sha256), le stesse azioni/divieti/frasi "
          "obbligatorie di M13 e il collegamento M5 con l'obiettivo. Rifiuta materiali non acquisiti o di altra materia.",
          {"materia": {"type": "string"}, "modalita": {"type": "string", "enum": list(ssession.MODES)},
           "riferimento": {"type": "string", "description": "id esatto del materiale acquisito"},
           "obiettivo": {"type": "string", "description": "uid esatto dell'obiettivo M12 (obbligatorio per prepara_obiettivo)"},
           "risposta_utente": {"type": "string", "description": "solo per 'correggi'"}},
          ["materia", "modalita", "riferimento"])
def school_study_material_v1(materia, modalita, riferimento, obiettivo=None, risposta_utente=None):
    return _material_call(lambda store, reg, now, **kw: smstore.study(store, reg, now, materia, modalita, riferimento,
                                                                      obiettivo=obiettivo or None, risposta_utente=risposta_utente,
                                                                      source_path=regmod.default_path(), **kw))


@srv.tool("cls_course_subjects",
          "Mappa corso Classroom -> materia (sola lettura) per la revisione dell'utente: per ogni corso categoria "
          "(confirmed/proposed/excluded/non_attributed/ambiguous), materie proposte ed evidenze E1 (campo subject), "
          "E2 (nome del corso), E3 (cognome del docente ClasseViva). Le proposte sono ricalcolate e NON persistenti; "
          "solo school_manual.json conferma un corso. Non scrive nulla.")
def cls_course_subjects():
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s)" % path)
    reg = regmod.Registry.from_snapshot(path)
    try:
        man = sctx.load_manual()
        out = sctx.course_subjects_report(reg, subjects(), man["dati"])
        out["school_manual"] = {"presente": man["presente"], "percorso": man["percorso"]}
        return out
    finally:
        reg.close()


def _agent_call(fn, subject, **kw):
    """Registro aperto come copia in sola lettura + school_manual.json letto + now con fuso: unico punto con l'orologio."""
    path = regmod.default_path()
    if not os.path.isfile(path):
        raise ValueError("registro locale assente (%s): esegui prima cv_registry_sync" % path)
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Europe/Rome")).replace(microsecond=0)
    reg = regmod.Registry.from_snapshot(path)
    try:
        return fn(reg, subject, subjects(), now, manual=sctx.load_manual()["dati"], **kw)
    finally:
        reg.close()


_SUBJ = {"subject": {"type": "string", "description": "una delle 8 materie: italiano, latino, inglese, storia, filosofia, "
                                                       "matematica, fisica, scienze"}}


@srv.tool("school_brief",
          "CONTESTO DI AVVIO di un agente di materia (sola lettura, compatto, schema cloud.school.brief/1): corsi "
          "CONFERMATI in school_manual.json, compiti operativi (attivi + recenti + conteggi per stato) con stato della "
          "consegna, ultimi annunci e materiali, argomenti, verifiche/voti ClasseViva della materia, da_verificare, regole "
          "e strumenti di recupero. Solo perimetro operativo 3a/4a; corsi proposti solo come nome + conteggi; nessun dato "
          "di altre materie, del biennio o dello storico. Citazioni 'ref' = uid@versione.",
          dict(_SUBJ), ["subject"])
def school_brief(subject):
    return _agent_call(sagent.brief, subject)


@srv.tool("school_tasks",
          "Elenco compatto (sola lettura) di TUTTI i compiti operativi dei corsi confermati della materia, opzionalmente "
          "filtrati per stato_operativo. Nessun compito di altre materie o dello storico.",
          dict(_SUBJ, stato={"type": "string", "enum": ["aperto", "scaduto_non_consegnato", "senza_scadenza", "consegnato",
                                                        "restituito", "bozza", "rimosso", "da_verificare"],
                             "description": "facoltativo"}), ["subject"])
def school_tasks(subject, stato=None):
    return _agent_call(sagent.tasks, subject, stato=stato or None)


@srv.tool("school_task",
          "Dettaglio (sola lettura) di UN compito della materia: descrizione, allegati, link, consegna, punti (non voti), "
          "provenienza completa (uid, versione, hash). Rifiuta compiti di altre materie, di corsi non confermati o storici.",
          dict(_SUBJ, uid={"type": "string", "description": "uid del compito (es. cls:<corso>/<compito>) o ref uid@versione"}),
          ["subject", "uid"])
def school_task(subject, uid):
    return _agent_call(sagent.task, subject, uid=uid)


@srv.tool("school_announcements",
          "Annunci operativi (sola lettura) dei corsi confermati della materia, con testo completo e allegati, piu' recenti "
          "prima; ricerca facoltativa per parola. Massimo 50 per chiamata, con 'totale' e 'troncato'.",
          dict(_SUBJ, cerca={"type": "string", "description": "facoltativo: parola o frase da cercare nel testo"},
               limite={"type": "integer", "description": "facoltativo, default 20, massimo 50"}), ["subject"])
def school_announcements(subject, cerca=None, limite=None):
    return _agent_call(sagent.announcements, subject, cerca=cerca or None, limite=limite)


@srv.tool("school_materials",
          "Materiali operativi (sola lettura) dei corsi confermati della materia, con descrizione e allegati; filtri "
          "facoltativi per argomento o parola. Massimo 100 per chiamata, con 'totale' e 'troncato'.",
          dict(_SUBJ, argomento={"type": "string", "description": "facoltativo: nome (o parte) dell'argomento Classroom"},
               cerca={"type": "string", "description": "facoltativo: parola nel titolo o nella descrizione"},
               limite={"type": "integer", "description": "facoltativo, default 30, massimo 100"}), ["subject"])
def school_materials(subject, argomento=None, cerca=None, limite=None):
    return _agent_call(sagent.materials, subject, argomento=argomento or None, cerca=cerca or None, limite=limite)


@srv.tool("school_subjects",
          "Elenco (sola lettura) delle 8 materie del futuro sistema di agenti, ciascuna con il proprio contesto "
          "school_context, e delle materie escluse (nessun contesto). Non configura agenti.")
def school_subjects():
    try:
        subs = subjects()
    except Exception:          # noqa: BLE001 - l'elenco resta valido anche senza i nomi ufficiali di ClasseViva
        subs = []
    return sctx.subjects_catalog(subs)


def selftest():
    c = client()
    b = dt.date.today()
    n = len(c.agenda(b, b + dt.timedelta(days=14)))
    print("LOGIN OK - nome: %s - eventi agenda (14 gg): %d" % (c.first_name, n))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        try:
            selftest()
        except cv.CVError as exc:
            print("ERRORE:", exc)
            sys.exit(1)
    else:
        srv.run()
