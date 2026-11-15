"""CONTESTO PER MATERIA: interfaccia dati STABILE tra il corpus del Cloud e i futuri agenti di materia.
Modulo PURO e SOLA LETTURA.

Classroom -> registro (L1) -> L2 normalizzato -> L3 cls_derive -> **school_context** -> futuro agente materia -> Claude
  * L1/L2/L3 = corpus COMPLETO e interrogabile (tutti gli anni, tutte le materie);
  * school_context = SELEZIONE compatta e deterministica del corpus per UNA materia, SOLO perimetro operativo 3a/4a
    (superficie agent-facing: `build`/`build_from_path`). Lo storico resta nel registro come dato tecnico e non e'
    esposto agli agenti; `_build(..., ambito=...)` e' solo interno (test/manutenzione);
  * corso -> materia: PROPOSTE ricalcolate da cls_subjects (non persistenti); CONFERME solo in school_manual.json.
    Un corso entra nel contesto di una materia SOLO se il suo course_id e' nella lista manuale di quella materia.
  * futuro agente = consumatore del contesto. Prompt, personalita', autonomia, meccanismo Claude: NON qui.
  * la dashboard (CUORE) e' una vista, mai fonte di verita'.

Legge il registro (lo apre come copia in sola lettura: Registry.from_snapshot, oppure un Registry gia' aperto passato dal
chiamante) e produce un documento JSON deterministico per UNA materia. Un solo codice per tutte le materie: l'agente di
una materia e' questo contesto + un template comune (docs/agente-materia-template.md), non 13 implementazioni.

Regole:
  * non modifica RAW, normalizzato, registro o le fonti;
  * non inventa: cio' che manca e' dichiarato (sezione `da_verificare`, conteggi a 0 = stato vuoto valido);
  * ClasseViva e Classroom restano SEPARATE: i collegamenti materia<->corso Classroom vengono PROPOSTI (evidenza visibile,
    stato 'proposto') e usati solo se CONFERMATI nel livello manuale; mai fusi automaticamente;
  * le informazioni dichiarate dall'utente (docenti, note, link confermati) vivono nel livello MANUALE, separato e mai
    sovrascritto dalle sincronizzazioni; qui sono solo lette e marcate come tali.
"""
from __future__ import annotations

import datetime as dt
import json
import os

import cls_derive as D
import cls_registry as CL
import cls_subjects as CS
import cvv_client as cv
import cvv_registry as CR
import cvv_subject as subj
import registry as R

SCHEMA = "cloud.school.context/2"
# Tetti di SICUREZZA (token): mai tagli silenziosi, il contesto dichiara troncato/totale/inclusi/omessi.
LIMITS = {"announcements": 30, "materials": 50, "course_work": 200, "past_tests": 30}

# Materie del futuro sistema di agenti (decisione dell'utente, 2026-10-03). Le altre non hanno contesto.
AGENT_SUBJECTS = CS.AGENT_SUBJECTS
EXCLUDED_SUBJECTS = CS.EXCLUDED_SUBJECTS
AMBITI = ("operativo", "storico", "tutto")


def manual_default_path():
    return os.path.join(os.path.dirname(R.default_path()), "manual", "school_manual.json")


def load_manual(path=None):
    """Livello manuale (JSON scritto dall'utente/da Claude su richiesta esplicita). Assente = vuoto. Mai creato qui."""
    p = path or manual_default_path()
    if not os.path.isfile(p):
        return {"presente": False, "percorso": p, "dati": {}}
    with open(p, encoding="utf-8") as f:
        return {"presente": True, "percorso": p, "dati": json.load(f)}


def subject_keys():
    return [k for k, _ in subj.SUBJECT_KEYS]


def _cite(rec):
    return {"uid": rec.get("uid"), "registry_version": rec["version"], "content_hash": rec["content_hash"],
            "source_status": rec["source_status"], "first_seen": rec["first_seen"]}


def course_subjects_report(reg, subjects, manual=None):
    """Mappa corso -> materia (sola lettura) per la revisione dell'utente: proposte ricalcolate + conferme manuali."""
    courses = [reg.get_record(CL.SOURCE, "course", r["source_uid"])
               for r in reg.list_records(source=CL.SOURCE, kind="course", status=R.PRESENT)]
    return CS.classify(courses, subjects, manual)


def subjects_catalog(subjects=None):
    """Le materie del futuro sistema di agenti (una per agente) e quelle escluse. Puro."""
    desc = {s["key"]: s["desc"] for s in (subjects or [])}
    return {"materie": [{"chiave": k, "nome_ufficiale": desc.get(k)} for k in AGENT_SUBJECTS],
            "escluse": [{"chiave": k, "nome_ufficiale": desc.get(k)} for k in EXCLUDED_SUBJECTS],
            "nota": "un contesto (e in futuro un agente) per ciascuna materia; le escluse non hanno contesto"}


def resolve_subject(subject, subjects):
    """Chiave della materia (dal nome o dalla chiave). ValueError se sconosciuta o ESCLUSA dal sistema agenti."""
    key = subj.subject_key(subject) or (subject if subject in subject_keys() else None)
    if key is None:
        matches = [s for s in subjects if subj.norm(subject) in subj.norm(s["desc"])]
        key = matches[0]["key"] if len(matches) == 1 else None
    if key is None:
        raise ValueError("materia non riconosciuta: %r (materie: %s)" % (subject, ", ".join(AGENT_SUBJECTS)))
    if key not in AGENT_SUBJECTS:
        raise ValueError("materia '%s' esclusa dal sistema degli agenti (escluse: %s)" % (key, ", ".join(EXCLUDED_SUBJECTS)))
    return key


def _school_year_of_ts(ts):
    """Anno scolastico (1/9-31/8, data locale) da un timestamp ISO; None se assente/illeggibile. Perimetro del contesto
    per annunci/materiali (regola di contesto, distinta da anno_compito di L3)."""
    if not (isinstance(ts, str) and len(ts) >= 10):
        return None
    try:
        d = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(D._zone()).date()
    except ValueError:
        return None
    y = d.year if d.month >= D.SCHOOL_YEAR_START_MONTH else d.year - 1
    return "%d-%02d" % (y, (y + 1) % 100)


def _in_scope(perimetro, anno, ambito, wanted_year):
    if wanted_year is not None:
        return anno == wanted_year
    if ambito == "operativo":
        return perimetro == "operativo"
    if ambito == "storico":
        return perimetro == "storico"
    return True


def _capped(rows, limit, ambito, label):
    total = len(rows)
    out = rows[:limit]
    meta = {"totale": total, "inclusi": len(out), "omessi": total - len(out), "troncato": total > len(out)}
    if meta["troncato"]:
        meta["come_richiedere_il_resto"] = ("%s: inclusi i %d piu' recenti; gli altri restano nel registro/L3 "
                                            "(non inclusi in questo contesto)" % (label, len(out)))
    return out, meta


def _task(v, confirmed_by, key, reg, include_raw):
    """work_view L3 -> voce compatta compito+consegna, con provenienza uid/versione/hash."""
    f, d, p = v["fonte"], v["derivato"], v["provenienza"]
    cite = lambda x: None if x is None else {"uid": x["uid"], "registry_version": x["version"],          # noqa: E731
                                             "content_hash": x["content_hash"], "source_status": x["stato_registro"]}
    c = f["consegna"]
    t = {"uid": v["uid"], "titolo": f["compito"]["titolo"], "tipo": f["compito"]["tipo"], "link": f["compito"]["link"],
         "creato": f["compito"]["creato"],
         "materiali": f["compito"]["materiali"], "topic_id": f["compito"]["topic_id"],
         "scadenza": {k: d["scadenza"][k] for k in ("data_locale", "momento_locale", "precisione", "giorni_mancanti", "nota")},
         "consegna": {"presente": c["presente"], "uid": c["uid"], "stato_fonte": c["stato_fonte"], "late": c["late"],
                      "assigned_points": c["assigned_points"], "nota_punti": c["nota_punti"],
                      "candidati": c["candidati"]},
         "stato_operativo": d["stato_operativo"], "motivo_stato": d["motivo_stato"], "note_stato": d["note_stato"],
         "anno_compito": {k: d["anno_compito"][k] for k in ("valore", "certezza", "fonte")},
         "perimetro": d["perimetro"]["valore"],
         "corso": {"id": f["corso"]["id"], "nome": f["corso"]["nome"], "attivo": d["corso_attivo"],
                   "condiviso_con": sorted(k for k in confirmed_by.get(f["corso"]["id"], ()) if k != key)},
         "materia": v["manuale"]["materia"],
         "da_verificare": v["da_verificare"],
         "fonte": {"course_work": cite(p["course_work"]), "submission": cite(p["submission"])},
         "derive_version": v["derive_version"]}
    if include_raw:
        cid, wid = f["corso"]["id"], f["compito"]["id"]
        rec = reg.get_record(CL.SOURCE, "course_work", "%s/%s" % (cid, wid))
        t["raw"] = rec["raw"] if rec else None
    return t


def _recency(t):
    """Data di riferimento per l'ordinamento: scadenza locale, altrimenti data di creazione del compito."""
    return t["scadenza"]["data_locale"] or (t["creato"] or "")[:10]


def _classroom_section(reg, key, now, ambito, anno, manual, include_raw, l3, subjects, limits=None):
    """Parte Classroom del contesto: corsi CONFERMATI (solo school_manual.json) separati dai corsi PROPOSTI
    (cls_subjects, E1/E2/E3). Nessuna proposta diventa conferma; corsi esclusi/non attribuiti mai inclusi."""
    courses = [reg.get_record(CL.SOURCE, "course", r["source_uid"])
               for r in reg.list_records(source=CL.SOURCE, kind="course", status=R.PRESENT)]
    by_id = {c["raw"].get("id"): c for c in courses}
    lim_ = dict(LIMITS, **(limits or {}))
    confirmed_by = CS.confirmed_map(manual)
    mine = sorted(cid for cid, ks in confirmed_by.items() if key in ks)
    report = CS.classify(courses, subjects, manual)
    rows = {r["course_id"]: r for r in report["corsi"]}
    proposed = [cid for cid in CS.proposed_for(report, key) if cid not in mine]
    todo = ["corso Classroom confermato %s non presente nel registro" % cid for cid in mine if cid not in by_id]
    todo.extend(report["avvisi"])

    def views_of(cids):
        return [v for v in l3["work_views"] if v["fonte"]["corso"]["id"] in cids
                and (v["provenienza"]["course_work"] or {}).get("stato_registro") in (None, R.PRESENT)]

    def block(cids):
        allv = views_of(set(cids))
        sel = [v for v in allv if _in_scope(v["derivato"]["perimetro"]["valore"],
                                            v["derivato"]["perimetro"]["anno"], ambito, anno)]
        tasks = [_task(v, confirmed_by, key, reg, include_raw) for v in sel]
        tasks.sort(key=lambda t: (_recency(t), t["uid"]), reverse=True)                 # piu' recenti prima
        tasks.sort(key=lambda t: 0 if t["stato_operativo"] == "aperto" else 1)          # stabile: aperti in testa
        tasks, meta = _capped(tasks, lim_["course_work"], ambito, "compiti")
        per = {}
        for v in allv:
            per[v["derivato"]["perimetro"]["valore"]] = per.get(v["derivato"]["perimetro"]["valore"], 0) + 1
        orphans = [o for o in l3["consegne_orfane"] if o["course_id"] in cids]
        out = {"corsi": [{"course_id": cid, "nome": by_id[cid]["raw"].get("name") if cid in by_id else None,
                          "stato": by_id[cid]["raw"].get("courseState") if cid in by_id else None,
                          "condiviso_con": sorted(k for k in confirmed_by.get(cid, ()) if k != key),
                          "fonte": _cite({**by_id[cid], "uid": "cls:" + cid}) if cid in by_id else None}
                         for cid in cids],
               "compiti": tasks, "compiti_conteggio": meta,
               "compiti_per_perimetro_nei_corsi": dict(sorted(per.items())),
               "consegne_orfane": orphans}
        used_topics = {t["topic_id"] for t in tasks if t["topic_id"]}
        for kind, bucket, lim, field in (("announcement", "annunci", lim_["announcements"], "text"),
                                         ("material", "materiali", lim_["materials"], "title")):
            rows, excluded = [], {}
            for r in reg.list_records(source=CL.SOURCE, kind=kind, status=R.PRESENT):
                if r["source_uid"].split("/")[0] not in cids:
                    continue
                full = reg.get_record(CL.SOURCE, kind, r["source_uid"])
                nz = CL.normalized(kind, full["raw"])
                y = _school_year_of_ts(nz.get("created"))
                per_v = "da_verificare" if y is None else ("operativo" if y in D.CLASSI else "storico")
                if not _in_scope(per_v, y, ambito, anno):
                    excluded[per_v] = excluded.get(per_v, 0) + 1
                    continue
                d = {k: v for k, v in nz.items() if k not in ("hash", "kind", "source", "interp_version", "raw")}
                d.update(anno_contesto=y, perimetro=per_v, fonte=_cite({**full, "uid": nz["uid"]}),
                         condiviso_con=sorted(k for k in confirmed_by.get(nz.get("course_id"), ()) if k != key))
                if include_raw:
                    d["raw"] = full["raw"]
                rows.append(d)
            rows.sort(key=lambda d: (d.get("created") or "", d["uid"]), reverse=True)
            rows, meta = _capped(rows, lim, ambito, bucket)
            if bucket == "materiali":
                used_topics |= {m.get("topic_id") for m in rows if m.get("topic_id")}
            out[bucket], out[bucket + "_conteggio"] = rows, dict(meta, esclusi_per_perimetro=dict(sorted(excluded.items())))
        topics = []
        for r in reg.list_records(source=CL.SOURCE, kind="topic", status=R.PRESENT):
            full = reg.get_record(CL.SOURCE, "topic", r["source_uid"])
            nz = CL.normalized("topic", full["raw"])
            if nz.get("course_id") in cids and nz.get("topic_id") in used_topics:
                topics.append({"uid": nz["uid"], "nome": nz.get("name"), "topic_id": nz.get("topic_id"),
                               "course_id": nz.get("course_id"), "fonte": _cite({**full, "uid": nz["uid"]})})
        out["argomenti"] = sorted(topics, key=lambda t: t["uid"])
        out["argomenti_regola"] = "solo argomenti collegati a compiti o materiali inclusi in questo contesto"
        return out

    conf = block(mine)
    prop = block(proposed)
    prop["stato"] = "PROPOSTA NON CONFERMATA"
    prop["avviso"] = ("corsi PROPOSTI automaticamente (cls_subjects, E1/E2/E3): NON sono fatti della materia finche' "
                      "l'utente non li conferma in school_manual.json; non trattarli come confermati")
    for c in prop["corsi"]:
        r = rows.get(c["course_id"], {})
        c.update(categoria=r.get("categoria"), materie_proposte=r.get("materie_proposte"),
                 motivazione=r.get("motivazione"), evidenze=r.get("evidenze"))
    if conf["consegne_orfane"]:
        todo.append("%d consegne senza compito nei corsi confermati" % len(conf["consegne_orfane"]))
    excl = conf["compiti_per_perimetro_nei_corsi"].get("da_verificare", 0)
    if excl and ambito == "operativo" and anno is None:
        todo.append("%d compiti con perimetro da verificare esclusi dal contesto operativo" % excl)
    classroom = {"fonte": "Google Classroom (separata da ClasseViva)", "corsi_registrati": len(courses),
                 "confermato": conf,
                 "nota": ("compito e consegna sono un'unica voce (work_view L3); assigned_points e' un PUNTEGGIO, non un "
                          "voto scolastico; i file Drive sono solo metadati")}
    if not courses:
        classroom["stato"] = "nessun dato Classroom nel registro (non sincronizzato o non autorizzato)"
    return classroom, prop, todo


def build(reg, subject, subjects, now, manual=None, include_raw=False, source_path=None):
    """SUPERFICIE AGENT-FACING. reg: Registry (sola lettura). subject: una delle AGENT_SUBJECTS (chiave o nome).
    subjects: build_subjects(...) di ClasseViva (anche per l'evidenza E3). now: datetime con fuso o ISO con offset
    (ESPLICITO: stesso now -> stesso JSON). manual: dict di school_manual.json (solo letto; mai creato qui).
    Restituisce SOLO il perimetro operativo (3a/4a): lo storico non e' esposto."""
    return _build(reg, subject, subjects, now, manual=manual, include_raw=include_raw, source_path=source_path)


def _build(reg, subject, subjects, now, manual=None, include_raw=False, source_path=None, ambito="operativo", anno=None,
           limits=None):
    """INTERNO (test/manutenzione): come build, con ambito 'operativo' | 'storico' | 'tutto' e anno 'AAAA-AA'.
    Non esposto da server.py: gli agenti non ricevono lo storico."""
    if ambito not in AMBITI:
        raise ValueError("ambito non valido: %r (ammessi: %s)" % (ambito, ", ".join(AMBITI)))
    if anno is not None and not (isinstance(anno, str) and len(anno) == 7 and anno[4] == "-"):
        raise ValueError("anno non valido: %r (formato 'AAAA-AA', es. '2025-26')" % (anno,))
    now = D.parse_now(now)
    today = now.astimezone(D._zone()).date().isoformat()
    key = resolve_subject(subject, subjects)
    official = [s for s in subjects if s["key"] == key]
    desc = official[0]["desc"] if official else None
    manual = manual or {}
    mman = (manual.get("subjects") or {}).get(key, {})
    to_verify = []

    # ---- ClasseViva: agenda -----------------------------------------------------------------------------
    items, possibili, gone = [], [], 0
    for rec in reg.list_records(source=CR.SOURCE, kind="agenda_event"):
        full = reg.get_record(CR.SOURCE, "agenda_event", rec["source_uid"])
        if full["source_status"] != R.PRESENT:
            gone += 1
            continue
        n = cv.enrich_event(CR.normalized("agenda_event", full["raw"]), subjects)
        n["_rec"] = full
        items.append(n)
    ok, maybe = subj.subject_filter(items, desc or subject)

    def view(n):
        d = {"uid": n["uid"], "tipo_dedotto": n["kind"], "data": n["date"], "inizio": n["start"], "fine": n["end"],
             "testo": n["text"], "docente": n.get("author"), "materia_criterio": n.get("subject_criterion"),
             "confidenza": n.get("subject_confidence"), "fonte": _cite({**n["_rec"], "uid": n["uid"]})}
        if include_raw:
            d["raw"] = n["_rec"]["raw"]
        return d
    by = lambda kind: sorted([view(n) for n in ok if n["kind"] == kind], key=lambda d: (d["data"], d["uid"]))   # noqa: E731
    tests = by("verifica")
    agenda = {"verifiche_future": [t for t in tests if t["data"] >= today],
              "verifiche_passate": [t for t in tests if t["data"] < today][-LIMITS["past_tests"]:],
              "compiti": [t for t in by("compito")],
              "eventi": by("evento"),
              "nota": "tipo (verifica/compito/evento) DEDOTTO da codice evento e testo; la materia solo con criterio verificabile"}
    possibili_view = [{**view(n), "candidati": n.get("subject_candidates")} for n in maybe]
    if possibili_view:
        to_verify.append("%d eventi con materia 'da verificare' potrebbero riguardare questa materia" % len(possibili_view))

    # ---- ClasseViva: voti -------------------------------------------------------------------------------
    grades = []
    for rec in reg.list_records(source=CR.SOURCE, kind="grade", status=R.PRESENT):
        full = reg.get_record(CR.SOURCE, "grade", rec["source_uid"])
        n = CR.normalized("grade", full["raw"])
        if n["interpreted"]["materia_chiave"] == key:
            grades.append({"uid": n["uid"], "data": n["date"], "nome": n["derived"]["nome"], "raw": full["raw"],
                           "interpretazione": n["interpreted"], "fonte": _cite({**full, "uid": n["uid"]})})
    grades.sort(key=lambda g: (g["data"] or "", g["uid"]))
    n_all_grades = len(reg.list_records(source=CR.SOURCE, kind="grade", status=R.PRESENT))

    # ---- Classroom (separata, da L3): corsi CONFERMATI e, a parte, corsi PROPOSTI -----------------------
    l3 = D.from_registry(reg, now, manual={"course_subjects": {
        cid: [{"subject": k} for k in sorted(ks)] for cid, ks in CS.confirmed_map(manual).items()}})
    classroom, classroom_proposto, cl_todo = _classroom_section(reg, key, now, ambito, anno, manual, include_raw, l3,
                                                                subjects, limits=limits)
    to_verify.extend(cl_todo)

    teachers = sorted({t for s in official for t in s.get("docenti_nomi", []) if t})
    return {
        "schema": SCHEMA,
        "materia": {"chiave": key, "nome_ufficiale": desc, "docenti_registro": teachers},
        "data_riferimento": today,
        "riferimento_temporale": now.isoformat(),
        "ambito": {"valore": ambito, "anno": anno, "perimetro_operativo": dict(D.CLASSI),
                   "nota": ("solo perimetro operativo 3a/4a classe; lo storico resta nel registro come dato tecnico e "
                            "non e' esposto agli agenti") if ambito == "operativo" and anno is None else
                           "vista INTERNA (manutenzione/test): non destinata agli agenti"},
        "agenda_classeviva": agenda,
        "possibili_da_verificare": possibili_view,
        "voti_classeviva": {"materia": grades, "voti_totali_registro": n_all_grades,
                            "nota": "0 voti = stato vuoto valido, non un dato mancante" if not grades else None},
        "classroom": classroom,
        "classroom_proposto": classroom_proposto,
        "livello_manuale": {"presente": bool(manual), "dichiarato_dall_utente": mman or None},
        "materiali_e_appunti": {"stato": "non ancora indicizzati (iCloud/appunti/PDF/registrazioni): fuori da questa versione"},
        "provenienza": {"registro": source_path or reg.path, "copia_in_sola_lettura": reg.path == ":memory:", "record_totali": len(reg.list_records()),
                        "agenda_scomparsa_dalla_fonte": gone,
                        "fonti": sorted({r["source"] for r in reg.list_records()}),
                        "derive_version": l3["derive_version"]},
        "da_verificare": to_verify,
        "regole_per_gli_agenti": [
            "sola lettura: non modificare RAW, normalizzato, registro o le fonti",
            "cita sempre uid + registry_version + content_hash di cio' che usi",
            "se un dato manca o e' 'da verificare', dillo: non inventare voti, date, docenti, materie",
            "ClasseViva e Classroom sono fonti separate: non fonderle senza collegamento confermato",
            "classroom_proposto contiene PROPOSTE non confermate: non trattarle come fatti della materia",
            "solo i corsi in classroom.confermato appartengono alla materia (confermati dall'utente in school_manual.json)",
            "stato_operativo e' tecnico: 'senza_scadenza' non significa 'da fare', 'scaduto_non_consegnato' non significa 'da recuperare'",
            "le informazioni dichiarate dall'utente (livello manuale) sono dichiarazioni, non fatti verificati"],
    }


def build_from_path(path, subject, subjects, now, manual_path=None, include_raw=False):
    """SUPERFICIE AGENT-FACING su file: registro aperto come copia in sola lettura; solo perimetro operativo."""
    reg = R.Registry.from_snapshot(path)
    try:
        return build(reg, subject, subjects, now, manual=load_manual(manual_path)["dati"], include_raw=include_raw,
                     source_path=os.path.abspath(path))
    finally:
        reg.close()
