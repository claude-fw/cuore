"""STUDY SESSION RUNTIME (M13 v1): `cloud.school.study_session/1`. Modulo PURO e SOLA LETTURA.

M13 e' il primo layer che abilita assistenza didattica, ma resta un ORCHESTRATORE/GUARDRAIL: non genera la risposta didattica (la
scrive Claude). Stabilisce, per UNA materia e UNA modalita', su quale contenuto si lavora, con quale provenienza, entro quali azioni
ammesse e vietate e con quali frasi obbligatorie.

Tre fonti SEMPRE separate:
  * `contesto_scolastico`     = cio' che M12 espone (contratto pubblico M12, unico input scolastico; nessuna lettura di M1-M11);
  * `contenuto_fornito`       = il testo fornito esplicitamente nella richiesta (`contenuto_fornito_utente`), oppure riferimenti
                                ESPLICITI a contenuti gia' esposti da M12 (`contenuto_scolastico`, risolti per uid esatto);
  * `conoscenza_generale`     = cio' che Claude aggiunge: va SEMPRE etichettato come tale e mai attribuito alla scuola.
Le azioni vietate di M12 (es. spiegare_argomento, creare_esercizi_o_quiz) restano valide per il CONTESTO SCOLASTICO; le azioni
didattiche di M13 valgono SOLO sul contenuto fornito. Nessuna acquisizione automatica di materiali, nessuna inferenza scolastica nuova,
nessun voto, diagnosi, giudizio sulla preparazione, stima di tempo, piano o scelta di cosa studiare.
Stesso input -> stesso JSON; nessun orologio (now dal chiamante, passato a M12); nessuna scrittura.
"""
from __future__ import annotations

import hashlib
import os

import cvv_client as cv
import registry as R
import school_common as CM
import school_context as SC
import school_study_assistant as SA

SCHEMA = "cloud.school.study_session/1"
SESSION_REV = 1

MODES = ("spiega", "interroga", "esercitati", "correggi", "ripassa", "prepara_obiettivo")
PROVENANCE = ("contenuto_scolastico", "contenuto_fornito_utente", "conoscenza_generale")
OUTCOMES = ("ammessa", "solo_contesto", "contenuto_insufficiente", "obiettivo_richiesto", "obiettivo_non_trovato")
PREVIEW = 160

PROVENANCE_DEF = {
    "contenuto_scolastico": "testo di un elemento esposto da M12 e richiamato esplicitamente per uid (uid/hash M12): fonte scolastica",
    "contenuto_fornito_utente": "testo fornito dall'utente nella richiesta: NON verificato dalle fonti scolastiche (l'origine dichiarata "
                                "dall'utente resta una dichiarazione)",
    "conoscenza_generale": "cio' che Claude aggiunge per chiarire: va etichettato come tale; mai presentato come proveniente da "
                           "ClasseViva/Classroom/M11/M12",
}
COMMON_FORBIDDEN = ("assegnare_voti", "diagnosticare", "valutare_preparazione_globale", "dichiarare_pronto_o_indietro", "stimare_tempo",
                    "proporre_piano_automatico", "scegliere_cosa_studiare", "dichiarare_programma_verifica",
                    "attribuire_conoscenza_generale_alla_scuola", "acquisire_materiali", "inferire_dati_scolastici",
                    "inventare_argomenti_obiettivo", "inventare_contenuto_scolastico")
MODE_FORBIDDEN = {
    "spiega": (),
    "interroga": ("presentare_domande_come_programma",),
    "esercitati": ("presentare_esercizi_come_richiesti_dalla_scuola",),
    "correggi": ("giudizio_globale_dalla_correzione",),
    "ripassa": ("chiamare_programma_completo",),
    "prepara_obiettivo": ("dichiarare_coincidenza_contenuto_programma",),
}
MODE_ALLOWED = {
    "spiega": ("spiegare_contenuto_fornito", "chiarire_con_conoscenza_generale_etichettata", "fare_esempi_etichettati", "citare_fonte"),
    "interroga": ("generare_domande_sul_contenuto", "valutare_singola_risposta_sul_contenuto", "citare_fonte"),
    "esercitati": ("generare_esercizi_sul_contenuto", "fornire_soluzioni_etichettate", "citare_fonte"),
    "correggi": ("analizzare_risposta_utente", "spiegare_errori", "chiarire_con_conoscenza_generale_etichettata", "citare_fonte"),
    "ripassa": ("organizzare_concetti", "elencare_punti_chiave", "evidenziare_relazioni_e_formule", "generare_domande_di_ripasso",
                "citare_fonte"),
    "prepara_obiettivo": ("riportare_contesto_obiettivo_m12", "indicare_limite", "lavorare_sul_contenuto_fornito",
                          "generare_domande_sul_contenuto", "generare_esercizi_sul_contenuto", "citare_fonte"),
}
INSUFFICIENT_ALLOWED = ("chiedere_contenuto_all_utente", "indicare_limite")
CONTEXT_ONLY_ALLOWED = ("riportare_contesto_obiettivo_m12", "indicare_limite", "chiedere_contenuto_all_utente", "citare_fonte")
PHRASES = {
    "conoscenza_generale": "Le parti aggiunte da conoscenza generale vanno indicate come tali: non provengono dalla scuola.",
    "contenuto_utente": "Il contenuto e' stato fornito dall'utente: non e' verificato dalle fonti scolastiche.",
    "contenuto_scolastico": "Il contenuto scolastico e' solo quello richiamato da M12, riportato com'e'.",
    "interroga": "Le domande sono basate sul contenuto fornito: non rappresentano il programma della verifica.",
    "esercitati": "Gli esercizi sono generati sul contenuto fornito: non sono richiesti dalla scuola.",
    "correggi": "La correzione riguarda solo questa risposta: non e' una valutazione della preparazione.",
    "correggi_senza_riferimento": "Non e' stato fornito un contenuto di riferimento: la correzione usa conoscenza generale, da etichettare.",
    "ripassa": "Il ripasso organizza solo il contenuto fornito: non e' il programma completo.",
    "argomenti_obiettivo": "Gli argomenti della verifica non sono determinati dalle fonti scolastiche.",
    "non_coincide_programma": "Non risulta che il contenuto fornito coincida con il programma della verifica.",
    "contenuto_parziale": "Il testo scolastico richiamato e' troncato: il contenuto e' parziale.",
}
INSUFFICIENT = {
    "contenuto_mancante": "Per questa modalita' serve un contenuto fornito (testo o riferimento a un elemento M12): non e' stato fornito.",
    "risposta_mancante": "Per correggere serve la risposta dell'utente (risposta_utente): non e' stata fornita.",
    "riferimento_non_trovato": "Un riferimento indicato non corrisponde a nessun contenuto esposto da M12 per questa materia.",
    "riferimento_senza_testo": "Un riferimento M12 indicato non ha testo utilizzabile (es. materiale senza titolo).",
    "obiettivo_richiesto": "Per prepara_obiettivo serve l'uid di un obiettivo M12: nessun obiettivo viene scelto automaticamente.",
    "obiettivo_non_trovato": "L'obiettivo indicato non e' tra gli obiettivi esposti da M12 per la materia.",
}
# Lessico vietato di M13: SOLO giudizio, tempo e piano. I verbi di studio (spiega, ripassa, esercitati) sono ammessi: M13 abilita lo
# studio sul contenuto fornito, quindi NON eredita il divieto dei verbi di studio di M11/M12 (che riguardava i dati scolastici).
FORBIDDEN_WORDS = ("bravo", "brava", "scarso", "scarsa", "insufficiente", "sufficiente", "ottimo", "pessimo", "rendimento basso",
                   "rendimento alto", "voto stimato", "promosso", "bocciato", "sei pronto", "sei pronta", "sei indietro",
                   "sei preparato", "sei preparata", "devi studiare", "ore di studio", "minuti di studio", "piano di studio",
                   "programma completo della verifica", "lacune")
NATURA = ("ORCHESTRATORE/GUARDRAIL della sessione di studio in sola lettura: separa contesto scolastico (M12), contenuto fornito e "
          "conoscenza generale; definisce azioni ammesse/vietate e frasi obbligatorie; non genera la risposta didattica")
RULES_FOR_AGENTS = [
    "Lavora didatticamente SOLO sul contenuto indicato in 'contenuto': non inventare contenuto scolastico.",
    "Etichetta ogni parte della risposta: contenuto_scolastico, contenuto_fornito_utente o conoscenza_generale.",
    "Non presentare mai la conoscenza generale come proveniente da ClasseViva, Classroom, M11 o M12.",
    "Rispetta azioni_ammesse e azioni_vietate della sessione e riporta le frasi_obbligatorie.",
    "Il contesto_scolastico (M12) mantiene i suoi limiti e i suoi divieti: non ricavare argomenti di una verifica dalle fonti scolastiche.",
    "Nessun voto, diagnosi, giudizio globale sulla preparazione, stima di tempo, piano o scelta di cosa studiare.",
    "Se l'esito non e' 'ammessa', chiedi il contenuto mancante o riporta solo il contesto: non sostituirlo con contenuti inventati.",
]


def study_session_version():
    return cv.interp_version(SESSION_REV, list(MODES), list(PROVENANCE), list(OUTCOMES), PREVIEW, PROVENANCE_DEF, list(COMMON_FORBIDDEN),
                             MODE_FORBIDDEN, MODE_ALLOWED, list(INSUFFICIENT_ALLOWED), list(CONTEXT_ONLY_ALLOWED), PHRASES, INSUFFICIENT,
                             list(FORBIDDEN_WORDS), SA.study_assistant_version())


# ------------------------------------------------------------------------------------------------ contenuto
def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_content(contenuto):
    """-> (testo_utente | None, origine_dichiarata | None, riferimenti [uid], risposta_utente | None). Nessuna acquisizione esterna."""
    if contenuto is None:
        return None, None, [], None
    if isinstance(contenuto, str):
        return contenuto, None, [], None
    if not isinstance(contenuto, dict):
        raise ValueError("contenuto non valido: %r (testo oppure oggetto {testo, origine_dichiarata, riferimenti_m12, risposta_utente})"
                         % (type(contenuto).__name__,))
    unknown = sorted(set(contenuto) - {"testo", "origine_dichiarata", "riferimenti_m12", "risposta_utente"})
    if unknown:
        raise ValueError("campi del contenuto non ammessi: %s" % ", ".join(unknown))
    refs = contenuto.get("riferimenti_m12") or []
    if not isinstance(refs, list) or not all(isinstance(r, str) and r.strip() for r in refs):
        raise ValueError("riferimenti_m12 non validi: lista di uid esatti di contenuti M12")
    for k in ("testo", "origine_dichiarata", "risposta_utente"):
        if contenuto.get(k) is not None and not isinstance(contenuto[k], str):
            raise ValueError("%s non valido: deve essere testo" % k)
    return contenuto.get("testo"), contenuto.get("origine_dichiarata"), [r.strip() for r in refs], contenuto.get("risposta_utente")


def _part(pid, provenienza, testo, dettaglio, **extra):
    p = {"id": pid, "provenienza": provenienza, "sha256": _sha(testo), "lunghezza": len(testo),
         "anteprima": CM.cap_text(testo, PREVIEW)[0], "anteprima_troncata": CM.cap_text(testo, PREVIEW)[1]}
    if dettaglio:
        p["testo"] = testo
    p.update(extra)
    return p


def _m12_index(m12):
    """uid -> primo contenuto M12 (ordine fisso delle modalita' M12). Solo contenuti esposti da M12."""
    idx = {}
    for m in m12["modalita"]:
        for c in m["contenuti"]:
            idx.setdefault(c["uid"], (m["modalita"], c))
    return idx


def _school_text(c):
    f = c["campi"]
    if c["tipo"] == "obiettivo":
        return f.get("testo_dichiarato"), bool(f.get("testo_troncato"))
    if c["tipo"] == "lezione":
        return f.get("argomento"), False
    if c["tipo"] == "comunicazione":
        return f.get("testo"), bool(f.get("testo_troncato"))
    return None, False                                       # materiale: nessun titolo/testo a questo livello (D-M12-7)


# ------------------------------------------------------------------------------------------------ costruzione
def build(reg, materia, modalita, contenuto, now_dt, manual=None, obiettivo=None, dettaglio=False, soglia_ore=48, *, subjects,
          source_path=None):
    """cloud.school.study_session/1. Sola lettura; `now_dt` letto UNA volta dal chiamante e passato a M12."""
    if not (isinstance(materia, str) and materia.strip()):
        raise ValueError("materia obbligatoria: %r" % (materia,))
    if modalita not in MODES:
        raise ValueError("modalita non valida: %r (ammesse: %s)" % (modalita, ", ".join(MODES)))
    if obiettivo is not None and not (isinstance(obiettivo, str) and obiettivo.strip()):
        raise ValueError("obiettivo non valido: %r (uid esatto di un obiettivo M12)" % (obiettivo,))
    if not isinstance(dettaglio, bool):
        raise ValueError("dettaglio non valido: %r (true | false)" % (dettaglio,))
    text, declared, refs, answer = _parse_content(contenuto)
    obj = obiettivo.strip() if obiettivo else None

    m12 = SA.build(reg, materia, now_dt, manual=manual, obiettivo=obj, dettaglio=False, soglia_ore=soglia_ore, subjects=subjects,
                   source_path=source_path)
    idx = _m12_index(m12)
    limits, insuff = [], []

    def miss(code, **extra):
        d = dict({"codice": code, "messaggio": INSUFFICIENT[code]}, **extra)
        if d not in insuff:
            insuff.append(d)

    # ---- contenuto fornito: parti con provenienza esplicita -------------------------------------------------------
    parts = []
    if text is not None and text.strip():
        extra = {"origine_dichiarata": declared, "nota": "dichiarazione dell'utente, non verificata"} if declared else {}
        parts.append(_part("utente", "contenuto_fornito_utente", text, dettaglio, verifica="non_verificato_dalle_fonti", **extra))
    for i, uid in enumerate(refs):
        hit = idx.get(uid)
        if hit is None:
            miss("riferimento_non_trovato", uid=uid)
            continue
        mode12, c = hit
        st, trunc = _school_text(c)
        if not (st or "").strip():
            miss("riferimento_senza_testo", uid=uid)
            continue
        parts.append(_part("m12_%d" % i, "contenuto_scolastico", st, dettaglio, verifica="fonte_scolastica_m12",
                           riferimento_m12={"uid": uid, "hash": c.get("hash"), "tipo": c["tipo"], "modalita_m12": mode12,
                                            "affidabilita": c.get("affidabilita"), "stato_dato": c["stato_dato"]},
                           **({"testo_troncato": True} if trunc else {})))
        if trunc:
            limits.append({"codice": "contenuto_parziale", "uid": uid})
    answer_part = _part("risposta", "contenuto_fornito_utente", answer, dettaglio, ruolo="risposta_da_correggere",
                        verifica="non_verificato_dalle_fonti") if (answer or "").strip() else None

    # ---- esito e azioni ---------------------------------------------------------------------------------------------
    m12_esito = m12["esito_richiesta"]
    quadro = next(m for m in m12["modalita"] if m["modalita"] == "quadro_obiettivo")
    obj_ctx = None
    if obj is not None and m12_esito["stato"] == "obiettivo_non_trovato":
        outcome = "obiettivo_non_trovato"
        miss("obiettivo_non_trovato", uid=obj, obiettivi_esistenti=m12_esito.get("obiettivi_esistenti", []))
    elif modalita == "prepara_obiettivo" and obj is None:
        outcome = "obiettivo_richiesto"
        miss("obiettivo_richiesto", obiettivi_esistenti=[c["uid"] for c in quadro["contenuti"]])
    elif modalita == "correggi" and answer_part is None:
        outcome = "contenuto_insufficiente"
        miss("risposta_mancante")
    elif modalita not in ("prepara_obiettivo", "correggi") and not parts:
        outcome = "contenuto_insufficiente"
        miss("contenuto_mancante")
    elif modalita == "prepara_obiettivo" and not parts:
        outcome = "solo_contesto"
        miss("contenuto_mancante")
    else:
        outcome = "ammessa"

    if modalita == "prepara_obiettivo" and obj is not None and outcome != "obiettivo_non_trovato":
        obj_ctx = {"quadro_obiettivo": quadro,
                   "materiale_collegato": next(m for m in m12["modalita"] if m["modalita"] == "materiale_collegato")}
        limits.append({"codice": "argomenti_obiettivo_non_determinati", "fonte": "M11/M12 (U4 sempre non determinabile)"})

    if outcome == "ammessa":
        allowed = list(MODE_ALLOWED[modalita])
        if modalita == "correggi" and not parts:
            limits.append({"codice": "correzione_senza_riferimento"})
    elif outcome == "solo_contesto":
        allowed = list(CONTEXT_ONLY_ALLOWED)
    else:
        allowed = list(INSUFFICIENT_ALLOWED)
    forbidden = list(COMMON_FORBIDDEN) + list(MODE_FORBIDDEN[modalita])

    phrases = []
    if outcome == "ammessa":
        phrases.append("conoscenza_generale")
    if any(p["provenienza"] == "contenuto_fornito_utente" for p in parts) or answer_part:
        phrases.append("contenuto_utente")
    if any(p["provenienza"] == "contenuto_scolastico" for p in parts):
        phrases.append("contenuto_scolastico")
    if outcome == "ammessa" and modalita in ("interroga", "esercitati", "correggi", "ripassa"):
        phrases.append(modalita)
    if outcome == "ammessa" and modalita == "correggi" and not parts:
        phrases.append("correggi_senza_riferimento")
    if obj_ctx is not None:
        phrases.append("argomenti_obiettivo")
        if parts:
            phrases.append("non_coincide_programma")
    if any(l["codice"] == "contenuto_parziale" for l in limits):
        phrases.append("contenuto_parziale")

    contenuto_out = {"parti": parts, "risposta_utente": answer_part,
                     "sha256_complessivo": _sha("|".join(p["sha256"] for p in parts + ([answer_part] if answer_part else []))),
                     "nota": "contenuto fornito nella richiesta (testo) o richiamato da M12 per uid: nessuna acquisizione automatica"}
    out = {
        "schema": SCHEMA,
        "natura": NATURA,
        "materia": m12["richiesta"]["materia"],
        "obiettivo": obj,
        "modalita": modalita,
        "esito": {"stato": outcome, "informazione_insufficiente": insuff},
        "contenuto": contenuto_out,
        "contesto_scolastico": {"fonte": "contratto pubblico M12", "esito_m12": m12_esito, "disponibilita_m12": m12["disponibilita"],
                                "obiettivo": obj_ctx, "informazione_insufficiente_m12": m12["informazione_insufficiente"],
                                "azioni_vietate_m12": m12["politica"]["azioni_vietate"],
                                "ambito_divieti_m12": "i divieti M12 valgono per il contesto scolastico; le azioni didattiche M13 valgono solo "
                                                      "sul contenuto fornito"},
        "sessione": {"azioni_ammesse": allowed, "azioni_vietate": forbidden,
                     "frasi_obbligatorie": [{"codice": c, "frase": PHRASES[c]} for c in phrases],
                     "etichette_provenienza": PROVENANCE_DEF, "limiti": limits},
        "regole_agente": RULES_FOR_AGENTS,
        "provenance": {"schema": SCHEMA, "study_session_version": study_session_version(),
                       "study_assistant_version": m12["provenance"]["study_assistant_version"],
                       "study_context_version": m12["provenance"]["study_context_version"],
                       "action_version": m12["provenance"]["action_version"], "priority_version": m12["provenance"]["priority_version"],
                       "status_version": m12["provenance"]["status_version"], "context_version": m12["provenance"]["context_version"],
                       "sorgente": source_path, "riferimento_temporale": m12["riferimento_temporale"],
                       "input": "contratto pubblico M12 + contenuto fornito nella richiesta"},
    }
    if obj_ctx is None:
        out["contesto_scolastico"].pop("obiettivo")
    return out


def build_from_path(path, materia, modalita, contenuto, now, manual_path=None, **kw):
    """Registro aperto come COPIA in memoria (Registry.from_snapshot, immutable=1): nessun byte del file viene scritto."""
    reg = R.Registry.from_snapshot(path)
    try:
        return build(reg, materia, modalita, contenuto, now, manual=SC.load_manual(manual_path)["dati"], source_path=os.path.abspath(path),
                     **kw)
    finally:
        reg.close()
