"""CONTESTO PER MATERIA v3 (M4): `cloud.school.context/3`. Modulo PURO e SOLA LETTURA.

Il Registry resta la fonte canonica; questo modulo e' una VISTA derivata, mai un secondo registro. Non scrive nulla, non
duplica ne' modifica i record canonici (li cita con uid + versione + hash).

Tre livelli distinti, sempre nominati nello stesso modo:
  * `dati`            = canonico: i campi ESATTI della fonte (raw), citati in `fonte`;
  * `interpretazione` = derivato: attribuzione di materia M2 (con criterio, confidenza, evidenze, conflitti), tipo dedotto,
                        stato operativo L3. E' una lettura del dato, non il dato;
  * `freschezza`      = operativo: quanto e' recente la lettura da cui la sezione dipende (M3).

Regole:
  * una materia e' assegnata a un record SOLO se M2 la DETERMINA (`stato=determinata`); se M2 non decide il record sta in
    `possibili_da_verificare` con i candidati, mai nella materia; i conflitti restano visibili e non cambiano la materia;
  * `stale=false` non e' una garanzia di correttezza: dice solo che l'ultima lettura riuscita e' entro la soglia;
  * cio' che manca e' dichiarato (`informazioni_mancanti`), mai riempito;
  * `cloud.school.context/2` (school_context.build) resta invariato e disponibile: v3 lo riusa per Classroom e voti.
"""
from __future__ import annotations

import school_context as SC
import cls_derive as D
import cls_subjects as CS
import cvv_attribution as CA
import cvv_client as cv
import cvv_registry as CR
import cvv_subject as S
import registry as R

SCHEMA = "cloud.school.context/3"
OVERVIEW_SCHEMA = "cloud.school.overview/1"
LIMITS = {"lezioni": 60, "uid_nei_warning": 20}

# sezione -> kind del registro da cui dipende (per la freschezza)
SECTION_KINDS = {
    "lezioni": [(CR.SOURCE, "lesson")],
    "agenda": [(CR.SOURCE, "agenda_event")],
    "voti": [(CR.SOURCE, "grade")],
    "classroom": [("classroom", k) for k in ("course", "course_work", "submission", "announcement", "material", "topic")],
}
FRESH_NOTE = ("'aggiornato' = l'ultima lettura riuscita e' entro la soglia: NON garantisce che il dato sia corretto o "
              "completo; 'legacy' = run precedenti a M3 (senza fine/durata): la verifica e' meno ricca")

LEVELS = {
    "dati": "canonico: campi esatti della fonte (raw), citati in 'fonte' con uid + registry_version + content_hash",
    "interpretazione": "derivato: attribuzione di materia M2 (criterio, confidenza, evidenze, conflitti), tipo dedotto; "
                       "e' una lettura del dato, non il dato",
    "freschezza": "operativo: eta' e stato dell'ultima lettura da cui dipende la sezione (M3)",
}

RULES_V3 = [
    "Il registro e' la fonte canonica; questo contesto e' una vista derivata in sola lettura: non e' un secondo registro.",
    "'dati' e' il fatto della fonte; 'interpretazione' e' una lettura derivata: citali separatamente e non scambiarli.",
    "Un record e' di una materia solo se interpretazione.stato='determinata'; i 'possibili_da_verificare' NON sono della materia.",
    "I conflitti (interpretazione.conflitti) sono segnalati, non risolti: la materia indicata non e' stata modificata.",
    "Controlla sempre la 'freschezza' della sezione: se e' 'stale' o 'senza_lettura' dichiaralo; 'aggiornato' non garantisce correttezza.",
    "'informazioni_mancanti' elenca cio' che non c'e': non inventarlo e non dedurlo.",
]


# ------------------------------------------------------------------------------------------------ helper
def _cite(rec, uid):
    return {"uid": uid, "registry_version": rec["version"], "content_hash": rec["content_hash"],
            "source_status": rec["source_status"], "first_seen": rec["first_seen"]}


def _present(reg, source, kind):
    """Record PRESENTI di un kind (RAW completo) + quanti sono scomparsi dalla fonte."""
    out, gone = [], 0
    for rec in reg.list_records(source=source, kind=kind):
        full = reg.get_record(source, kind, rec["source_uid"])
        if full["source_status"] != R.PRESENT:
            gone += 1
            continue
        out.append(full)
    return out, gone


def _desc_to_key(subjects):
    return {S.norm(s["desc"]): s["key"] for s in subjects if s.get("desc")}


def _teacher_ok(who, docente):
    return not docente or S.norm(docente) in S.norm(who or "")


def _flagged(a):
    return a["stato"] != CA.ST_OK or bool(a["conflitti"]) or bool(a["avvisi"])


def _interp(a, with_evidence):
    d = {"attr_version": a["attr_version"], "materia_api": a["materia_api"], "materia_derivata": a["materia_derivata"],
         "stato": a["stato"], "criterio": a["criterio"], "confidenza": a["confidenza"], "candidati": a["candidati"],
         "conflitti": a["conflitti"], "avvisi": a["avvisi"]}
    if with_evidence:
        d["evidenze"] = a["evidenze"]
    return d


def _classify(a, key, d2k):
    """'incluso' (determinata e della materia) | 'possibile' (M2 non decide ma la materia e' tra i candidati) | 'altro'."""
    if a["stato"] == CA.ST_OK:
        return "incluso" if d2k.get(S.norm(a["materia_derivata"] or "")) == key else "altro"
    cand = {d2k.get(S.norm(c), c if c in d2k.values() else None) for c in a["candidati"]}
    return "possibile" if key in cand else "altro"


def _kind_entry(fresh, source, kind):
    return (fresh["per_kind"].get("%s/%s" % (source, kind))
            or {"ultimo_run": None, "stale": True, "motivo": "mai_sincronizzato"})


def section_freshness(fresh, kinds):
    """Freschezza di UNA sezione dai run M3 dei kind da cui dipende. 'aggiornato' | 'stale' | 'senza_lettura', piu' i flag
    'legacy' e 'con_rifiuti'. La sezione e' vecchia quanto la sua parte piu' vecchia."""
    rows = [(k, _kind_entry(fresh, s, k)) for s, k in kinds]
    no_read = [k for k, e in rows if e["motivo"] in ("mai_sincronizzato", "nessuna_lettura_riuscita")]
    stale = [k for k, e in rows if e["stale"] and k not in no_read]
    legacy = [k for k, e in rows if (e.get("ultimo_run") or {}).get("legacy")]
    refused = [k for k, e in rows if e["motivo"] == "ultima_lettura_con_rifiuti"]
    reads = [e["ultima_lettura_riuscita"] for _, e in rows if e.get("ultima_lettura_riuscita")]
    ages = [e["eta_ore"] for _, e in rows if e.get("eta_ore") is not None]
    return {"stato": "senza_lettura" if no_read else ("stale" if stale else "aggiornato"),
            "ultima_lettura_riuscita_piu_vecchia": min(reads) if reads else None,
            "eta_ore_max": max(ages) if ages else None, "soglia_ore": fresh["soglia_ore"],
            "legacy": bool(legacy), "kind_legacy": legacy, "kind_senza_lettura": no_read, "kind_stale": stale,
            "con_rifiuti": refused,
            "per_kind": {k: {"motivo": e["motivo"], "eta_ore": e.get("eta_ore"), "stale": e["stale"]} for k, e in rows},
            "nota": FRESH_NOTE}


def _w(out, codice, gravita, sezione, messaggio, uids=None, **extra):
    d = {"codice": codice, "gravita": gravita, "sezione": sezione, "messaggio": messaggio}
    if uids:
        d["n"] = len(uids)
        d["uid"] = sorted(uids)[:LIMITS["uid_nei_warning"]]
        d["uid_troncati"] = len(uids) > LIMITS["uid_nei_warning"]
    d.update(extra)
    out.append(d)


def _truncation_warning(out, sezione, voce, meta):
    """Se il contenuto mostrato e' PARZIALE lo dichiara anche nei warning (i limiti e la selezione non cambiano)."""
    if meta and meta.get("troncato"):
        _w(out, "contenuto_parziale", "attenzione", sezione,
           "%s: mostrati %d di %d (%d omessi): il contenuto e' PARZIALE" % (voce, meta["inclusi"], meta["totale"], meta["omessi"]),
           mostrati=meta["inclusi"], totale=meta["totale"], omessi=meta["omessi"])


def _freshness_warnings(out, sezione, f):
    if f["stato"] == "senza_lettura":
        _w(out, "sezione_senza_lettura", "attenzione", sezione,
           "nessuna lettura riuscita registrata per: %s" % ", ".join(f["kind_senza_lettura"]))
    elif f["stato"] == "stale":
        _w(out, "sezione_stale", "attenzione", sezione,
           "lettura oltre la soglia di %sh o ultimo run in errore: %s" % (f["soglia_ore"], ", ".join(f["kind_stale"])))
    if f["con_rifiuti"]:
        _w(out, "ultima_lettura_con_rifiuti", "info", sezione,
           "l'ultima lettura ha rifiutato record anomali (nessun record marcato scomparso): %s" % ", ".join(f["con_rifiuti"]))
    if f["legacy"]:
        _w(out, "dati_legacy", "info", sezione,
           "run precedenti a M3 (fine/durata assenti): freschezza ricavata da inizio run, verifica meno ricca")


# ------------------------------------------------------------------------------------------------ lezioni
def lessons_section(reg, key, subjects, now, docente=None, include_raw=False, fresh=None, limit=None):
    """Lezioni ClasseViva della materia, solo se M2 la determina. Ritorna (sezione, warning, mancanti)."""
    d2k = _desc_to_key(subjects)
    recs, gone = _present(reg, CR.SOURCE, "lesson")
    inc, poss = [], []
    n_tot = len(recs)
    for full in recs:
        raw = full["raw"]
        if not _teacher_ok(raw.get("authorName"), docente):
            continue
        a = CA.attribute_lesson(raw, subjects)
        c = _classify(a, key, d2k)
        if c == "altro":
            continue
        uid = cv.normalize_lesson(raw)["uid"]
        rec = {"uid": uid,
               "dati": {"data": str(raw.get("evtDate") or "")[:10] or None, "ora": raw.get("evtHPos"),
                        "docente": raw.get("authorName"), "materia_api": raw.get("subjectDesc"),
                        "tipo": raw.get("lessonType"), "argomento": (raw.get("lessonArg") or "").strip() or None},
               "interpretazione": _interp(a, include_raw or _flagged(a)),
               "fonte": _cite(full, uid)}
        if include_raw:
            rec["raw"] = raw
        (inc if c == "incluso" else poss).append(rec)
    order = lambda r: (r["dati"]["data"] or "", r["dati"]["ora"] or 0, r["uid"])           # noqa: E731
    inc.sort(key=order, reverse=True)
    poss.sort(key=order, reverse=True)
    rows, meta = SC._capped(inc, limit or LIMITS["lezioni"], "operativo", "lezioni")
    sec = {"livello": LEVELS, "record": rows, "conteggio": dict(meta, nel_registro=n_tot, scomparse_dalla_fonte=gone),
           "senza_argomento": sum(1 for r in inc if not r["dati"]["argomento"])}
    warn, miss = [], []
    flagged_conf = [r["uid"] for r in inc if r["interpretazione"]["conflitti"]]
    flagged_av = [r["uid"] for r in inc if r["interpretazione"]["avvisi"]]
    _truncation_warning(warn, "lezioni", "lezioni", meta)
    if poss:
        _w(warn, "attribuzione_da_verificare", "attenzione", "lezioni",
           "lezioni con materia 'da verificare' che potrebbero essere di questa materia: NON incluse nella materia",
           [r["uid"] for r in poss])
    if flagged_conf:
        _w(warn, "conflitto_attribuzione", "attenzione", "lezioni",
           "conflitto tra subjectDesc e testo dell'argomento: materia NON modificata", flagged_conf)
    if flagged_av:
        _w(warn, "avviso_attribuzione", "info", "lezioni", "avvisi sulle lezioni (es. supplenza)", flagged_av)
    if not recs:
        miss.append({"sezione": "lezioni", "messaggio": "nessuna lezione ClasseViva nel registro (kind 'lesson' non "
                                                       "sincronizzato o vuoto)"})
    elif not inc and not poss:
        miss.append({"sezione": "lezioni", "messaggio": "nessuna lezione attribuita con certezza a questa materia"
                                                       + (" per il docente indicato" if docente else "")})
    if inc and sec["senza_argomento"]:
        miss.append({"sezione": "lezioni", "messaggio": "%d lezioni senza argomento compilato" % sec["senza_argomento"]})
    sec["possibili_da_verificare"] = poss
    return sec, warn, miss


# ------------------------------------------------------------------------------------------------ agenda
def agenda_section(reg, key, subjects, now, docente=None, include_raw=False, past_limit=None):
    d2k = _desc_to_key(subjects)
    lessons, _ = _present(reg, CR.SOURCE, "lesson")
    idx = CA.lessons_index([f["raw"] for f in lessons])
    evs, gone = _present(reg, CR.SOURCE, "agenda_event")
    today = D.parse_now(now).astimezone(D._zone()).date().isoformat()
    inc, poss, classes = [], [], set()
    for full in evs:
        n = CR.normalized("agenda_event", full["raw"])
        if full["raw"].get("classDesc"):
            classes.add(full["raw"]["classDesc"])
        if not _teacher_ok(n.get("author"), docente):
            continue
        a = CA.attribute_event(n, subjects, idx)
        c = _classify(a, key, d2k)
        if c == "altro":
            continue
        rec = {"uid": n["uid"],
               "dati": {"data": n["date"], "inizio": n["start"], "fine": n["end"], "testo": n["text"],
                        "docente": n.get("author"), "materia_api": n.get("subject_api")},
               "interpretazione": dict(_interp(a, True), tipo_dedotto=n["kind"]),
               "fonte": _cite(full, n["uid"])}
        if include_raw:
            rec["raw"] = full["raw"]
        (inc if c == "incluso" else poss).append(rec)
    by = lambda tipo: sorted((r for r in inc if r["interpretazione"]["tipo_dedotto"] == tipo),         # noqa: E731
                             key=lambda r: (r["dati"]["data"] or "", r["uid"]))
    tests = by("verifica")
    sec = {"livello": LEVELS,
           "verifiche_future": [t for t in tests if (t["dati"]["data"] or "") >= today],
           "verifiche_passate": [t for t in tests if (t["dati"]["data"] or "") < today][-(past_limit or SC.LIMITS["past_tests"]):],
           "compiti": by("compito"), "eventi": by("evento"),
           "possibili_da_verificare": sorted(poss, key=lambda r: (r["dati"]["data"] or "", r["uid"])),
           "conteggio": {"nel_registro": len(evs), "scomparsi_dalla_fonte": gone, "della_materia": len(inc)},
           "nota": "il tipo (verifica/compito/evento) e' DEDOTTO da codice evento e testo (interpretazione)"}
    warn, miss = [], []
    n_past = sum(1 for t in tests if (t["dati"]["data"] or "") < today)
    n_shown = len(sec["verifiche_passate"])
    _truncation_warning(warn, "agenda", "verifiche passate", {"troncato": n_past > n_shown, "inclusi": n_shown,
                                                              "totale": n_past, "omessi": n_past - n_shown})
    if poss:
        _w(warn, "attribuzione_da_verificare", "attenzione", "agenda",
           "eventi con materia 'da verificare' che potrebbero essere di questa materia: NON inclusi nella materia",
           [r["uid"] for r in poss])
    conf = [r["uid"] for r in inc if r["interpretazione"]["conflitti"]]
    if conf:
        _w(warn, "conflitto_attribuzione", "attenzione", "agenda",
           "conflitto di attribuzione (testo/docente o evento/lezioni): materia NON modificata", conf)
    if not evs:
        miss.append({"sezione": "agenda", "messaggio": "nessun evento di agenda ClasseViva nel registro"})
    elif not inc:
        miss.append({"sezione": "agenda", "messaggio": "nessun evento di agenda attribuito con certezza a questa materia"
                                                      + (" per il docente indicato" if docente else "")})
    return sec, warn, miss, sorted(classes)


# ------------------------------------------------------------------------------------------------ voti
def grades_section(x):
    g = x["voti_classeviva"]
    rows = [{"uid": r["uid"], "dati": {"data": r["data"], "nome": r["nome"], "raw": r["raw"]},
             "interpretazione": r["interpretazione"], "fonte": r["fonte"]} for r in g["materia"]]
    return {"livello": LEVELS, "record": rows, "conteggio": {"della_materia": len(rows), "nel_registro": g["voti_totali_registro"]},
            "nota": "0 voti = stato vuoto valido, non un dato mancante" if not rows else None}


# ------------------------------------------------------------------------------------------------ costruzione
def build(reg, subject, subjects, now, manual=None, docente=None, include_raw=False, source_path=None, soglia_ore=None):
    """cloud.school.context/3 per UNA delle 8 materie. Sola lettura; stesso registro + stesso now -> stesso JSON."""
    if docente is not None and not (isinstance(docente, str) and docente.strip()):
        raise ValueError("docente non valido: %r" % (docente,))
    now_dt = D.parse_now(now)
    x = SC.build(reg, subject, subjects, now_dt, manual=manual, include_raw=include_raw, source_path=source_path)   # v2
    key = x["materia"]["chiave"]
    fresh = reg.freshness(now_dt.isoformat(), stale_after_hours=soglia_ore)
    warn, miss = [], []

    lez, w, m = lessons_section(reg, key, subjects, now_dt, docente=docente, include_raw=include_raw)
    warn += w
    miss += m
    age, w, m, classes = agenda_section(reg, key, subjects, now_dt, docente=docente, include_raw=include_raw)
    warn += w
    miss += m
    voti = grades_section(x)
    if not voti["record"]:
        miss.append({"sezione": "voti", "messaggio": "nessun voto per questa materia (0 voti = stato vuoto valido)"})

    conf, prop = x["classroom"]["confermato"], x["classroom_proposto"]
    classroom = {"livello": "derivato (L3): compito+consegna sono una vista del RAW; 'fonte' cita i record RAW",
                 "fonte": x["classroom"]["fonte"], "corsi_registrati": x["classroom"]["corsi_registrati"],
                 "confermato": conf, "proposto": prop, "nota": x["classroom"]["nota"]}
    if "stato" in x["classroom"]:
        classroom["stato"] = x["classroom"]["stato"]
        miss.append({"sezione": "classroom", "messaggio": x["classroom"]["stato"]})
    elif not conf["corsi"]:
        miss.append({"sezione": "classroom", "messaggio": "nessun corso Classroom CONFERMATO per questa materia "
                                                         "(conferme solo in school_manual.json)"})
    for nome, blocco in (("confermato", conf), ("proposto", prop)):
        for voce in ("compiti", "annunci", "materiali"):
            _truncation_warning(warn, "classroom", "classroom %s: %s" % (nome, voce), blocco.get(voce + "_conteggio"))
    if prop["corsi"]:
        _w(warn, "corsi_classroom_proposti_non_confermati", "info", "classroom",
           "%d corsi PROPOSTI automaticamente e non confermati: non sono fatti della materia" % len(prop["corsi"]),
           [c["course_id"] for c in prop["corsi"]])
    for s in x["da_verificare"]:
        if "eventi con materia 'da verificare'" in s:           # gia' coperto dall'attribuzione M2 dell'agenda
            continue
        _w(warn, "da_verificare_classroom", "attenzione", "classroom", s)
    if not x["livello_manuale"]["presente"]:
        miss.append({"sezione": "livello_manuale", "messaggio": "school_manual.json assente o vuoto: nessuna conferma "
                                                              "di corsi Classroom e nessuna dichiarazione dell'utente"})
    n_notice = len(reg.list_records(source=CR.SOURCE, kind="notice", status=R.PRESENT))
    if n_notice:
        _w(warn, "comunicazioni_non_attribuibili", "info", "comunicazioni",
           "%d comunicazioni ClasseViva non hanno una materia: non incluse in nessun contesto di materia" % n_notice)
    miss.append({"sezione": "materiali_e_appunti", "messaggio": x["materiali_e_appunti"]["stato"]})

    sezioni = {"lezioni": lez, "agenda": age, "voti": voti, "classroom": classroom}
    for name, sec in sezioni.items():
        sec["freschezza"] = section_freshness(fresh, SECTION_KINDS[name])
        _freshness_warnings(warn, name, sec["freschezza"])

    seen = sorted({r["dati"]["docente"] for r in lez["record"] if r["dati"]["docente"]})
    return {
        "schema": SCHEMA,
        "compatibilita": {"v2": "cloud.school.context/2 resta disponibile e invariato nel tool separato school_context (nessun parametro di versione)",
                          "v3_riusa_v2_per": ["classroom (L3)", "voti"],
                          "differenze_da_v2": ["lezioni (nuove)", "attribuzione M2 al posto del filtro testuale per l'agenda",
                                               "freschezza per sezione", "warning strutturati", "informazioni_mancanti",
                                               "dati/interpretazione separati in ogni record"]},
        "livelli": LEVELS,
        "riferimento_temporale": now_dt.isoformat(),
        "data_riferimento": x["data_riferimento"],
        "identita": {
            "classe": {"perimetro_operativo": x["ambito"]["perimetro_operativo"],
                       "classi_osservate_in_agenda": classes,
                       "anno_scolastico": SC._school_year_of_ts(now_dt.isoformat())},
            "materia": x["materia"]["chiave"], "nome_ufficiale": x["materia"]["nome_ufficiale"],
            "docenti": {"registro_subjects": x["materia"]["docenti_registro"], "osservati_nelle_lezioni": seen,
                        "filtro_docente": docente}},
        "sezioni": sezioni,
        "freschezza": {"calcolata_a": fresh["calcolato_a"], "soglia_ore": fresh["soglia_ore"],
                       "per_sezione": {n: s["freschezza"]["stato"] for n, s in sezioni.items()},
                       "qualcosa_stale": fresh["qualcosa_stale"], "nota": FRESH_NOTE},
        "livello_manuale": x["livello_manuale"],
        "warning": warn,
        "informazioni_mancanti": miss,
        "provenienza": dict(x["provenienza"], attr_version=CA.attr_version(), schema_registro=R._version_of(reg.db),
                            registro_migrato_in_memoria=bool(getattr(reg, "migrated_in_memory", False))),
        "regole_per_gli_agenti": x["regole_per_gli_agenti"] + RULES_V3,
    }


def build_from_path(path, subject, subjects, now, manual_path=None, docente=None, include_raw=False, soglia_ore=None):
    reg = R.Registry.from_snapshot(path)
    try:
        import os
        return build(reg, subject, subjects, now, manual=SC.load_manual(manual_path)["dati"], docente=docente,
                     include_raw=include_raw, source_path=os.path.abspath(path), soglia_ore=soglia_ore)
    finally:
        reg.close()


# ------------------------------------------------------------------------------------------------ overview
def overview(reg, subjects, now, manual=None, soglia_ore=None):
    """cloud.school.overview/1: stato scolastico sintetico, in sola lettura. NON sostituisce il registro: e' un indice."""
    now_dt = D.parse_now(now)
    today = now_dt.astimezone(D._zone()).date().isoformat()
    fresh = reg.freshness(now_dt.isoformat(), stale_after_hours=soglia_ore)
    d2k = _desc_to_key(subjects)
    keys = list(SC.AGENT_SUBJECTS)
    warn, miss = [], []

    per_kind = {}
    for r in reg.list_records():
        k = "%s/%s" % (r["source"], r["kind"])
        e = per_kind.setdefault(k, {"present": 0, "missing_in_source": 0})
        e["present" if r["source_status"] == R.PRESENT else "missing_in_source"] += 1
    freshness = {}
    for k, e in sorted(fresh["per_kind"].items()):
        freshness[k] = {"motivo": e["motivo"], "stale": e["stale"], "eta_ore": e.get("eta_ore"),
                        "legacy": bool((e.get("ultimo_run") or {}).get("legacy"))}
    stale_kinds = sorted(k for k, e in fresh["per_kind"].items() if e["stale"])

    lessons, _ = _present(reg, CR.SOURCE, "lesson")
    events, _ = _present(reg, CR.SOURCE, "agenda_event")
    idx = CA.lessons_index([f["raw"] for f in lessons])
    L = [(cv.normalize_lesson(f["raw"])["uid"], CA.attribute_lesson(f["raw"], subjects)) for f in lessons]
    E = []
    for f in events:
        n = CR.normalized("agenda_event", f["raw"])
        E.append((n, CA.attribute_event(n, subjects, idx)))
    attr = {}
    for name, rows in (("lezioni", L), ("agenda", [(n["uid"], a) for n, a in E])):
        attr[name] = {"totale": len(rows),
                      "determinate": sum(1 for _, a in rows if a["stato"] == CA.ST_OK),
                      "da_verificare": sum(1 for _, a in rows if a["stato"] != CA.ST_OK),
                      "con_conflitti": sum(1 for _, a in rows if a["conflitti"]),
                      "con_avvisi": sum(1 for _, a in rows if a["avvisi"])}

    confirmed = CS.confirmed_map(manual or {})
    l3 = D.from_registry(reg, now_dt, manual={"course_subjects": {
        cid: [{"subject": k} for k in sorted(ks)] for cid, ks in confirmed.items()}})
    open_by, tasks_by = {k: 0 for k in keys}, {k: 0 for k in keys}
    for v in l3["work_views"]:
        if v["derivato"]["perimetro"]["valore"] != "operativo":
            continue
        if (v["provenienza"]["course_work"] or {}).get("stato_registro") not in (None, R.PRESENT):
            continue
        for k in confirmed.get(v["fonte"]["corso"]["id"], ()):
            if k in tasks_by:
                tasks_by[k] += 1
                if v["derivato"]["stato_operativo"] == "aperto":
                    open_by[k] += 1

    materie = []
    for k in keys:
        l_inc = [uid for uid, a in L if _classify(a, k, d2k) == "incluso"]
        l_pos = [uid for uid, a in L if _classify(a, k, d2k) == "possibile"]
        e_inc = [(n, a) for n, a in E if _classify(a, k, d2k) == "incluso"]
        e_pos = [n["uid"] for n, a in E if _classify(a, k, d2k) == "possibile"]
        future = sorted((n["date"], n["uid"]) for n, a in e_inc if n["kind"] == "verifica" and (n["date"] or "") >= today)
        conf_n = sum(1 for _, a in L if a["conflitti"] and _classify(a, k, d2k) == "incluso") + \
            sum(1 for _, a in e_inc if a["conflitti"])
        corsi = sorted(cid for cid, ks in confirmed.items() if k in ks)
        materie.append({"materia": k, "lezioni": len(l_inc), "agenda_eventi": len(e_inc),
                        "verifiche_future": len(future),
                        "prossima_verifica": {"data": future[0][0], "uid": future[0][1]} if future else None,
                        "classroom": {"corsi_confermati": len(corsi), "compiti_operativi": tasks_by[k],
                                      "compiti_aperti": open_by[k]},
                        "da_verificare": len(l_pos) + len(e_pos), "conflitti": conf_n})
        if not corsi:
            miss.append({"sezione": "classroom", "materia": k, "messaggio": "nessun corso Classroom confermato"})
    n_out = sum(1 for _, a in L if a["stato"] == CA.ST_OK and d2k.get(S.norm(a["materia_derivata"] or "")) not in keys) + \
        sum(1 for _, a in E if a["stato"] == CA.ST_OK and d2k.get(S.norm(a["materia_derivata"] or "")) not in keys)

    if stale_kinds:
        _w(warn, "kind_stale_o_senza_lettura", "attenzione", "freschezza",
           "kind stale o senza lettura riuscita: %s" % ", ".join(stale_kinds))
    if any(e["legacy"] for e in freshness.values()):
        _w(warn, "dati_legacy", "info", "freschezza", "run precedenti a M3 (fine/durata assenti) per: %s" % ", ".join(
            k for k, e in freshness.items() if e["legacy"]))
    for name in ("lezioni", "agenda"):
        if attr[name]["da_verificare"]:
            _w(warn, "attribuzione_da_verificare", "attenzione", name,
               "%d record con materia da verificare (non attribuiti a nessuna materia)" % attr[name]["da_verificare"])
        if attr[name]["con_conflitti"]:
            _w(warn, "conflitto_attribuzione", "attenzione", name, "%d record con conflitti di attribuzione" % attr[name]["con_conflitti"])
    n_notice = len(reg.list_records(source=CR.SOURCE, kind="notice", status=R.PRESENT))
    if n_notice:
        _w(warn, "comunicazioni_non_attribuibili", "info", "comunicazioni",
           "%d comunicazioni ClasseViva senza materia: non incluse in nessun contesto di materia" % n_notice)
    if not manual:
        miss.append({"sezione": "livello_manuale", "messaggio": "school_manual.json assente o vuoto: nessun corso Classroom confermato"})
    if not lessons:
        miss.append({"sezione": "lezioni", "messaggio": "nessuna lezione nel registro"})

    return {
        "schema": OVERVIEW_SCHEMA,
        "natura": "indice derivato in sola lettura: NON sostituisce il registro, non e' fonte di verita'",
        "riferimento_temporale": now_dt.isoformat(), "data_riferimento": today,
        "registro": {"schema": R._version_of(reg.db), "atteso": R.SCHEMA_VERSION,
                     "migrato_in_memoria": bool(getattr(reg, "migrated_in_memory", False)),
                     "record_per_kind": dict(sorted(per_kind.items()))},
        "freschezza": {"soglia_ore": fresh["soglia_ore"], "qualcosa_stale": fresh["qualcosa_stale"],
                       "per_kind": freshness, "nota": FRESH_NOTE},
        "attribuzione": dict(attr, attr_version=CA.attr_version(),
                             record_di_materie_fuori_dal_sistema_agenti=n_out,
                             nota="M2: materia assegnata solo se 'determinata'; il resto e' 'da verificare'"),
        "materie": materie,
        "classroom": {"corsi_registrati": len([1 for r in reg.list_records(source="classroom", kind="course", status=R.PRESENT)]),
                      "corsi_confermati": len(confirmed)},
        "warning": warn, "informazioni_mancanti": miss,
        "regole": RULES_V3,
    }


def overview_from_path(path, subjects, now, manual_path=None, soglia_ore=None):
    reg = R.Registry.from_snapshot(path)
    try:
        return overview(reg, subjects, now, manual=SC.load_manual(manual_path)["dati"], soglia_ore=soglia_ore)
    finally:
        reg.close()
