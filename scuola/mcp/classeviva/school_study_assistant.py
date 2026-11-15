"""STUDY ASSISTANT v1 (M12): `cloud.school.study_assistant/1`. Modulo PURO e SOLA LETTURA.

M12 e' un ORCHESTRATORE/GUARDRAIL, non un generatore didattico: dato il contesto M11 di UNA materia stabilisce quali modalita' di
assistenza sono ammesse, con quali contenuti VERIFICATI (copiati verbatim da M11, con uid e, quando M11 lo espone, hash), con quale
affidabilita', con quali limiti e frasi obbligatorie e con quali azioni vietate. Il testo finale lo scrive Claude dentro questi vincoli.

Input (D-M12-2): SOLO il contratto pubblico di M11 (`school_study_context.build`). Nessuna lettura di M1-M10, nessun ricalcolo.
Modalita' v1 (D-M12-4, elenco chiuso): quadro_obiettivo | materiale_collegato | rassegna_lezioni | comunicazioni_da_leggere |
chiarimento_limiti (sempre disponibile). Nessuna spiegazione, esercizio, quiz, riassunto, piano, tempo o valutazione.
Contenuti (D-M12-5): solo elementi M11 dato_disponibile / dato_rilevante / dato_sufficiente; il non determinabile sta nei limiti;
lezioni e obiettivi mai nella stessa modalita'; nessuno storico. D-M12-7 (opzione A): materiale = {uid, tipo, data, stato,
affidabilita'} con il limite esplicito "titolo non disponibile a questo livello".
Affidabilita' di una modalita' = minimo delle fonti M11 usate (mai sopra M11). Stesso input -> stesso JSON; nessun orologio/scrittura.
"""
from __future__ import annotations

import os

import cvv_client as cv
import registry as R
import school_context as SC
import school_status as ST
import school_study_context as SX

SCHEMA = "cloud.school.study_assistant/1"
ASSISTANT_REV = 1

MODES = ("quadro_obiettivo", "materiale_collegato", "rassegna_lezioni", "comunicazioni_da_leggere", "chiarimento_limiti")
AVAILABILITY = ("disponibile", "limitata", "non_disponibile")
REQUEST_OUTCOMES = ("ammessa", "non_disponibile", "obiettivo_non_trovato", "nessuna_modalita_richiesta")
ADMITTED_STATES = ("dato_disponibile", "dato_rilevante", "dato_sufficiente")
RELIABILITY = ST.RELIABILITY

ALLOWED = ("riportare_fatto", "citare_fonte", "riformulare_senza_aggiungere", "elencare", "indicare_limite")
FORBIDDEN_ACTIONS = ("spiegare_argomento", "ricavare_argomenti", "collegare_lezione_obiettivo", "stimare_tempo", "proporre_piano",
                     "valutare_preparazione", "dichiarare_materiale_necessario", "risolvere_incertezza", "inventare_contenuto",
                     "usare_storico", "creare_esercizi_o_quiz", "riassumere_argomenti_non_presenti")
MODE_ALLOWED = {
    "quadro_obiettivo": ("riportare_fatto", "citare_fonte", "riformulare_senza_aggiungere", "indicare_limite"),
    "materiale_collegato": ("riportare_fatto", "citare_fonte", "indicare_limite"),
    "rassegna_lezioni": ("elencare", "citare_fonte", "indicare_limite"),
    "comunicazioni_da_leggere": ("riportare_fatto", "citare_fonte", "riformulare_senza_aggiungere", "indicare_limite"),
    "chiarimento_limiti": ("indicare_limite", "citare_fonte", "elencare"),
}
PHRASES = {
    "argomenti_non_determinati": "Gli argomenti dell'obiettivo non sono determinati dalle fonti.",
    "affidabilita_non_alta": "L'affidabilita' di almeno un'informazione non e' alta: va presentata come da verificare.",
    "collegamento_non_confermato": "Il collegamento al materiale e' probabile o proposto, non confermato.",
    "titolo_non_disponibile": "Il titolo del materiale non e' disponibile a questo livello.",
    "lezioni_non_collegate": "Le lezioni sono contesto della materia: non sono collegate agli obiettivi.",
    "lezioni_senza_argomento": "Alcune lezioni non hanno un argomento registrato: l'argomento non viene ricostruito.",
    "lezioni_discordanza": "Per alcune lezioni l'attribuzione della materia e' in discordanza (M2).",
    "comunicazione_troncata": "Il testo di almeno una comunicazione e' troncato.",
    "limiti_non_completare": "Le informazioni non determinabili non vanno completate ne' risolte.",
    "hash_non_esposto": "Per questo elemento M11 espone l'uid ma non l'hash: la verifica passa per la provenienza indicata.",
}
INSUFFICIENT_M12 = {
    "modalita_non_disponibile": "La modalita' richiesta non e' disponibile con i dati attuali.",
    "obiettivo_non_trovato": "L'obiettivo indicato non e' tra gli obiettivi del contesto M11 della materia.",
    "nessun_obiettivo_nel_perimetro": "Non risultano obiettivi nel perimetro operativo della materia.",
    "obiettivi_non_determinabili": "Non e' possibile stabilire se ci siano obiettivi: almeno una fonte non e' stata valutata.",
    "materiale_senza_titolo": "Il materiale collegato e' identificato solo da uid, tipo e data: il titolo non e' disponibile a questo livello.",
}
REASONS = {
    "nessun_obiettivo": "nessun obiettivo selezionato nel contesto M11",
    "u1_non_soddisfatto": "nessun obiettivo identificabile (U1 non soddisfatto)",
    "u3_non_soddisfatto": "nessun materiale collegato identificabile (U3 non soddisfatto o non determinabile)",
    "lezioni_non_disponibili": "lezioni non disponibili (fonte non letta o vuota)",
    "nessun_argomento": "nessuna lezione con argomento registrato",
    "nessuna_comunicazione": "nessuna comunicazione da leggere",
    "obiettivo_non_trovato": "obiettivo non trovato tra quelli M11",
}
FORBIDDEN_WORDS = SX.FORBIDDEN_WORDS + ("sei pronto", "sei pronta", "sei indietro", "devi studiare")
NATURA = ("ORCHESTRATORE/GUARDRAIL in sola lettura: stabilisce modalita' ammesse, contenuti verificati, affidabilita', limiti e azioni "
          "vietate; nessun testo didattico generato")
RULES_FOR_AGENTS = [
    "Usa solo le modalita' con disponibilita' 'disponibile' o 'limitata' e solo i loro contenuti.",
    "Rispetta azioni_ammesse e azioni_vietate della modalita': niente spiegazioni di argomenti, esercizi, quiz, piani, tempi o valutazioni.",
    "Riporta sempre le frasi_obbligatorie della modalita' che usi.",
    "Una modalita' non disponibile non ha contenuti: non sostituirla con contenuti inventati; proponi solo le alternative indicate.",
    "Non collegare lezioni e obiettivi; non ricavare argomenti da date, titoli o testi.",
    "Cita uid (e hash quando presente) degli elementi che riporti.",
]


def study_assistant_version():
    return cv.interp_version(ASSISTANT_REV, list(MODES), list(AVAILABILITY), list(REQUEST_OUTCOMES), list(ADMITTED_STATES), list(ALLOWED),
                             list(FORBIDDEN_ACTIONS), MODE_ALLOWED, PHRASES, INSUFFICIENT_M12, REASONS, list(FORBIDDEN_WORDS),
                             SX.study_context_version())


# ------------------------------------------------------------------------------------------------ helper
def _worst(*levels):
    levels = [x for x in levels if x]
    return max(levels, key=RELIABILITY.index) if levels else None


def _mode(name, availability, motivo=None, contenuti=None, requisiti=None, limiti=None, frasi=None, affidabilita=None):
    assert name in MODES and availability in AVAILABILITY
    if availability == "non_disponibile":
        contenuti = []
    return {"modalita": name, "disponibilita": availability, "motivo": motivo, "requisiti": requisiti or [],
            "affidabilita": affidabilita if availability != "non_disponibile" else None, "contenuti": contenuti or [],
            "limiti": limiti or [], "azioni_ammesse": list(MODE_ALLOWED[name]), "azioni_vietate": list(FORBIDDEN_ACTIONS),
            "frasi_obbligatorie": [{"codice": c, "frase": PHRASES[c]} for c in (frasi or [])]}


def _limits_for(insuff, uid=None, codes=None):
    return [dict(i) for i in insuff if (uid is None or i.get("uid") == uid) and (codes is None or i["codice"] in codes)]


# ------------------------------------------------------------------------------------------------ modalita'
def _quadro(objs, insuff, not_found, dettaglio):
    if not_found:
        return _mode("quadro_obiettivo", "non_disponibile", REASONS["obiettivo_non_trovato"])
    if not objs:
        return _mode("quadro_obiettivo", "non_disponibile", REASONS["nessun_obiettivo"])
    content, req, lim, partial = [], [], [], False
    for o in objs:
        u1 = o["usi"]["identificare_obiettivo"]
        req.append({"fonte": "M11.obiettivi[%s].usi.identificare_obiettivo" % o["uid"], "esito": u1["esito"], "affidabilita": u1["affidabilita"]})
        lim += _limits_for(insuff, o["uid"])
        if u1["esito"] != "soddisfatto":
            partial = True
            continue
        td = o["testo_dichiarato"]
        c = {"tipo": "obiettivo", "uid": o["uid"], "hash": o["hash"], "stato_dato": u1["stato_dato"],
             "campi": {"tipo_obiettivo": o["tipo"], "data": o["data"], "priorita_m9": o["priorita_m9"],
                       "eseguibilita_m10": o["eseguibilita_m10"],
                       "testo_dichiarato": td.get("valore") if td["stato"] in ADMITTED_STATES else None,
                       "testo_troncato": bool(td.get("troncato"))},
             "affidabilita": _worst(o["affidabilita"], u1["affidabilita"])}
        if o.get("fonte"):
            c["campi"]["fonte"] = o["fonte"]
        if o.get("compito"):
            c["campi"]["compito"] = o["compito"]
        if o["eseguibilita_m10"] != "eseguibile" or td.get("troncato") or td["stato"] not in ADMITTED_STATES:
            partial = True
        if dettaglio:
            c["elemento_m11"] = o
        content.append(c)
    if not content:
        return _mode("quadro_obiettivo", "non_disponibile", REASONS["u1_non_soddisfatto"], requisiti=req, limiti=lim)
    aff = _worst(*[c["affidabilita"] for c in content])
    frasi = ["argomenti_non_determinati"] + (["affidabilita_non_alta"] if aff != "alta" else [])
    return _mode("quadro_obiettivo", "limitata" if partial else "disponibile", None, content, req, lim, frasi, aff)


def _materiale(objs, insuff, not_found, dettaglio):
    if not_found:
        return _mode("materiale_collegato", "non_disponibile", REASONS["obiettivo_non_trovato"])
    if not objs:
        return _mode("materiale_collegato", "non_disponibile", REASONS["nessun_obiettivo"])
    content, req, lim = [], [], []
    for o in objs:
        u3 = o["usi"]["materiale_collegato_identificabile"]
        req.append({"fonte": "M11.obiettivi[%s].usi.materiale_collegato_identificabile" % o["uid"], "esito": u3["esito"],
                    "affidabilita": u3["affidabilita"]})
        lim += _limits_for(insuff, o["uid"], ("materiale_collegato", "collegamenti_non_esposti"))
        if u3["esito"] != "soddisfatto":
            continue
        for p in o["preparazione"]:
            if p["stato"] not in ADMITTED_STATES:
                continue
            c = {"tipo": "materiale", "uid": p["materiale"]["uid"], "hash": None, "hash_esposto_da_m11": False,
                 "stato_dato": p["stato"],
                 "campi": {"tipo_materiale": p["materiale"]["tipo"], "data": p["materiale"]["data"],
                           "stato_collegamento": p["stato_collegamento"], "relazione": p["relazione"], "titolo": None},
                 "affidabilita": _worst(p["affidabilita"], u3["affidabilita"]),
                 "provenienza": {"obiettivo": o["uid"], "obiettivo_hash": o["hash"], "collegamento": p["collegamento"],
                                 "azione_m10": p["azione_m10"]}}
            if dettaglio:
                c["elemento_m11"] = p
            content.append(c)
    if not content:
        return _mode("materiale_collegato", "non_disponibile", REASONS["u3_non_soddisfatto"], requisiti=req, limiti=lim)
    lim.append({"codice": "materiale_senza_titolo", "ambito": "materiale", "messaggio": INSUFFICIENT_M12["materiale_senza_titolo"]})
    aff = _worst(*[c["affidabilita"] for c in content])
    return _mode("materiale_collegato", "limitata", None, content, req, lim,
                 ["collegamento_non_confermato", "titolo_non_disponibile", "hash_non_esposto"] + (["affidabilita_non_alta"] if aff != "alta" else []),
                 aff)


def _lezioni(lez, insuff, dettaglio):
    req = [{"fonte": "M11.lezioni", "esito": lez["stato"], "affidabilita": lez.get("affidabilita")}]
    if lez["stato"] != "dato_disponibile":
        return _mode("rassegna_lezioni", "non_disponibile", "%s (%s)" % (REASONS["lezioni_non_disponibili"], lez.get("motivo")),
                     requisiti=req, limiti=_limits_for(insuff, codes=("fonte_non_letta",)))
    with_arg = [l for l in lez["elenco"] if l["argomento"]["stato"] in ADMITTED_STATES]
    if not with_arg:
        return _mode("rassegna_lezioni", "non_disponibile", REASONS["nessun_argomento"], requisiti=req,
                     limiti=_limits_for(insuff, codes=("lezioni_senza_argomento",)))
    content = []
    for l in with_arg:
        c = {"tipo": "lezione", "uid": l["uid"], "hash": l["hash"], "stato_dato": l["stato"],
             "campi": {"data": l["data"], "ora": l.get("ora"), "argomento": l["argomento"]["valore"]}, "affidabilita": l["affidabilita"]}
        if l.get("discordanza_m2"):
            c["campi"]["discordanza_m2"] = True
        if dettaglio:
            c["elemento_m11"] = l
        content.append(c)
    without = lez["n"] - len(with_arg)
    lim = _limits_for(insuff, codes=("lezioni_senza_argomento",))
    if without:
        lim.append({"codice": "lezioni_senza_argomento_n", "ambito": "materia", "n": without})
    frasi = ["lezioni_non_collegate"] + (["lezioni_senza_argomento"] if without else []) + \
        (["lezioni_discordanza"] if any(c["campi"].get("discordanza_m2") for c in content) else [])
    aff = _worst(*[c["affidabilita"] for c in content])
    if aff != "alta":
        frasi.append("affidabilita_non_alta")
    return _mode("rassegna_lezioni", "limitata" if without else "disponibile", None, content, req, lim, frasi, aff)


def _comunicazioni(comm, dettaglio):
    items = [c for c in comm["da_leggere"] if c["stato"] in ADMITTED_STATES]
    req = [{"fonte": "M11.comunicazioni.da_leggere", "esito": "n=%d" % len(items)}]
    if not items:
        return _mode("comunicazioni_da_leggere", "non_disponibile", REASONS["nessuna_comunicazione"], requisiti=req)
    content = []
    for c in items:
        x = {"tipo": "comunicazione", "uid": c["uid"], "hash": None, "hash_esposto_da_m11": False, "stato_dato": c["stato"],
             "campi": {"data": c["data"], "testo": c["testo"], "testo_troncato": c["testo_troncato"],
                       "classe_principale": c["classe_principale"], "stato_m6": c["stato_m6"]},
             "affidabilita": c["affidabilita"], "provenienza": {"azione_m10": c["azione_m10"]}}
        if c.get("caso_speciale"):
            x["campi"]["caso_speciale"] = c["caso_speciale"]
        if dettaglio:
            x["elemento_m11"] = c
        content.append(x)
    trunc = any(c["campi"]["testo_troncato"] for c in content)
    aff = _worst(*[c["affidabilita"] for c in content])
    frasi = (["comunicazione_troncata"] if trunc else []) + ["hash_non_esposto"] + (["affidabilita_non_alta"] if aff != "alta" else [])
    return _mode("comunicazioni_da_leggere", "limitata" if trunc else "disponibile", None, content, req, [], frasi, aff)


def _chiarimento(m11, objs, insuff):
    lim = [dict(i) for i in insuff]
    for v in m11["da_verificare"]:
        lim.append({"codice": "da_verificare", "ambito": "oggetto", "uid": v["oggetto"], "informazione": v["informazione_non_determinabile"],
                    "affidabilita": v["affidabilita"]})
    for o in objs:
        for u, val in o["usi"].items():
            if val["esito"] != "soddisfatto":
                lim.append({"codice": "uso_%s" % val["esito"], "ambito": "obiettivo", "uid": o["uid"], "uso": u, "motivo": val["motivo"]})
    req = [{"fonte": "M11 (limiti, da_verificare, informazione_insufficiente)", "esito": "sempre"}]
    return _mode("chiarimento_limiti", "disponibile", None, [], req, lim, ["limiti_non_completare"], None)


# ------------------------------------------------------------------------------------------------ costruzione
def build(reg, materia, now_dt, manual=None, obiettivo=None, modalita=None, dettaglio=False, soglia_ore=48, *, subjects, source_path=None):
    """cloud.school.study_assistant/1 per UNA materia. Sola lettura; `now_dt` letto UNA volta dal chiamante."""
    if not (isinstance(materia, str) and materia.strip()):
        raise ValueError("materia obbligatoria: %r (una delle 8 materie)" % (materia,))
    if modalita is not None and modalita not in MODES:
        raise ValueError("modalita sconosciuta: %r (ammesse: %s)" % (modalita, ", ".join(MODES)))
    if obiettivo is not None and not (isinstance(obiettivo, str) and obiettivo.strip()):
        raise ValueError("obiettivo non valido: %r (uid di un obiettivo M11)" % (obiettivo,))
    if not isinstance(dettaglio, bool):
        raise ValueError("dettaglio non valido: %r (true | false)" % (dettaglio,))
    m11 = SX.build(reg, materia, now_dt, manual=manual, dettaglio=False, soglia_ore=soglia_ore, subjects=subjects, source_path=source_path)
    insuff = [dict(i) for i in m11["informazione_insufficiente"]]
    all_objs = m11["obiettivi"]
    not_found = False
    if obiettivo is not None:
        objs = [o for o in all_objs if o["uid"] == obiettivo.strip()]           # solo corrispondenza esatta, nessuna ricerca approssimata
        not_found = not objs
    else:
        objs = all_objs
    extra_insuff = []
    if not_found:
        extra_insuff.append({"codice": "obiettivo_non_trovato", "ambito": "richiesta", "uid": obiettivo.strip(),
                             "messaggio": INSUFFICIENT_M12["obiettivo_non_trovato"]})
    elif not all_objs:
        code = "obiettivi_non_determinabili" if m11["stato_input"]["obiettivi"] == "non_determinabile" else "nessun_obiettivo_nel_perimetro"
        extra_insuff.append({"codice": code, "ambito": "materia", "messaggio": INSUFFICIENT_M12[code]})

    modes = {"quadro_obiettivo": _quadro(objs, insuff, not_found, dettaglio),
             "materiale_collegato": _materiale(objs, insuff, not_found, dettaglio),
             "rassegna_lezioni": _lezioni(m11["lezioni"], insuff, dettaglio),
             "comunicazioni_da_leggere": _comunicazioni(m11["comunicazioni"], dettaglio),
             "chiarimento_limiti": _chiarimento(m11, objs, insuff + extra_insuff)}
    if modes["materiale_collegato"]["disponibilita"] != "non_disponibile":
        extra_insuff.append({"codice": "materiale_senza_titolo", "ambito": "materiale", "messaggio": INSUFFICIENT_M12["materiale_senza_titolo"]})
    available = [m for m in MODES if modes[m]["disponibilita"] != "non_disponibile"]

    if not_found:
        esito = {"stato": "obiettivo_non_trovato", "motivo": REASONS["obiettivo_non_trovato"],
                 "obiettivi_esistenti": [o["uid"] for o in all_objs], "alternative": [m for m in available if m != modalita]}
    elif modalita is None:
        esito = {"stato": "nessuna_modalita_richiesta", "motivo": "disponibilita' delle 5 modalita' (nessuna scelta)", "alternative": []}
    elif modes[modalita]["disponibilita"] == "non_disponibile":
        esito = {"stato": "non_disponibile", "motivo": modes[modalita]["motivo"], "alternative": [m for m in available if m != modalita]}
        extra_insuff.append({"codice": "modalita_non_disponibile", "ambito": "richiesta", "modalita": modalita,
                             "messaggio": INSUFFICIENT_M12["modalita_non_disponibile"]})
    else:
        esito = {"stato": "ammessa", "motivo": None, "alternative": []}
    shown = [modes[modalita]] if modalita is not None else [modes[m] for m in MODES]

    nondet = [{"fonte": "M11.da_verificare", "uid": v["oggetto"], "informazione": v["informazione_non_determinabile"]}
              for v in m11["da_verificare"]]
    for o in objs:
        for u, val in o["usi"].items():
            if val["esito"] == "non_determinabile":
                nondet.append({"fonte": "M11.obiettivi.usi", "uid": o["uid"], "uso": u, "motivo": val["motivo"]})
    return {
        "schema": SCHEMA,
        "natura": NATURA,
        "richiesta": {"materia": m11["materia"]["chiave"], "obiettivo": obiettivo.strip() if obiettivo else None, "modalita": modalita,
                      "dettaglio": dettaglio},
        "esito_richiesta": esito,
        "riferimento_temporale": m11["riferimento_temporale"],
        "stato_input_m11": m11["stato_input"],
        "modalita": shown,
        "disponibilita": {m: modes[m]["disponibilita"] for m in MODES},
        "non_determinabile": nondet,
        "informazione_insufficiente": insuff + extra_insuff,
        "politica": {"versione": study_assistant_version(), "modalita": list(MODES), "disponibilita": list(AVAILABILITY),
                     "azioni_ammesse": list(ALLOWED), "azioni_vietate": list(FORBIDDEN_ACTIONS), "stati_ammessi": list(ADMITTED_STATES),
                     "nota": "modalita' didattiche (spiegazioni, esercizi, quiz, riassunti, piani, valutazioni) fuori da v1"},
        "provenance": {"schema": SCHEMA, "study_assistant_version": study_assistant_version(),
                       "study_context_version": m11["provenance"]["study_context_version"],
                       "action_version": m11["provenance"]["action_version"], "priority_version": m11["provenance"]["priority_version"],
                       "status_version": m11["provenance"]["status_version"], "context_version": m11["provenance"]["context_version"],
                       "sorgente": source_path, "input": "contratto pubblico M11"},
        "regole_agente": RULES_FOR_AGENTS,
    }


def build_from_path(path, materia, now, manual_path=None, **kw):
    """Registro aperto come COPIA in memoria (Registry.from_snapshot, immutable=1): nessun byte del file viene scritto."""
    reg = R.Registry.from_snapshot(path)
    try:
        return build(reg, materia, now, manual=SC.load_manual(manual_path)["dati"], source_path=os.path.abspath(path), **kw)
    finally:
        reg.close()
