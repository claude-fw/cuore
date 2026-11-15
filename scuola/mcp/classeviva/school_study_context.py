"""CONTESTO DI STUDIO PER MATERIA (M11 v1): `cloud.school.study_context/1`. Modulo PURO e SOLA LETTURA.

M11 NON e' uno Study Assistant: non insegna, non spiega, non pianifica, non assegna tempi, non diagnostica, non giudica. Raccoglie per
UNA materia il contesto GIA' disponibile attorno agli obiettivi (verifiche e compiti delle azioni M10) e le lezioni svolte, con una
classificazione esplicita di ogni dato.

Input (D-M11-2): SOLO i contratti pubblici di M10 (`school_action.build(materia, dettaglio=True)`, che include M9 transitivamente) e di
M8 (`school_status.build(materia, dettaglio=True)`, per le lezioni S4, la copertura, lo storico e le ambiguita'). Nessuna lettura
diretta di M1-M7 o M9, nessun ricalcolo di segnali, priorita', affidabilita' o giorni.
Decisioni vincolanti (2026-10-06):
  * D-M11-1 materia obbligatoria, nessuna panoramica;
  * D-M11-3 obiettivi 1:1 con le azioni M10 controllare_verifica / controllare_compito; le lezioni sono contesto della MATERIA e
    non vengono MAI legate a un obiettivo;
  * D-M11-4 usi formali chiusi U1-U4; U4 `argomenti_dell_obiettivo` SEMPRE non_determinabile in v1;
  * D-M11-5 materiali SOLO dai collegamenti M5 esposti da M10; nessun materiale M4/school_agent;
  * D-M11-6 `data` e `priorita_m9` riportate come ricevute.
Stati del dato (chiusi): dato_disponibile | dato_rilevante | dato_sufficiente (solo per un uso formale) | non_determinabile |
non_disponibile (motivo: fonte_non_letta | non_presente_nella_fonte | vuoto_valido). Affidabilita' mai sopra la fonte (monotona).
Stesso input -> stesso JSON; nessun orologio, nessuna scrittura.
"""
from __future__ import annotations

import os

import cvv_client as cv
import registry as R
import school_action as AC
import school_context as SC
import school_status as ST

SCHEMA = "cloud.school.study_context/1"
STUDY_REV = 1

DATA_STATES = ("dato_disponibile", "dato_rilevante", "dato_sufficiente", "non_determinabile", "non_disponibile")
UNAVAILABLE_REASONS = ("fonte_non_letta", "non_presente_nella_fonte", "vuoto_valido")
USES = ("identificare_obiettivo", "testo_dichiarato_disponibile", "materiale_collegato_identificabile", "argomenti_dell_obiettivo")
OUTCOMES = ("soddisfatto", "non_soddisfatto", "non_determinabile")
OBJECTIVE_ACTIONS = {"controllare_verifica": "verifica", "controllare_compito": "compito"}
OBJECTIVES_STATE = ("presenti", "nessuno_nel_perimetro", "non_determinabile")
RELIABILITY = ST.RELIABILITY

# frasi FISSE per "informazione insufficiente": nessuna formulazione libera
INSUFFICIENT = {
    "argomenti_obiettivo": "Non risultano dati sufficienti per stabilire gli argomenti dell'obiettivo: M11 non valuta se il testo li descrive.",
    "materiale_collegato": "Non risultano dati sufficienti per stabilire quali materiali siano collegati all'obiettivo.",
    "materiali_necessari": "Non risultano dati sufficienti per stabilire quali materiali siano necessari.",
    "obiettivo_non_identificabile": "Non risultano dati sufficienti per identificare l'obiettivo (evidenza non risolta o data assente).",
    "azione_non_completa": "L'azione M10 sull'obiettivo non e' completamente eseguibile: parte del contesto manca.",
    "fonte_non_letta": "Una fonte non e' stata letta: non e' possibile stabilire se esistano obiettivi o elementi corrispondenti.",
    "collegamenti_non_esposti": "I collegamenti M5 dell'obiettivo non sono esposti a questo livello: una lista vuota non significa assenza.",
    "lezioni_senza_argomento": "Alcune lezioni non hanno un argomento registrato: l'argomento non viene ricostruito.",
    "obiettivi_non_determinabili": "Non e' possibile stabilire se ci siano obiettivi: almeno una regola M9 non e' stata valutata.",
}
RULES = {
    "obiettivi": "uno per ogni azione M10 controllare_verifica / controllare_compito; data, priorita' M9, eseguibilita' e affidabilita' M10 "
                 "riportate come ricevute",
    "testo_dichiarato": "testo della verifica o del compito cosi' com'e' (dall'elemento risolto da M10); nessuna estrazione di argomenti",
    "collegamenti_m5": "solo quelli esposti da M10, con copertura esposti_da_m9 / non_esposti_da_m9 (lista vuota non esposta != assenza)",
    "preparazione": "azioni M10 aprire_preparazione della stessa voce M9; stato del collegamento mantenuto (mai certo)",
    "lezioni": "lezioni attribuite dell'anno operativo (M8 S4): contesto della materia, MAI legate a un obiettivo",
    "U1": "identificare_obiettivo: soddisfatto se l'azione M10 e' eseguibile o parziale e la data e' presente; altrimenti non_determinabile",
    "U2": "testo_dichiarato_disponibile: soddisfatto se il testo e' non vuoto; non_soddisfatto se l'elemento e' risolto ma il testo e' vuoto; "
          "non_determinabile se l'elemento non e' risolto",
    "U3": "materiale_collegato_identificabile: soddisfatto con almeno una preparazione; non_soddisfatto se i collegamenti sono esposti e "
          "nessuno e' utilizzabile; non_determinabile se esposti solo non_determinabili/conflitti o non esposti",
    "U4": "argomenti_dell_obiettivo: SEMPRE non_determinabile in v1",
    "affidabilita": "obiettivo = azione M10; preparazione = azione aprire_preparazione; uso = minimo delle fonti usate; lezioni = S4, "
                    "bassa per una lezione con discordanza M2; mai sopra la fonte",
}
FORBIDDEN_WORDS = AC.FORBIDDEN_WORDS + ("studia", "studiare", "ripassa", "ripassare", "preparati", "ore", "minuti", "prontezza",
                                        "pronto", "pronta", "sufficiente per studiare", "devi studiare")
NATURA = ("contesto DERIVATO e in SOLA LETTURA per UNA materia: NON e' uno Study Assistant; riporta obiettivi, testi, collegamenti, "
          "lezioni e informazioni mancanti gia' segnalate dalle fonti, senza insegnare, pianificare, assegnare tempi, diagnosticare o giudicare")
RULES_FOR_AGENTS = [
    "Questo e' un contesto informativo: non trasformarlo in un piano, in tempi o in un giudizio sullo studente.",
    "Le lezioni sono contesto della materia: non dire che una lezione riguarda una verifica.",
    "Il testo dichiarato di una verifica e' riportato com'e': non estrarne argomenti; gli argomenti dell'obiettivo non sono determinati.",
    "Un materiale collegato da M5 e' 'probabile' o 'proposto': non dire che serve o che basta.",
    "Con copertura non_esposti_da_m9 una lista vuota non significa assenza.",
    "'dato_sufficiente' vale solo per l'uso formale indicato, mai per lo studio.",
    "Riporta le frasi di 'informazione_insufficiente' quando servono: non completare le informazioni mancanti.",
    "Lo storico e' separato: non usarlo per descrivere la situazione attuale.",
]


def study_context_version():
    return cv.interp_version(STUDY_REV, list(DATA_STATES), list(UNAVAILABLE_REASONS), list(USES), list(OUTCOMES), OBJECTIVE_ACTIONS,
                             list(OBJECTIVES_STATE), INSUFFICIENT, RULES, list(FORBIDDEN_WORDS), AC.action_version())


# ------------------------------------------------------------------------------------------------ helper
def _worst(*levels):
    levels = [x for x in levels if x]
    return max(levels, key=RELIABILITY.index) if levels else "non_determinabile"


def _unavailable(reason, **extra):
    assert reason in UNAVAILABLE_REASONS, reason
    return dict({"stato": "non_disponibile", "motivo": reason}, **extra)


def _use(outcome, aff, motivo, stato=None):
    assert outcome in OUTCOMES
    st = stato or {"soddisfatto": "dato_sufficiente", "non_determinabile": "non_determinabile"}.get(outcome, "non_disponibile")
    u = {"esito": outcome, "stato_dato": st, "affidabilita": aff, "motivo": motivo}
    if st == "non_disponibile":
        u["motivo_non_disponibile"] = "vuoto_valido"
    return u


def _insufficient(out, codice, ambito, uid=None):
    d = {"codice": codice, "ambito": ambito, "messaggio": INSUFFICIENT[codice]}
    if uid:
        d["uid"] = uid
    if d not in out:
        out.append(d)


# ------------------------------------------------------------------------------------------------ obiettivi
def _objective(x, preps, amb, insuff, dettaglio):
    ctx, tipo = x["contesto"], OBJECTIVE_ACTIONS[x["azione"]]
    resolved = bool(ctx) and x["provenienza"]["m7"] is not None
    data = ctx.get("data") if tipo == "verifica" else ctx.get("scadenza")
    text = ctx.get("testo_verifica") if tipo == "verifica" else ctx.get("testo")
    aff = x["affidabilita"]

    if not resolved:
        testo = {"stato": "non_determinabile", "valore": None, "motivo": "elemento M8 non risolto da M10"}
    elif (text or "").strip():
        testo = {"stato": "dato_rilevante", "valore": text, "troncato": bool(ctx.get("testo_troncato"))}
    else:
        testo = _unavailable("non_presente_nella_fonte", valore=None)

    if tipo == "verifica" and resolved:
        cov = ctx.get("collegamenti_m5_copertura")
        links = ctx.get("collegamenti_m5") or []
        if cov == "esposti_da_m9":
            coll = {"copertura": cov, "stato": "dato_rilevante" if links else "non_disponibile", "elenco": links}
            if not links:
                coll["motivo"] = "vuoto_valido"
        else:
            coll = {"copertura": "non_esposti_da_m9", "stato": "non_determinabile", "elenco": links, "nota": ctx.get("nota_copertura")}
        comm = {"copertura": cov, "elenco": ctx.get("comunicazioni_collegate") or []}
    else:
        coll = {"copertura": "non_esposti_da_m9", "stato": "non_determinabile", "elenco": [],
                "nota": "M9/M10 non espongono collegamenti M5 per i compiti" if tipo == "compito" and resolved else "elemento non risolto"}
        comm = {"copertura": "non_esposti_da_m9", "elenco": []}

    prep = [{"stato": "dato_rilevante", "collegamento": p["target"], "relazione": p["contesto"].get("relazione"),
             "stato_collegamento": p["contesto"].get("stato_collegamento"), "certezza": p["contesto"].get("certezza"),
             "materiale": {k: (p["contesto"].get("materiale") or {}).get(k) for k in ("uid", "tipo", "data")},
             "eseguibilita_m10": p["eseguibilita"], "affidabilita": p["affidabilita"], "azione_m10": p["id"]} for p in preps]

    # ---- usi formali ----------------------------------------------------------------------------------------------
    usi = {}
    if resolved and data and x["eseguibilita"] in ("eseguibile", "parzialmente_eseguibile"):
        usi["identificare_obiettivo"] = _use("soddisfatto", aff, "azione M10 %s con data presente" % x["eseguibilita"])
    else:
        usi["identificare_obiettivo"] = _use("non_determinabile", "non_determinabile", "evidenza non risolta o data assente")
        _insufficient(insuff, "obiettivo_non_identificabile", "obiettivo", x["target"])
    if testo["stato"] == "dato_rilevante":
        usi["testo_dichiarato_disponibile"] = _use("soddisfatto", aff, "testo dichiarato presente (riportato com'e')")
    elif testo["stato"] == "non_determinabile":
        usi["testo_dichiarato_disponibile"] = _use("non_determinabile", "non_determinabile", "elemento non risolto")
    else:
        usi["testo_dichiarato_disponibile"] = _use("non_soddisfatto", aff, "elemento risolto ma testo vuoto")
    usable = [l for l in coll["elenco"] if l.get("stato") not in ("non_determinabile", "conflitto")]
    if prep:
        usi["materiale_collegato_identificabile"] = _use("soddisfatto", _worst(aff, *[p["affidabilita"] for p in prep]),
                                                         "%d collegamenti M5 utilizzabili, non confermati" % len(prep))
    elif coll["copertura"] == "esposti_da_m9" and not coll["elenco"]:
        usi["materiale_collegato_identificabile"] = _use("non_soddisfatto", aff, "collegamenti esposti: nessuno")
    else:
        why = "collegamenti non esposti a questo livello" if coll["copertura"] != "esposti_da_m9" else \
            "solo collegamenti non_determinabili o in conflitto" if not usable else "nessuna preparazione M10 per i collegamenti esposti"
        usi["materiale_collegato_identificabile"] = _use("non_determinabile", _worst(aff, "non_determinabile"), why)
    usi["argomenti_dell_obiettivo"] = _use("non_determinabile", "non_determinabile", RULES["U4"])

    _insufficient(insuff, "argomenti_obiettivo", "obiettivo", x["target"])
    if usi["materiale_collegato_identificabile"]["esito"] != "soddisfatto":
        _insufficient(insuff, "materiale_collegato", "obiettivo", x["target"])
    if x["eseguibilita"] != "eseguibile":
        _insufficient(insuff, "azione_non_completa", "obiettivo", x["target"])
    if coll["copertura"] == "non_esposti_da_m9":
        _insufficient(insuff, "collegamenti_non_esposti", "obiettivo", x["target"])

    m7 = x["provenienza"]["m7"]
    o = {"id": x["id"], "tipo": tipo, "uid": x["target"], "hash": m7["hash"] if m7 else None, "data": data,
         "priorita_m9": x["priorita_m9"], "eseguibilita_m10": x["eseguibilita"], "affidabilita": aff,
         "testo_dichiarato": testo, "collegamenti_m5": coll, "preparazione": prep, "comunicazioni_collegate": comm, "usi": usi,
         "lacune_m10": [{"codice": g["codice"], "effetto": g["effetto"]} for g in x["gaps"]],
         "ambiguita": [{"codice": a["codice"], "sezione": a["sezione"]} for a in amb if x["target"] in (a.get("uid") or [])],
         "provenienza": {"azione_m10": x["id"], "voce_m9": x["voce_m9"], "regola_m9": x["regola_m9"], "m8": x["provenienza"]["m8"],
                         "m7": m7}}
    if tipo == "compito" and resolved:
        o["compito"] = {k: ctx[k] for k in ("origine", "stato_operativo", "stato_consegna", "formulazione", "corso") if k in ctx}
    if tipo == "verifica" and resolved:
        o["fonte"] = ctx.get("fonte")
    if dettaglio:
        o["azione_m10_completa"] = x
    return o


# ------------------------------------------------------------------------------------------------ costruzione
def build(reg, materia, now_dt, manual=None, dettaglio=False, soglia_ore=48, *, subjects, source_path=None):
    """cloud.school.study_context/1 per UNA materia (obbligatoria). Sola lettura; `now_dt` letto UNA volta dal chiamante."""
    if not (isinstance(materia, str) and materia.strip()):
        raise ValueError("materia obbligatoria: %r (una delle 8 materie)" % (materia,))
    if not isinstance(dettaglio, bool):
        raise ValueError("dettaglio non valido: %r (true | false)" % (dettaglio,))
    key = SC.resolve_subject(materia.strip(), subjects)
    m10 = AC.build(reg, now_dt, manual=manual, materia=key, dettaglio=True, soglia_ore=soglia_ore, subjects=subjects,
                   source_path=source_path)
    d8 = ST.build(reg, key, now_dt, manual=manual, dettaglio=True, soglia_ore=soglia_ore, subjects=subjects, source_path=source_path)
    mat = m10["materie"][0]
    amb = d8["ambiguita"]
    insuff = []

    # ---- obiettivi (1:1 con le azioni M10) ----------------------------------------------------------------------
    acts = mat["azioni"]
    objectives = []
    for x in acts:
        if x["azione"] not in OBJECTIVE_ACTIONS:
            continue
        preps = [p for p in acts if p["azione"] == "aprire_preparazione" and p["voce_m9"] == x["voce_m9"]] \
            if x["azione"] == "controllare_verifica" else []
        objectives.append(_objective(x, preps, amb, insuff, dettaglio))
    if objectives:
        obj_state = "presenti"
    elif mat["regole_non_valutate_m9"] or mat["priorita_m9"] == "non_determinabile":
        obj_state = "non_determinabile"
        _insufficient(insuff, "obiettivi_non_determinabili", "materia")
    else:
        obj_state = "nessuno_nel_perimetro"
    if mat["regole_non_valutate_m9"]:
        _insufficient(insuff, "fonte_non_letta", "materia")

    # ---- lezioni (contesto della materia, mai legate agli obiettivi) --------------------------------------------
    s4 = next(s for s in d8["segnali"] if s["id"] == "S4")
    cov = d8["copertura"]["lezioni"]
    lessons = []
    for e in sorted(s4.get("evidenze_dettaglio") or [], key=lambda e: (e["data"] or "", e["uid"]), reverse=True):
        disc = bool(e.get("conflitti_m2"))
        arg = (e.get("testo") or "").strip()
        lessons.append({"data": e["data"], "ora": e.get("ora"), "uid": e["uid"], "hash": e["hash"], "stato": "dato_disponibile",
                        "argomento": {"stato": "dato_disponibile", "valore": arg} if arg else _unavailable("non_presente_nella_fonte", valore=None),
                        "affidabilita": _worst(s4["affidabilita"], "bassa" if disc else None),
                        **({"discordanza_m2": True} if disc else {})})
    if cov["stato"] == "non_letto":
        lez = dict(_unavailable("fonte_non_letta"), anno=d8["riferimento_temporale"]["anno_operativo"], n=None, elenco=[])
        _insufficient(insuff, "fonte_non_letta", "materia")
    elif not lessons:
        lez = dict(_unavailable("vuoto_valido"), anno=d8["riferimento_temporale"]["anno_operativo"], n=0, elenco=[])
    else:
        lez = {"stato": "dato_disponibile", "anno": d8["riferimento_temporale"]["anno_operativo"], "n": len(lessons), "elenco": lessons}
    lez.update(senza_argomento=sum(1 for l in lessons if l["argomento"]["stato"] != "dato_disponibile"),
               con_discordanza_m2=sum(1 for l in lessons if l.get("discordanza_m2")), affidabilita=s4["affidabilita"],
               nota="lezioni svolte nella materia: NESSUN legame con gli obiettivi e' dedotto; l'argomento non viene ricostruito")
    if lez["senza_argomento"]:
        _insufficient(insuff, "lezioni_senza_argomento", "materia")

    # ---- comunicazioni, informazioni da verificare, materiali ----------------------------------------------------
    comm = [{"uid": x["target"], "data": x["contesto"].get("data"), "classe_principale": x["contesto"].get("classe_principale"),
             "stato_m6": x["contesto"].get("stato_m6"), "testo": x["contesto"].get("testo"),
             "testo_troncato": bool(x["contesto"].get("testo_troncato")), "caso_speciale": x["contesto"].get("caso_speciale"),
             "priorita_m9": x["priorita_m9"], "eseguibilita_m10": x["eseguibilita"], "affidabilita": x["affidabilita"],
             "azione_m10": x["id"], "stato": "dato_disponibile"} for x in acts if x["azione"] == "leggere_comunicazione"]
    for c_ in comm:
        if c_["caso_speciale"] is None:
            c_.pop("caso_speciale")
    verify = [{"oggetto": x["target"], "informazione_non_determinabile": x["contesto"].get("informazione_non_determinabile"),
               "origine_regola_m9": x["contesto"].get("origine_regola_m9"), "affidabilita": x["affidabilita"],
               "azione_m10": x["id"], "stato": "non_determinabile"} for x in acts if x["azione"] == "verificare_informazione"]
    linked = sorted({(p["materiale"]["uid"], p["collegamento"], p["stato_collegamento"], p["affidabilita"])
                     for o in objectives for p in o["preparazione"] if p["materiale"]["uid"]})
    not_covered = any(g["codice"] == "possibili_non_comunicazioni_non_coperti" for g in d8["gap_m7"])
    materiali = {"collegati": [{"uid": u, "collegamento": l, "stato_collegamento": s, "affidabilita": a, "stato": "dato_rilevante"}
                               for u, l, s, a in linked],
                 "altri_materiali": {"stato": "non_determinabile" if not_covered else "non_disponibile",
                                     **({} if not_covered else {"motivo": "vuoto_valido"}),
                                     "nota": "materiali dei corsi non attribuiti alla materia a questo livello (corsi condivisi: attribuzione "
                                             "per elemento non coperta); school_materials li elenca per CORSO, non per materia: non usati qui"},
                 "nota": "M11 non stabilisce se un materiale serve o basta"}
    if objectives:
        _insufficient(insuff, "materiali_necessari", "materia")

    lacune = []
    for o in objectives:
        for g in o["lacune_m10"]:
            lacune.append({"codice": g["codice"], "ambito": "obiettivo", "uid": o["uid"], "fonte": "M10"})
    for g in d8["gap"]:
        lacune.append({"codice": g["codice"], "ambito": "materia", "fonte": "M8"})
    used = {o["uid"] for o in objectives} | {l["uid"] for l in lessons} | {c_["uid"] for c_ in comm}
    ambig = [{"codice": a["codice"], "sezione": a["sezione"], "uid": sorted(set(a.get("uid") or []) & used)} for a in amb
             if set(a.get("uid") or []) & used]

    affs = [o["affidabilita"] for o in objectives] + [p["affidabilita"] for o in objectives for p in o["preparazione"]] + \
        [l["affidabilita"] for l in lessons] + [c_["affidabilita"] for c_ in comm] + [v["affidabilita"] for v in verify]
    out = {
        "schema": SCHEMA,
        "natura": NATURA,
        "materia": {"chiave": key, "nome_ufficiale": mat["nome_ufficiale"], "docenti": d8["materia"]["docenti"]},
        "riferimento_temporale": {"now": m10["riferimento_temporale"]["now"], "data_riferimento": m10["riferimento_temporale"]["data_riferimento"],
                                  "anno_operativo": m10["riferimento_temporale"]["anno_operativo"],
                                  "nota": "riferimento di M10/M9: nessun calcolo autonomo di giorni o priorita'"},
        "stato_input": {"priorita_m9": mat["priorita_m9"], "affidabilita_m9": mat["affidabilita_m9"],
                        "stato_decisione_m9": mat["stato_decisione_m9"], "regole_non_valutate_m9": mat["regole_non_valutate_m9"],
                        "obiettivi": obj_state},
        "obiettivi": objectives,
        "lezioni": lez,
        "comunicazioni": {"da_leggere": comm, "copertura": "solo le comunicazioni per cui M9/M10 propongono un'azione"},
        "da_verificare": verify,
        "materiali": materiali,
        "lacune_informative": lacune,
        "ambiguita": ambig,
        "informazione_insufficiente": insuff,
        "affidabilita_minima": _worst(*affs) if affs else None,
        "storico": {"anni": d8["storico"]["anni"], "nota": "contesto storico SEPARATO: non usato in obiettivi, preparazioni o lezioni"},
        "politica": {"versione": study_context_version(), "stati_dato": list(DATA_STATES), "motivi_non_disponibile": list(UNAVAILABLE_REASONS),
                     "usi": list(USES), "esiti": list(OUTCOMES), "regole": RULES},
        "provenance": {"schema": SCHEMA, "study_context_version": study_context_version(),
                       "action_version": m10["provenance"]["action_version"], "priority_version": m10["provenance"]["priority_version"],
                       "status_version": m10["provenance"]["status_version"], "context_version": m10["provenance"]["context_version"],
                       "sorgente": source_path, "input": "contratti pubblici M10 (+M9 transitivo) e M8"},
        "regole_agente": RULES_FOR_AGENTS,
    }
    return out


def build_from_path(path, materia, now, manual_path=None, **kw):
    """Registro aperto come COPIA in memoria (Registry.from_snapshot, immutable=1): nessun byte del file viene scritto."""
    reg = R.Registry.from_snapshot(path)
    try:
        return build(reg, materia, now, manual=SC.load_manual(manual_path)["dati"], source_path=os.path.abspath(path), **kw)
    finally:
        reg.close()
