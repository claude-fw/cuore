"""DECISION LAYER PRUDENTE (M9 v1): `cloud.school.priority/1`. Modulo PURO e SOLA LETTURA.

Consuma SOLO il contratto pubblico di M8 (`school_status.build(..., dettaglio=True)`): nessuna nuova fonte, nessun ricalcolo dei
segnali, nessuna nuova attribuzione. Trasforma lo stato oggettivo in VOCI di priorita' con azione minima, motivo, evidenze M8
(uid + hash copiati) e affidabilita' propagata. Principio: AFFIDABILE PRIMA DI INTELLIGENTE.

Separazione obbligatoria:
  * `priorita`    = quanto la situazione meriterebbe attenzione SE il dato e' corretto (dipende solo dai valori dei segnali);
  * `affidabilita`= quanto il sistema e' sicuro del dato (propagata da M8, monotona, mai rialzata).
Decisioni (2026-10-06): D-M9-1 soglie P1 (<=1 alta, 2-7 attenzione, >7 informativa) e P3a al massimo attenzione; D-M9-2 quinto stato
`non_determinabile` (una fonte non letta non diventa mai `nessuna`); D-M9-3 P2 non alza mai la priorita' (solo azione di
preparazione sulla voce P1) e P5 e' solo contesto. Nessun piano di studio, durata, diagnosi, voto, giudizio o peso numerico.
Stesso Registry + now + manuale + materia + dettaglio + soglia -> stesso JSON; nessun orologio, nessuna scrittura.
"""
from __future__ import annotations

import datetime as dt
import os

import cvv_client as cv
import registry as R
import school_common as CM
import school_context as SC
import school_status as ST

SCHEMA = "cloud.school.priority/1"
PRIORITY_REV = 1

PRIORITY_LEVELS = ("nessuna", "informativa", "attenzione", "alta", "non_determinabile")
_RANK = {"nessuna": 0, "informativa": 1, "attenzione": 2, "alta": 3}         # non_determinabile NON e' nella scala: e' assenza di decisione
ORDER_SUBJECTS = ("alta", "attenzione", "informativa", "non_determinabile", "nessuna")
ACTIONS = ("controllare_verifica", "aprire_preparazione", "controllare_compito", "leggere_comunicazione", "verificare_informazione")
RELIABILITY = ST.RELIABILITY
RULE_ORDER = ("P1", "P1b", "P3a", "P3b", "P4")                                 # ordine fisso per i pareggi sullo stesso oggetto
INPUT_SIGNALS = {"P1": "S1", "P1b": "S1", "P2": "S6", "P3a": "S2", "P3b": "S3", "P4": "S7"}

# Politica VERSIONATA (D-M9-1): non derivabile dai dati, approvata dall'utente.
POLICY = {
    "P1_soglie_giorni": {"alta_max": 1, "attenzione_max": 7},          # <=1 alta, 2..7 attenzione, >7 informativa
    "P1b_massimo": "informativa",
    "P3a_massimo": "attenzione",
    "P3b_massimo": "attenzione",
    "P4_massimo": "informativa",
    "P4_recente_giorni": ST.RECENT_DAYS,                               # finestra gia' di M7/M8 (14 giorni)
    "P4_classi": ["compito", "organizzativa", "verifica"],
    "P4_stati_classe": ["determinato", "probabile"],
    "P2_stati_collegamento_azione": ["probabile", "proposto"],
    "P2_stati_collegamento_verifica": ["conflitto"],
    "P6_soglia_priorita": "attenzione", "P6_affidabilita": ["bassa", "non_determinabile"],
}
RULES = {
    "P1": "verifica futura attribuita (S1 presente): una voce per verifica; priorita' dalle soglie su giorni_mancanti; azione controllare_verifica",
    "P1b": "S1 'assente_con_indizi_non_datati': indizi di verifica senza data -> al massimo informativa; azione verificare_informazione",
    "P2": "collegamento M5 probabile|proposto a una verifica futura (S6): azione aprire_preparazione sulla voce P1, MAI aumento di priorita'; "
          "collegamento in conflitto -> verificare_informazione; non_determinabile -> nessuna azione",
    "P3a": "compito aperto con scadenza futura (S2): <=7 giorni attenzione, oltre informativa (mai alta); azione controllare_compito",
    "P3b": "compito Classroom scaduto non consegnato entro 14 giorni (S3): attenzione; non consegnato SECONDO CLASSROOM, che non dice se "
           "la consegna era richiesta; azione controllare_compito",
    "P4": "comunicazione della materia degli ultimi 14 giorni (S7) con classe principale compito|verifica|organizzativa in stato "
          "determinato|probabile: informativa; azione leggere_comunicazione; avvisi/materiali/proposte -> nessuna voce",
    "P5": "continuita' e argomenti (S4/S5), attribuzione Classroom (S8), dati valutativi (S9): SOLO contesto, nessuna voce",
    "P6": "voce con priorita' >= attenzione e affidabilita' bassa|non_determinabile -> azione aggiuntiva verificare_informazione; "
          "la priorita' non cambia",
}
RELIABILITY_RULES = {
    "R1": "affidabilita' di una voce = minimo tra le affidabilita' M8 dei segnali che la generano (mai rialzata)",
    "R2": "un segnale di input con valore null (fonte mai letta, M8 A6) -> regola in regole_non_valutate e tetto non_determinabile per la materia",
    "R3": "affidabilita' della materia = minimo tra le voci di priorita' massima e il tetto R2; senza voci: minimo tra gli input valutati e R2",
    "R4": "P2 e P6 non cambiano l'affidabilita' della voce: l'azione di preparazione ha la sua (minimo tra S1 e S6)",
    "R5": "legacy non ha effetto (D1)",
    "R6": "monotonia: per voce sulla coppia (oggetto, regola); per materia tramite R2/R3. Una voce di regola diversa sullo stesso oggetto "
          "(es. P4 quando P1b non e' valutabile) e' un'altra affermazione, non un rialzo",
}
FORBIDDEN_WORDS = ST.FORBIDDEN_WORDS + ("indietro", "recuperare", "recupera", "migliorare", "ore di studio", "studia", "lacuna",
                                        "lacune", "ritardo nello studio")

NATURA = ("decision layer PRUDENTE e in SOLA LETTURA sopra M8: priorita' operative con azione minima, motivo, evidenze e affidabilita'; "
          "nessun giudizio, piano di studio, durata, diagnosi o voto")
RULES_FOR_AGENTS = [
    "La priorita' dice quanto una situazione meriterebbe attenzione SE il dato e' corretto; l'affidabilita' dice quanto il dato e' sicuro: "
    "riportale sempre insieme.",
    "Con stato_decisione 'da_verificare' o affidabilita' bassa presenta la voce come possibile situazione da verificare, non come fatto.",
    "'non_determinabile' significa che una fonte non e' stata letta: non dire che non c'e' nulla da fare.",
    "Proponi solo le azioni elencate (controllare, aprire, leggere, verificare): nessun piano di studio, durata o giudizio.",
    "Il 'contesto' (lezioni, attribuzione Classroom, voti) non e' una priorita' e non e' una valutazione.",
    "Lo storico (anni diversi dall'operativo) non genera priorita'.",
    "Cita le evidenze (segnale M8 + uid + hash) di ogni voce.",
]


def priority_version():
    """'r<rev>-<impronta>': cambia con politica, regole, azioni, livelli o con la versione di M8 (e quindi di M7)."""
    return cv.interp_version(PRIORITY_REV, POLICY, RULES, RELIABILITY_RULES, list(PRIORITY_LEVELS), list(ACTIONS), list(RULE_ORDER),
                             list(FORBIDDEN_WORDS), ST.status_version())


# ------------------------------------------------------------------------------------------------ helper
def _worst(*levels):
    return max(levels, key=RELIABILITY.index) if levels else "alta"


def _cap(level, maximum):
    return level if _RANK[level] <= _RANK[maximum] else maximum


def p1_level(giorni):
    """Soglie D-M9-1 su giorni_mancanti (>= 0)."""
    s = POLICY["P1_soglie_giorni"]
    if giorni <= s["alta_max"]:
        return "alta"
    if giorni <= s["attenzione_max"]:
        return "attenzione"
    return "informativa"


def _when(g):
    return "oggi" if g == 0 else ("tra 1 giorno" if g == 1 else "tra %d giorni" % g)


def _days(day, ref):
    return (dt.date.fromisoformat(day) - dt.date.fromisoformat(ref)).days


def _sig(doc, sid):
    return next(s for s in doc["segnali"] if s["id"] == sid)


def _evidence(sig, uids):
    """Evidenze M8 COPIATE (segnale + uid + hash): nessun nuovo hash, nessun ricalcolo."""
    by = {e["uid"]: e for e in sig["evidenze"] if "uid" in e}
    return [{"segnale": sig["id"], "uid": u, "hash": by[u]["hash"]} for u in uids if u in by]


def _voce(regola, priorita, aff, motivo, oggetto, data, segnali, evidenze, azioni, note=None):
    v = {"id": CM.short_hash(PRIORITY_REV, regola, oggetto, *[e["hash"] for e in evidenze]), "regola": regola, "priorita": priorita,
         "affidabilita": aff, "oggetto": oggetto, "data": data, "motivo": motivo, "azioni": azioni, "segnali": segnali,
         "evidenze": evidenze}
    if note:
        v["note"] = note
    return v


def _azione(nome, uid, aff, motivo=None, evidenze=None):
    assert nome in ACTIONS, nome
    a = {"azione": nome, "uid": uid, "affidabilita": aff}
    if motivo:
        a["motivo"] = motivo
    if evidenze:
        a["evidenze"] = evidenze
    return a


# ------------------------------------------------------------------------------------------------ una materia
def subject_priority(doc, dettaglio=False):
    """Voci, priorita', affidabilita' e contesto di UNA materia da un documento M8 (dettaglio=True). Puro: nessun accesso al registro."""
    td = doc["riferimento_temporale"]["data_riferimento"]
    s1, s2, s3, s6, s7 = (_sig(doc, k) for k in ("S1", "S2", "S3", "S6", "S7"))
    voci, non_valutate = [], []

    def missing(regola, sig):
        if sig["valore"] is None:
            non_valutate.append({"regola": regola, "segnale": sig["id"], "affidabilita": sig["affidabilita"],
                                 "motivo": "; ".join(sig["motivi_affidabilita"]) or "valore non disponibile"})
            return True
        return False

    # ---- P1 / P1b / P2 ------------------------------------------------------------------------------------------
    p1_by_uid = {}
    if not missing("P1", s1):
        if s1["valore"]["stato"] == "presente":
            for e in sorted(s1["evidenze_dettaglio"], key=lambda e: (e["data"], e["uid"])):
                g = _days(e["data"], td)
                pr = p1_level(g)
                note = ["oggi: l'ora non e' disponibile, la verifica potrebbe essere gia' avvenuta"] if g == 0 else None
                v = _voce("P1", pr, s1["affidabilita"], "verifica %s %s (%s)" % (
                    "attribuita" if e.get("origine") == "agenda_classeviva" else "annunciata (M6)", _when(g), e["data"]),
                    e["uid"], e["data"], ["S1"], _evidence(s1, [e["uid"]]),
                    [_azione("controllare_verifica", e["uid"], s1["affidabilita"])], note)
                p1_by_uid[e["uid"]] = v
                voci.append(v)
        elif s1["valore"]["stato"] == "assente_con_indizi_non_datati":
            for e in sorted(s1["evidenze_dettaglio"], key=lambda e: e["uid"]):
                voci.append(_voce("P1b", POLICY["P1b_massimo"], s1["affidabilita"],
                                  "comunicazione con indizi di verifica ma senza data utilizzabile (%s)" % e["data"], e["uid"], e["data"],
                                  ["S1"], _evidence(s1, [e["uid"]]),
                                  [_azione("verificare_informazione", e["uid"], s1["affidabilita"])]))
    if not missing("P2", s6) and isinstance(s6["valore"], list):
        for item in s6["valore"]:
            v = p1_by_uid.get(item["verifica"])
            if v is None:
                continue
            for link in item["collegamenti"]:
                ev = _evidence(s6, [link["uid"]])
                aff = _worst(s1["affidabilita"], s6["affidabilita"])
                if link["stato"] in POLICY["P2_stati_collegamento_azione"]:
                    v["azioni"].append(_azione("aprire_preparazione", link["uid"], aff,
                                               "collegamento M5 '%s' (%s): non e' un fatto" % (link["relazione"], link["stato"]), ev))
                elif link["stato"] in POLICY["P2_stati_collegamento_verifica"]:
                    v["azioni"].append(_azione("verificare_informazione", link["uid"], aff,
                                               "conflitto M5 sul collegamento: nessuna scelta", ev))
                else:
                    continue                    # non_determinabile: nessuna azione, quindi il collegamento non entra nella voce
                if ev:
                    v["segnali"] = sorted(set(v["segnali"]) | {"S6"})
                    v["evidenze"] = v["evidenze"] + ev

    # ---- P3a / P3b ------------------------------------------------------------------------------------------------
    if not missing("P3a", s2):
        for e in sorted(s2["evidenze_dettaglio"], key=lambda e: (e["data"] or "", e["uid"])):
            g = _days(e["data"], td)
            pr = _cap(p1_level(g), POLICY["P3a_massimo"])
            voci.append(_voce("P3a", pr, s2["affidabilita"], "compito con scadenza %s (%s)" % (_when(g), e["data"]), e["uid"], e["data"],
                              ["S2"], _evidence(s2, [e["uid"]]), [_azione("controllare_compito", e["uid"], s2["affidabilita"])]))
    if not missing("P3b", s3):
        for e in sorted(s3["evidenze_dettaglio"], key=lambda e: (e["data"] or "", e["uid"])):
            voci.append(_voce("P3b", POLICY["P3b_massimo"], s3["affidabilita"],
                              "compito scaduto il %s e non consegnato secondo Classroom" % e["data"], e["uid"], e["data"], ["S3"],
                              _evidence(s3, [e["uid"]]), [_azione("controllare_compito", e["uid"], s3["affidabilita"])],
                              ["Classroom non dice se la consegna era richiesta"]))

    # ---- P4 -----------------------------------------------------------------------------------------------------------
    if not missing("P4", s7):
        since = (dt.date.fromisoformat(td) - dt.timedelta(days=POLICY["P4_recente_giorni"])).isoformat()
        for e in sorted(s7["evidenze_dettaglio"], key=lambda e: (e["data"], e["uid"])):
            cp = e.get("classe_principale") or {}
            if e["data"] < since or cp.get("valore") not in POLICY["P4_classi"] or cp.get("stato") not in POLICY["P4_stati_classe"]:
                continue
            what = "assenza docente" if e.get("caso_speciale") == "ASSENZA_DOCENTE" else cp["valore"]
            voci.append(_voce("P4", POLICY["P4_massimo"], s7["affidabilita"], "comunicazione recente (%s): %s (%s)" % (
                e["data"], what, cp["stato"]), e["uid"], e["data"], ["S7"], _evidence(s7, [e["uid"]]),
                [_azione("leggere_comunicazione", e["uid"], s7["affidabilita"])]))

    # ---- conflitti tra regole sullo stesso oggetto: tiene la priorita' piu' alta, a pari l'ordine fisso; nessuna perdita ---------
    by_obj = {}
    for v in voci:
        by_obj.setdefault(v["oggetto"], []).append(v)
    kept = []
    for obj in sorted(by_obj):
        vs = sorted(by_obj[obj], key=lambda v: (-_RANK[v["priorita"]], RULE_ORDER.index(v["regola"])))
        main = vs[0]
        for other in vs[1:]:
            main["azioni"] += [dict(a, secondaria_da=other["regola"]) for a in other["azioni"]
                               if (a["azione"], a["uid"]) not in {(b["azione"], b["uid"]) for b in main["azioni"]}]
            main["evidenze"] += [x for x in other["evidenze"] if x not in main["evidenze"]]
            main["segnali"] = sorted(set(main["segnali"]) | set(other["segnali"]))
            main["affidabilita"] = _worst(main["affidabilita"], other["affidabilita"])
            main.setdefault("regole_unite", []).append(other["regola"])
        kept.append(main)
    voci = kept

    # ---- P6 -----------------------------------------------------------------------------------------------------------
    for v in voci:
        if _RANK[v["priorita"]] >= _RANK[POLICY["P6_soglia_priorita"]] and v["affidabilita"] in POLICY["P6_affidabilita"] and \
                not any(a["azione"] == "verificare_informazione" and a["uid"] == v["oggetto"] for a in v["azioni"]):
            v["azioni"].append(_azione("verificare_informazione", v["oggetto"], v["affidabilita"],
                                       "P6: priorita' %s ma affidabilita' %s" % (v["priorita"], v["affidabilita"])))
    voci.sort(key=lambda v: (-_RANK[v["priorita"]], v["data"] or "9999", RULE_ORDER.index(v["regola"]), v["oggetto"]))

    # ---- materia: priorita', affidabilita' (monotona), stato ------------------------------------------------------------
    cap = "non_determinabile" if non_valutate else None
    if voci:
        top = max(_RANK[v["priorita"]] for v in voci)
        prio = next(k for k, r in _RANK.items() if r == top)
        decisive = [v["affidabilita"] for v in voci if _RANK[v["priorita"]] == top]
    else:
        prio = "non_determinabile" if non_valutate else "nessuna"
        decisive = [s["affidabilita"] for s in (s1, s2, s3, s6, s7) if s["valore"] is not None]
    aff = _worst(*(decisive + ([cap] if cap else [])))
    da_verificare = any(_RANK[v["priorita"]] >= _RANK["attenzione"] and v["affidabilita"] in ("bassa", "non_determinabile") for v in voci)
    stato = "parziale" if non_valutate else ("da_verificare" if da_verificare else "completa")

    s4, s5, s8, s9 = (_sig(doc, k) for k in ("S4", "S5", "S8", "S9"))
    contesto = {"nota": "P5: solo contesto, nessuna priorita' e nessuna valutazione",
                "S4_continuita_lezioni": {"valore": s4["valore"], "affidabilita": s4["affidabilita"]},
                "S5_argomenti_recenti": {"n": len(s5["valore"]["argomenti"]) if s5["valore"] else None, "affidabilita": s5["affidabilita"]},
                "S8_attribuzione_classroom": {"stato": (s8["valore"] or {}).get("stato"), "affidabilita": s8["affidabilita"]},
                "S9_dati_valutativi": {"stato": (s9["valore"] or {}).get("stato"), "affidabilita": s9["affidabilita"]}}
    ev_uids = {x["uid"] for v in voci for x in v["evidenze"]}
    ambig = [dict(a, tocca_voci=sorted(v["id"] for v in voci if {x["uid"] for x in v["evidenze"]} & set(a.get("uid") or [])))
             for a in doc["ambiguita"]]
    out = {"materia": doc["materia"]["chiave"], "nome_ufficiale": doc["materia"]["nome_ufficiale"],
           "priorita": prio, "affidabilita": aff, "stato_decisione": stato, "da_verificare": da_verificare,
           "voci": voci, "regole_non_valutate": non_valutate, "contesto": contesto,
           "copertura": {k: (v.get("stato") if isinstance(v, dict) and "stato" in v else v) for k, v in doc["copertura"].items()
                         if k != "classroom"} | {"classroom_corsi_confermati": [c["id"] for c in doc["copertura"]["classroom"]["corsi_confermati"]]},
           "storico": {"anni": doc["storico"]["anni"], "nota": "contesto storico separato: mai usato nelle voci"},
           "gaps": doc["gap"], "ambiguities": [a for a in ambig if a["tocca_voci"]] if not dettaglio else ambig,
           "evidenze_uid_n": len(ev_uids)}
    if dettaglio:
        used = sorted({s for v in voci for s in v["segnali"]})
        out["segnali_m8"] = [{k: v for k, v in s.items() if k != "evidenze_dettaglio"} for s in doc["segnali"] if s["id"] in used]
    return out


# ------------------------------------------------------------------------------------------------ costruzione
def build(reg, now_dt, manual=None, materia=None, dettaglio=False, soglia_ore=48, *, subjects, source_path=None):
    """cloud.school.priority/1: una materia (`materia`) o la panoramica delle 8. Sola lettura; `now_dt` letto UNA volta dal chiamante."""
    if not isinstance(dettaglio, bool):
        raise ValueError("dettaglio non valido: %r (true | false)" % (dettaglio,))
    if materia is not None and not (isinstance(materia, str) and materia.strip()):
        raise ValueError("materia non valida: %r" % (materia,))
    keys = [SC.resolve_subject(materia.strip(), subjects)] if materia else list(SC.AGENT_SUBJECTS)
    docs = [ST.build(reg, k, now_dt, manual=manual, dettaglio=True, soglia_ore=soglia_ore, subjects=subjects, source_path=source_path)
            for k in keys]
    materie = [subject_priority(d, dettaglio=dettaglio) for d in docs]
    first_date = lambda m: min((v["data"] for v in m["voci"] if v["data"] and _RANK[v["priorita"]] == _RANK.get(m["priorita"], -1)),  # noqa: E731
                               default="9999")
    materie.sort(key=lambda m: (ORDER_SUBJECTS.index(m["priorita"]), first_date(m), m["materia"]))
    d0 = docs[0]
    return {
        "schema": SCHEMA,
        "natura": NATURA,
        "riferimento_temporale": {"now": d0["riferimento_temporale"]["now"], "data_riferimento": d0["riferimento_temporale"]["data_riferimento"],
                                  "anno_operativo": d0["riferimento_temporale"]["anno_operativo"],
                                  "nota": "now letto UNA volta dal chiamante; giorni calcolati sulle date M8"},
        "politica": {"versione": priority_version(), "valori": POLICY, "regole": RULES, "affidabilita": RELIABILITY_RULES,
                     "livelli_priorita": list(PRIORITY_LEVELS), "azioni": list(ACTIONS)},
        "ambito": {"materia": keys[0] if materia else None, "materie": keys, "dettaglio": dettaglio},
        "sintesi": {p: [m["materia"] for m in materie if m["priorita"] == p] for p in ORDER_SUBJECTS},
        "materie": materie,
        "ordinamento": "priorita' (alta, attenzione, informativa, non_determinabile, nessuna), poi data della voce decisiva, poi materia: "
                       "nessun confronto di importanza tra materie",
        "provenance": {"schema": SCHEMA, "priority_version": priority_version(), "status_version": ST.status_version(),
                       "context_version": d0["provenance"]["context_version"], "comm_version": d0["provenance"]["comm_version"],
                       "link_version": d0["provenance"]["link_version"], "schema_registro": d0["provenance"]["schema_registro"],
                       "sorgente": source_path, "kind_legacy": d0["provenance"]["kind_legacy"],
                       "nota_legacy": "D1: legacy e' solo provenance"},
        "regole_agente": RULES_FOR_AGENTS,
    }


def build_from_path(path, now, manual_path=None, **kw):
    """Registro aperto come COPIA in memoria (Registry.from_snapshot, immutable=1): nessun byte del file viene scritto."""
    reg = R.Registry.from_snapshot(path)
    try:
        return build(reg, now, manual=SC.load_manual(manual_path)["dati"], source_path=os.path.abspath(path), **kw)
    finally:
        reg.close()
