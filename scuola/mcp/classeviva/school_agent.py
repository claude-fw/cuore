"""SUPERFICIE PER GLI AGENTI DI MATERIA: brief compatto + recupero su richiesta. Modulo PURO e SOLA LETTURA.

    Registry (L1) -> L3 cls_derive -> cls_subjects + school_manual.json -> school_context._build (vista completa)
        -> school_agent.brief(materia)            contesto di AVVIO dell'agente (compatto, deterministico)
        -> school_agent.tasks/task/announcements/materials   dettagli SU RICHIESTA (stessi limiti di materia)

Regole (verificate dai test):
  * una sola materia per chiamata, solo tra le 8 materie degli agenti (le escluse sono rifiutate);
  * SOLO corsi CONFERMATI in school_manual.json; i corsi proposti compaiono nel brief solo come nome + conteggi
    (e solo se hanno contenuto operativo); esclusi / non attribuiti / biennio / storico: mai;
  * SOLO perimetro operativo (3a/4a); lo storico resta nel registro e non e' raggiungibile da qui;
  * nessun dato di altre materie: il recupero rifiuta uid di corsi non confermati per la materia richiesta;
  * citazioni brevi `ref` = uid@versione; il dettaglio (school_task) riporta hash e provenienza completi;
  * estratti DETERMINISTICI dai dati (nessun riassunto generato): stesso registro + stesso now -> stesso JSON;
  * nessuna scrittura: niente registro, niente file manuale.
La vista completa `school_context` resta per diagnosi e test: NON e' il contesto di avvio dell'agente.
"""
from __future__ import annotations

import school_context as SC
import school_context3 as C3
import cvv_subject as subj

SCHEMA = "cloud.school.brief/2"      # /2 = /1 + chiavi additive "lezioni" e "freschezza" (nessuna chiave rimossa)
BRIEF = {"recent_tasks": 8, "announcements": 8, "announcement_chars": 280, "materials": 10, "past_tests": 3,
         "lessons": 8, "lesson_chars": 160}
RETRIEVAL_MAX = {"tasks": 200, "announcements": 50, "materials": 100}
_ALL = {"course_work": 100000, "announcements": 100000, "materials": 100000}

RULES = [
    "Sola lettura: non modificare registro, Classroom o ClasseViva.",
    "Usa solo i dati di questa materia; i corsi in 'corsi_proposti' NON sono confermati: non trattarli come fatti.",
    "Non inventare voti, date, docenti o contenuti: se un dato manca o e' 'da verificare', dillo.",
    "stato_operativo e' tecnico: 'senza_scadenza' non significa 'da fare', 'scaduto_non_consegnato' non significa 'da recuperare'.",
    "I punti Classroom (assigned_points) NON sono voti scolastici; i voti ufficiali sono solo quelli ClasseViva.",
    "Cita i dati con il loro 'ref' (uid@versione); per testo completo, allegati e hash usa gli strumenti di recupero.",
    "Un compito con 'condiviso_con' appartiene a un corso condiviso con altre materie: non attribuirlo arbitrariamente.",
    "Guarda 'freschezza': se una sezione e' 'stale' o 'senza_lettura' dichiaralo; 'aggiornato' non garantisce correttezza.",
    "Le lezioni sono della materia solo se l'attribuzione M2 e' determinata; quelle 'da_verificare' non sono incluse.",
]
RETRIEVAL = {
    "school_tasks(subject, stato?)": "elenco compatto di tutti i compiti operativi dei corsi confermati, filtrabile per stato_operativo",
    "school_task(subject, uid)": "dettaglio di un compito: descrizione, allegati, link, consegna, punti, provenienza completa",
    "school_announcements(subject, cerca?, limite?)": "annunci operativi dei corsi confermati con testo completo, ricerca per parola",
    "school_materials(subject, argomento?, cerca?, limite?)": "materiali operativi dei corsi confermati con allegati",
}


# ------------------------------------------------------------------------------------------------ helper
def _full(reg, subject, subjects, now, manual, everything=False):
    """Vista completa (operativa) da school_context. everything=True: senza tetti, per il recupero."""
    return SC._build(reg, subject, subjects, now, manual=manual, limits=_ALL if everything else None)


def _ref(cite):
    return None if not cite else "%s@v%s" % (cite["uid"], cite["registry_version"])


def _clip(text, n):
    t = " ".join((text or "").split())
    if len(t) <= n:
        return t
    cut = t[:n].rsplit(" ", 1)[0]
    return cut + " …"


def _when(scad):
    return scad.get("momento_locale") or scad.get("data_locale")


def _task_brief(t):
    out = {"ref": _ref(t["fonte"]["course_work"]), "titolo": t["titolo"], "corso": t["corso"]["nome"],
           "scadenza": _when(t["scadenza"]), "giorni_mancanti": t["scadenza"]["giorni_mancanti"],
           "stato": t["stato_operativo"], "consegna": t["consegna"]["stato_fonte"],
           "allegati": len(t["materiali"] or [])}
    if t["consegna"]["late"]:
        out["late"] = True
    if t["corso"]["condiviso_con"]:
        out["condiviso_con"] = t["corso"]["condiviso_con"]
    if t["da_verificare"]:
        out["da_verificare"] = t["da_verificare"]
    return out


def _course_name(conf, cid):
    for c in conf["corsi"]:
        if c["course_id"] == cid:
            return c["nome"]
    return None


def _ann_brief(a, conf, chars):
    out = {"ref": _ref(a["fonte"]), "data": (a.get("created") or "")[:10], "corso": _course_name(conf, a.get("course_id")),
           "testo": _clip(a.get("text"), chars), "allegati": len(a.get("materials") or [])}
    if a.get("condiviso_con"):
        out["condiviso_con"] = a["condiviso_con"]
    return out


def _mat_brief(m, conf, topics):
    return {"ref": _ref(m["fonte"]), "data": (m.get("created") or "")[:10], "corso": _course_name(conf, m.get("course_id")),
            "titolo": m.get("title"), "argomento": topics.get((m.get("course_id"), m.get("topic_id"))),
            "allegati": len(m.get("materials") or [])}


def _cv_event(e):
    return {"uid": e["uid"], "data": e["data"], "inizio": e.get("inizio"), "testo": e.get("testo"), "docente": e.get("docente")}


# ------------------------------------------------------------------------------------------------ brief
def brief(reg, subject, subjects, now, manual=None):
    """Contesto di AVVIO di un agente di materia: compatto, solo corsi confermati, solo perimetro operativo."""
    x = _full(reg, subject, subjects, now, manual)
    conf, prop = x["classroom"]["confermato"], x["classroom_proposto"]
    tasks = conf["compiti"]                                   # gia' ordinati: aperti in testa, poi piu' recenti
    active = [t for t in tasks if t["stato_operativo"] in ("aperto", "da_verificare")]
    others = [t for t in tasks if t["stato_operativo"] not in ("aperto", "da_verificare")]
    by_state = {}
    for t in tasks:
        by_state[t["stato_operativo"]] = by_state.get(t["stato_operativo"], 0) + 1
    topics = {(t["course_id"], t["topic_id"]): t["nome"] for t in conf["argomenti"]}
    prop_courses = []
    hidden = 0
    for c in prop["corsi"]:
        n_ops = sum(1 for t in prop["compiti"] if t["corso"]["id"] == c["course_id"])
        n_ann = sum(1 for a in prop["annunci"] if a.get("course_id") == c["course_id"])
        n_mat = sum(1 for m in prop["materiali"] if m.get("course_id") == c["course_id"])
        if n_ops + n_ann + n_mat == 0:
            hidden += 1
            continue
        prop_courses.append({"course_id": c["course_id"], "nome": c["nome"], "materie_proposte": c.get("materie_proposte"),
                             "compiti_operativi": n_ops, "annunci_operativi": n_ann, "materiali_operativi": n_mat})
    now_dt = SC.D.parse_now(now)
    lez, _lw, _lm = C3.lessons_section(reg, x["materia"]["chiave"], subjects, now_dt)
    fresh = reg.freshness(now_dt.isoformat())
    fsec = {n: C3.section_freshness(fresh, k) for n, k in C3.SECTION_KINDS.items()}
    lezioni = {"recenti": [{"ref": _ref(r["fonte"]), "data": r["dati"]["data"], "ora": r["dati"]["ora"],
                            "docente": r["dati"]["docente"], "argomento": _clip(r["dati"]["argomento"], BRIEF["lesson_chars"])}
                           for r in lez["record"][:BRIEF["lessons"]]],
               "totale_determinate": lez["conteggio"]["totale"], "senza_argomento": lez["senza_argomento"],
               "da_verificare_non_incluse": len(lez["possibili_da_verificare"]),
               "con_conflitti": sum(1 for r in lez["record"] if r["interpretazione"]["conflitti"])}
    freschezza = {"per_sezione": {n: f["stato"] for n, f in fsec.items()},
                  "legacy": any(f["legacy"] for f in fsec.values()),
                  "qualcosa_stale": any(f["stato"] != "aggiornato" for f in fsec.values()),
                  "soglia_ore": fresh["soglia_ore"], "nota": C3.FRESH_NOTE}
    agenda = x["agenda_classeviva"]
    cv = {"verifiche_future": [_cv_event(e) for e in agenda["verifiche_future"]],
          "verifiche_passate_recenti": [_cv_event(e) for e in agenda["verifiche_passate"][-BRIEF["past_tests"]:]],
          "compiti": [_cv_event(e) for e in agenda["compiti"]],
          "eventi": len(agenda["eventi"]),
          "voti": [{"uid": g["uid"], "data": g["data"], "voto": (g.get("raw") or {}).get("displayValue"),
                    "tipo": (g.get("raw") or {}).get("componentDesc")} for g in x["voti_classeviva"]["materia"]],
          "possibili_da_verificare": [{"uid": p["uid"], "data": p["data"], "testo": p.get("testo"),
                                       "candidati": p.get("candidati")} for p in x["possibili_da_verificare"]]}
    return {
        "schema": SCHEMA,
        "materia": {"chiave": x["materia"]["chiave"], "nome_ufficiale": x["materia"]["nome_ufficiale"],
                    "docenti": x["materia"]["docenti_registro"]},
        "riferimento_temporale": x["riferimento_temporale"],
        "perimetro": {"classi": x["ambito"]["perimetro_operativo"],
                      "nota": "solo perimetro operativo; lo storico non e' disponibile per gli agenti"},
        "corsi_confermati": [{"course_id": c["course_id"], "nome": c["nome"], "attivo": c["stato"] == "ACTIVE",
                              "condiviso_con": c["condiviso_con"]} for c in conf["corsi"]],
        "corsi_proposti": prop_courses,
        "corsi_proposti_nascosti_senza_contenuto_operativo": hidden,
        "compiti": {"attivi": [_task_brief(t) for t in active],
                    "recenti": [_task_brief(t) for t in others[:BRIEF["recent_tasks"]]],
                    "per_stato": dict(sorted(by_state.items())), "totale": len(tasks),
                    "altri_non_mostrati": max(0, len(others) - BRIEF["recent_tasks"]),
                    "consegne_orfane": len(conf["consegne_orfane"])},
        "annunci": {"recenti": [_ann_brief(a, conf, BRIEF["announcement_chars"]) for a in conf["annunci"][:BRIEF["announcements"]]],
                    "totale_operativi": conf["annunci_conteggio"]["totale"]},
        "materiali": {"recenti": [_mat_brief(m, conf, topics) for m in conf["materiali"][:BRIEF["materials"]]],
                      "totale_operativi": conf["materiali_conteggio"]["totale"]},
        "argomenti": sorted({t["nome"] for t in conf["argomenti"] if t.get("nome")}),
        "classeviva": cv,
        "lezioni": lezioni,
        "freschezza": freschezza,
        "da_verificare": x["da_verificare"],
        "regole": RULES,
        "recupero_su_richiesta": RETRIEVAL,
        "provenienza": {"derive_version": x["provenienza"]["derive_version"], "context_schema": x["schema"],
                        "ref": "uid@versione del registro; hash completo con school_task"},
    }


# ------------------------------------------------------------------------------------------------ recupero
def _limit(n, maxv, default):
    if n is None:
        return default
    if not isinstance(n, int) or n < 1:
        raise ValueError("limite non valido: %r" % (n,))
    return min(n, maxv)


def _match(text, q):
    return subj.norm(q) in subj.norm(text or "")


def tasks(reg, subject, subjects, now, manual=None, stato=None):
    """Tutti i compiti operativi dei corsi CONFERMATI della materia (compatti), opz. filtrati per stato_operativo."""
    x = _full(reg, subject, subjects, now, manual, everything=True)
    rows = x["classroom"]["confermato"]["compiti"]
    if stato is not None:
        rows = [t for t in rows if t["stato_operativo"] == stato]
    rows = rows[:RETRIEVAL_MAX["tasks"]]
    return {"materia": x["materia"]["chiave"], "filtro_stato": stato, "compiti": [_task_brief(t) for t in rows],
            "totale": len(rows)}


def task(reg, subject, subjects, now, manual=None, uid=None):
    """Dettaglio di UN compito, solo se appartiene a un corso CONFERMATO della materia e al perimetro operativo."""
    uid = (uid or "").split("@")[0]
    x = _full(reg, subject, subjects, now, manual, everything=True)
    found = [t for t in x["classroom"]["confermato"]["compiti"] if t["uid"] == uid]
    if not found:
        raise ValueError("compito %r non disponibile per la materia '%s' (non nei corsi confermati o fuori dal "
                         "perimetro operativo)" % (uid, x["materia"]["chiave"]))
    t = dict(found[0])
    cid, wid = uid[len("cls:"):].split("/", 1)
    rec = reg.get_record("classroom", "course_work", "%s/%s" % (cid, wid))
    t["descrizione"] = (rec["raw"].get("description") if rec else None)
    t["ref"] = _ref(t["fonte"]["course_work"])
    return {"materia": x["materia"]["chiave"], "compito": t}


def announcements(reg, subject, subjects, now, manual=None, cerca=None, limite=None):
    """Annunci operativi dei corsi CONFERMATI (testo completo), piu' recenti prima, opz. ricerca per parola."""
    lim = _limit(limite, RETRIEVAL_MAX["announcements"], 20)
    x = _full(reg, subject, subjects, now, manual, everything=True)
    conf = x["classroom"]["confermato"]
    rows = [a for a in conf["annunci"] if not cerca or _match(a.get("text"), cerca)]
    out = [{"ref": _ref(a["fonte"]), "data": (a.get("created") or "")[:10], "corso": _course_name(conf, a.get("course_id")),
            "testo": a.get("text"), "link": a.get("alternate_link"),
            "allegati": [{"titolo": m.get("title"), "tipo": m.get("type"), "url": m.get("url")} for m in a.get("materials") or []],
            "condiviso_con": a.get("condiviso_con")} for a in rows[:lim]]
    return {"materia": x["materia"]["chiave"], "cerca": cerca, "annunci": out, "totale": len(rows),
            "troncato": len(rows) > lim}


def materials(reg, subject, subjects, now, manual=None, argomento=None, cerca=None, limite=None):
    """Materiali operativi dei corsi CONFERMATI con allegati, opz. filtro per argomento (nome) o parola."""
    lim = _limit(limite, RETRIEVAL_MAX["materials"], 30)
    x = _full(reg, subject, subjects, now, manual, everything=True)
    conf = x["classroom"]["confermato"]
    topics = {(t["course_id"], t["topic_id"]): t["nome"] for t in conf["argomenti"]}
    rows = []
    for m in conf["materiali"]:
        top = topics.get((m.get("course_id"), m.get("topic_id")))
        if argomento and not _match(top, argomento):
            continue
        if cerca and not (_match(m.get("title"), cerca) or _match(m.get("description"), cerca)):
            continue
        rows.append((m, top))
    out = [{"ref": _ref(m["fonte"]), "data": (m.get("created") or "")[:10], "corso": _course_name(conf, m.get("course_id")),
            "titolo": m.get("title"), "argomento": top, "descrizione": m.get("description"), "link": m.get("alternate_link"),
            "allegati": [{"titolo": a.get("title"), "tipo": a.get("type"), "url": a.get("url")} for a in m.get("materials") or []],
            "condiviso_con": m.get("condiviso_con")} for m, top in rows[:lim]]
    return {"materia": x["materia"]["chiave"], "argomento": argomento, "cerca": cerca, "materiali": out,
            "totale": len(rows), "troncato": len(rows) > lim}
