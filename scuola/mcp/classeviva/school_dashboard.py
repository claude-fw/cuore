"""MODELLO DI VISTA DELLA DASHBOARD (M16 v1): `cloud.school.dashboard/1`. Modulo PURO e SOLA LETTURA.

La dashboard e' la UI del sistema Cloud Scuola; questo modulo ne calcola il CONTENUTO nel backend, pagina per pagina, cosi' che qualunque
superficie (oggi la dashboard CUORE, `app/`) sia solo una vista. Nessuna logica scolastica nuova: solo SELEZIONE, ORDINAMENTO e
FORMATTAZIONE dei contratti pubblici gia' verificati:
  M3 freshness/eventi (registry.freshness, registry.events) · M8 school_status · M9 school_priority · M10 school_action ·
  M12 school_study_assistant · M14 school_records · M15 school_materials_catalog.
I layer non ancora esistenti (M18 dichiarazioni, M19 store, M21 profilo, M22 piano) compaiono come blocchi `non_attivo`.

Ogni blocco: `stato` (dati | vuoto_valido | non_letto | non_disponibile | non_attivo), `affidabilita` quando la fonte la fornisce,
`provenienza` (contratto + versione + uid). Distinzioni sempre esplicite: dato reale / mancante, zero / null, anno operativo / storico,
valore calcolato / valore non verificato (es. medie M14: corrispondenza con ClasseViva non verificata), provenienza / sola
visualizzazione. I grafici sono SERIE DICHIARATIVE (tipo, assi, punti con uid, formula): emessi solo con almeno un punto reale; null
non diventa mai 0. Stesso input -> stesso JSON; nessun orologio (now dal chiamante, uno solo per tutti i layer); nessuna scrittura.
"""
from __future__ import annotations

import collections
import datetime as dt
import os

import cls_derive as D
import cvv_client as cv
import registry as R
import school_action as AC
import school_context as SC
import school_context4 as C4
import school_materials_catalog as MC
import school_priority as PR
import school_records as RC
import school_status as ST
import school_study_assistant as SA
import school_study_session as SS

SCHEMA = "cloud.school.dashboard/1"
DASHBOARD_REV = 1

PAGES = ("home", "attenzione", "materie", "materia", "verifiche", "compiti", "voti", "assenze", "studio", "materiali", "storico",
         "qualita_dati")
BLOCK_STATES = ("dati", "vuoto_valido", "non_letto", "non_disponibile", "non_attivo")
CHART_TYPES = ("linea", "barre", "barre_impilate", "calendario", "indicatore")
NEWS_DAYS = 7                       # finestra di presentazione delle novita' del Registry (solo vista, nessuna interpretazione)
NEWS_SHOWN = 15
INACTIVE = {
    "dichiarazioni": "M18 (dichiarazioni e conferme utente) non ancora attivo",
    "store_materiali": "M19 (acquisizione materiali) non ancora attivo: nessun materiale disponibile in locale",
    "profilo": "M21 (profilo su evidenze) non ancora attivo",
    "pianificazione": "M22 (pianificazione vincolata) non ancora attivo",
}
FORBIDDEN_WORDS = ("bravo", "brava", "scarso", "scarsa", "insufficiente", "ottimo", "pessimo", "sei pronto", "sei pronta",
                   "sei indietro", "la tua media", "rendimento basso", "rendimento alto", "devi studiare", "voto atteso")
NATURA = ("modello di vista DERIVATO e in SOLA LETTURA della dashboard Cloud Scuola: seleziona e formatta contratti gia' verificati; "
          "il backend resta la fonte di verita', la UI e' una proiezione")
RULES_FOR_AGENTS = [
    "Mostra ogni blocco con il suo stato: 'vuoto_valido' (fonte letta, nessun dato) non e' 'non_letto' ne' 'non_disponibile'.",
    "Un valore null resta null: non disegnarlo come zero e non riempirlo.",
    "Le medie sono politiche nominate con corrispondenza ClasseViva non verificata: indicale cosi', mai come media ufficiale.",
    "Lo storico ha una pagina sua: non mescolarlo all'anno operativo.",
    "I grafici derivano solo dalle serie fornite (punti con uid): non aggiungere indicatori.",
    "I blocchi 'non_attivo' indicano layer futuri: non simularne il contenuto.",
]


def dashboard_version():
    return cv.interp_version(DASHBOARD_REV, list(PAGES), list(BLOCK_STATES), list(CHART_TYPES), NEWS_DAYS, NEWS_SHOWN, INACTIVE,
                             list(FORBIDDEN_WORDS))


# ------------------------------------------------------------------------------------------------ fonti (lazy, stesso now)
class _Sources:
    """Calcola i contratti sorgente al primo uso, con lo STESSO now, registro e manuale. Solo letture."""

    def __init__(self, reg, now_dt, manual, subjects, soglia_ore, source_path):
        self.reg, self.now, self.manual, self.subjects, self.soglia, self.path = reg, now_dt, manual, subjects, soglia_ore, source_path
        self._c = {}
        self._used = set()                       # chiavi richieste dalla pagina in costruzione (provenienza per pagina)

    def _get(self, key, fn):
        self._used.add(key)
        if key not in self._c:
            self._c[key] = fn()
        return self._c[key]

    def m8(self, k):
        return self._get(("m8", k), lambda: ST.build(self.reg, k, self.now, manual=self.manual, dettaglio=True, soglia_ore=self.soglia,
                                                      subjects=self.subjects, source_path=self.path))

    def m9(self):
        return self._get("m9", lambda: PR.build(self.reg, self.now, manual=self.manual, dettaglio=True, soglia_ore=self.soglia,
                                                subjects=self.subjects, source_path=self.path))

    def m10(self):
        return self._get("m10", lambda: AC.build(self.reg, self.now, manual=self.manual, soglia_ore=self.soglia, subjects=self.subjects,
                                                 source_path=self.path))

    def m12(self, k):
        return self._get(("m12", k), lambda: SA.build(self.reg, k, self.now, manual=self.manual, soglia_ore=self.soglia,
                                                       subjects=self.subjects, source_path=self.path))

    def m14(self):
        return self._get("m14", lambda: RC.build(self.reg, self.now, manual=self.manual, soglia_ore=self.soglia, subjects=self.subjects,
                                                 source_path=self.path))

    def m15(self, k=None, **kw):
        key = ("m15", k, tuple(sorted(kw.items())))
        return self._get(key, lambda: MC.build(self.reg, self.now, manual=self.manual, materia=k, soglia_ore=self.soglia,
                                               subjects=self.subjects, source_path=self.path, **kw))

    def fresh(self):
        return self._get("fresh", lambda: self.reg.freshness(self.now.isoformat(), stale_after_hours=self.soglia))


# ------------------------------------------------------------------------------------------------ helper
def _blk(bid, titolo, stato, dati=None, provenienza=None, affidabilita=None, nota=None):
    assert stato in BLOCK_STATES, stato
    b = {"id": bid, "titolo": titolo, "stato": stato, "affidabilita": affidabilita, "provenienza": provenienza or {}, "dati": dati}
    if nota:
        b["nota"] = nota
    return b


def _prov(contratto, versione, uid=None):
    p = {"contratto": contratto, "versione": versione}
    if uid is not None:
        p["uid"] = sorted(set(u for u in uid if u))
    return p


def _chart(cid, titolo, tipo, asse_x, asse_y, serie, provenienza, formula=None, verificato=None):
    """Grafico dichiarativo: emesso solo se ha almeno un punto reale (y non null). Altrimenti None."""
    assert tipo in CHART_TYPES
    clean = [dict(s, punti=[p for p in s["punti"] if p.get("y") is not None]) for s in serie]
    clean = [s for s in clean if s["punti"]]
    if not clean:
        return None
    c = {"id": cid, "titolo": titolo, "tipo": tipo, "asse_x": asse_x, "asse_y": asse_y, "serie": clean, "provenienza": provenienza}
    if formula:
        c["formula"] = formula
    if verificato is not None:
        c["valore_verificato"] = verificato
    return c


def _src_state(stato):
    return {"presente": "dati", "vuoto_valido": "vuoto_valido", "non_letto": "non_letto"}.get(stato, "non_disponibile")


def _inactive(key):
    return _blk("non_attivo_%s" % key, key.replace("_", " "), "non_attivo", nota=INACTIVE[key])


def _sig(doc, sid):
    return next(s for s in doc["segnali"] if s["id"] == sid)


def _news(reg, now_dt):
    """Eventi del Registry negli ultimi NEWS_DAYS giorni; i 'create' del PRIMO run commit di un kind sono 'prima_lettura', non novita'."""
    since = now_dt - dt.timedelta(days=NEWS_DAYS)
    first_run = {}
    for r in reg.runs():
        if r["mode"] == "commit":
            first_run.setdefault((r["source"], r["kind"]), r["run_id"])
    rows, initial = [], 0
    for e in reg.events():
        at = dt.datetime.fromisoformat(e["at"])                    # istanti con offset (UTC nel Registry): confronto tra datetime, non stringhe
        if at < since or at > now_dt:
            continue
        if e["event"] == "create" and first_run.get((e["source"], e["kind"])) == e["run_id"]:
            initial += 1
            continue
        rows.append({"at": e["at"], "ora_locale": at.astimezone(D._zone()).isoformat(), "fonte": e["source"], "kind": e["kind"],
                     "uid_fonte": e["source_uid"], "evento": e["event"]})
    rows.sort(key=lambda r: (dt.datetime.fromisoformat(r["at"]), r["fonte"], r["kind"], r["uid_fonte"], r["evento"]), reverse=True)
    counts = collections.Counter((r["fonte"] + "/" + r["kind"], r["evento"]) for r in rows)
    return rows, initial, {"%s %s" % k: v for k, v in sorted(counts.items())}


# ------------------------------------------------------------------------------------------------ pagine
def _page_home(S):
    m9, m10, m14, fresh = S.m9(), S.m10(), S.m14(), S.fresh()
    td = m9["riferimento_temporale"]["data_riferimento"]
    blocks = [_blk("oggi", "Oggi", "dati", {"data": td, "anno_operativo": m9["riferimento_temporale"]["anno_operativo"]},
                   _prov("cloud.school.priority/1", m9["provenance"]["priority_version"]))]
    sint = {k: v for k, v in m9["sintesi"].items()}
    blocks.append(_blk("attenzione", "Cosa merita attenzione", "dati", {"materie_per_priorita": sint,
                       "dettaglio": "pagina 'attenzione'"}, _prov("cloud.school.priority/1", m9["provenance"]["priority_version"])))
    imp = []
    for m in m9["materie"]:
        for v in m["voci"]:
            if v["regola"] in ("P1", "P3a") and v["data"] and v["data"] >= td:
                imp.append({"data": v["data"], "materia": m["materia"], "tipo": "verifica" if v["regola"] == "P1" else "compito",
                            "priorita": v["priorita"], "affidabilita": v["affidabilita"], "motivo": v["motivo"], "oggetto": v["oggetto"]})
    imp.sort(key=lambda x: (x["data"], x["materia"], x["oggetto"]))
    nd = [m["materia"] for m in m9["materie"] if m["priorita"] == "non_determinabile"]
    blocks.append(_blk("prossimi_impegni", "Verifiche e scadenze note", "dati" if imp else "vuoto_valido", imp,
                       _prov("cloud.school.priority/1", m9["provenance"]["priority_version"], [x["oggetto"] for x in imp]),
                       nota=("materie non determinabili (fonte non letta): %s" % ", ".join(nd)) if nd else None))
    blocks.append(_blk("azioni_disponibili", "Azioni disponibili", "dati" if m10["sintesi"]["azioni"] else "vuoto_valido",
                       {"totale": m10["sintesi"]["azioni"], "per_azione": m10["sintesi"]["per_azione"],
                        "per_eseguibilita": m10["sintesi"]["per_eseguibilita"]},
                       _prov("cloud.school.action/1", m10["provenance"]["action_version"])))
    unc = [{"materia": m["materia"], "affidabilita": m["affidabilita"], "stato_decisione": m["stato_decisione"]}
           for m in m9["materie"] if m["affidabilita"] in ("bassa", "non_determinabile") or m["stato_decisione"] != "completa"]
    blocks.append(_blk("incertezze", "Dati incerti", "dati" if unc else "vuoto_valido",
                       {"materie": unc, "medie_voti": "politiche M14 con corrispondenza ClasseViva non verificata",
                        "dettaglio": "pagina 'qualita_dati'"}, _prov("cloud.school.priority/1", m9["provenance"]["priority_version"])))
    rows, initial, counts = _news(S.reg, S.now)
    blocks.append(_blk("novita", "Cosa e' cambiato (ultimi %d giorni)" % NEWS_DAYS, "dati" if rows else "vuoto_valido",
                       {"conteggi": counts, "ultimi": rows[:NEWS_SHOWN], "mostrati": min(len(rows), NEWS_SHOWN), "totale": len(rows),
                        "prima_lettura_esclusa": initial},
                       _prov("registry.events (M1/M3)", R.SCHEMA_VERSION),
                       nota="gli eventi della prima acquisizione di un kind non sono novita'" if initial else None))
    study = collections.Counter(x["materia"] for x in [dict(a, materia=m["materia"]) for m in m10["materie"] for a in m["azioni"]]
                                if x["azione"] in ("controllare_verifica", "controllare_compito"))
    blocks.append(_blk("studio", "Dove entrare per studiare", "dati" if study else "vuoto_valido",
                       {"materie_con_obiettivi": dict(sorted(study.items())), "dettaglio": "pagina 'studio'"},
                       _prov("cloud.school.action/1", m10["provenance"]["action_version"])))
    stale = sorted(k for k, e in fresh["per_kind"].items() if e["stale"])
    blocks.append(_blk("qualita_sintesi", "Qualita' dei dati", "dati", {"qualcosa_stale": fresh["qualcosa_stale"], "kind_stale_o_non_letti": stale,
                       "voti": m14["voti"]["stato_fonte"]["stato"], "dettaglio": "pagina 'qualita_dati'"},
                       _prov("registry.freshness (M3)", R.SCHEMA_VERSION)))
    return blocks, []


def _page_attenzione(S):
    m9, m10 = S.m9(), S.m10()
    acts = {}
    for m in m10["materie"]:
        for a in m["azioni"]:
            acts.setdefault(a["voce_m9"], []).append({"azione": a["azione"], "target": a["target"], "eseguibilita": a["eseguibilita"],
                                                      "affidabilita": a["affidabilita"], "gaps": [g["codice"] for g in a["gaps"]]})
    blocks = []
    for m in m9["materie"]:
        voci = [{"regola": v["regola"], "priorita": v["priorita"], "affidabilita": v["affidabilita"], "data": v["data"], "motivo": v["motivo"],
                 "oggetto": v["oggetto"], "azioni": acts.get(v["id"], [])} for v in m["voci"]]
        st = "dati" if voci else ("non_disponibile" if m["priorita"] == "non_determinabile" else "vuoto_valido")
        blocks.append(_blk("materia_%s" % m["materia"], m["nome_ufficiale"] or m["materia"], st,
                           {"priorita": m["priorita"], "stato_decisione": m["stato_decisione"], "voci": voci,
                            "regole_non_valutate": [r["regola"] for r in m["regole_non_valutate"]]},
                           _prov("cloud.school.priority/1 + cloud.school.action/1", m9["provenance"]["priority_version"],
                                 [v["oggetto"] for v in m["voci"]]), m["affidabilita"]))
    return blocks, []


def _subject_card(S, k):
    d8 = S.m8(k)
    m9 = next(m for m in S.m9()["materie"] if m["materia"] == k)
    m14 = next(m for m in S.m14()["voti"]["per_materia"] if m["materia"] == k)
    m15 = S.m15(k)
    s1 = _sig(d8, "S1")
    medie = m14["medie"] or {}
    prim = medie.get(RC.PRIMARY_POLICY) or {}
    return {"materia": k, "nome_ufficiale": d8["materia"]["nome_ufficiale"], "priorita": m9["priorita"], "affidabilita": m9["affidabilita"],
            "prossima_verifica": s1["valore"] if (s1["valore"] or {}).get("stato") == "presente" else None,
            "verifica_stato": (s1["valore"] or {}).get("stato") if s1["valore"] else "non_determinabile",
            "lezioni_anno": d8["fatti"]["lezioni"]["n"], "comunicazioni_anno": d8["fatti"]["comunicazioni"]["n"],
            "materiali_identificati": m15["sintesi"]["totale"],
            "voti": {"stato": m14["stato"], "n": m14["n_totali"], "media_politica": RC.PRIMARY_POLICY,
                     "media_valore": prim.get("valore"), "corrispondenza_classeviva": "non_verificata"},
            "assenze": "non attribuite alle materie (pagina 'assenze')"}


def _page_materie(S):
    cards = [_subject_card(S, k) for k in SC.AGENT_SUBJECTS]
    return [_blk("materie", "Materie", "dati", cards, _prov("M8+M9+M14+M15", dashboard_version()))], []


def _page_materia(S, k):
    d8, m14, m15, m12 = S.m8(k), S.m14(), S.m15(k), S.m12(k)
    m9 = next(m for m in S.m9()["materie"] if m["materia"] == k)
    m10 = next(m for m in S.m10()["materie"] if m["materia"] == k)
    pv = lambda c, v, u=None: _prov(c, v, u)                                                    # noqa: E731
    s1, s2, s3, s4, s7 = (_sig(d8, x) for x in ("S1", "S2", "S3", "S4", "S7"))
    blocks = [_blk("scheda", "Scheda", "dati", _subject_card(S, k), pv("M8+M9+M14+M15", dashboard_version()))]
    blocks.append(_blk("priorita", "Priorita' e azioni", "dati" if m9["voci"] else ("non_disponibile" if m9["priorita"] == "non_determinabile" else "vuoto_valido"),
                       {"priorita": m9["priorita"], "voci": [{"regola": v["regola"], "priorita": v["priorita"], "data": v["data"],
                                                             "motivo": v["motivo"], "affidabilita": v["affidabilita"]} for v in m9["voci"]],
                        "azioni": [{"azione": a["azione"], "target": a["target"], "eseguibilita": a["eseguibilita"],
                                    "affidabilita": a["affidabilita"]} for a in m10["azioni"]]},
                       pv("cloud.school.priority/1 + cloud.school.action/1", S.m9()["provenance"]["priority_version"],
                          [v["oggetto"] for v in m9["voci"]]), m9["affidabilita"]))

    def items(sig, keys):
        return [{k_: e.get(k_) for k_ in keys} for e in (sig.get("evidenze_dettaglio") or [])]

    def sig_block(bid, titolo, sig, keys, empty_note=None):
        if sig["valore"] is None:
            return _blk(bid, titolo, "non_letto", None, pv("cloud.school.status/1", d8["provenance"]["status_version"]), sig["affidabilita"])
        it = items(sig, keys)
        return _blk(bid, titolo, "dati" if it else "vuoto_valido", it,
                    pv("cloud.school.status/1", d8["provenance"]["status_version"], [x.get("uid") for x in it]), sig["affidabilita"], empty_note)
    blocks.append(sig_block("verifiche", "Verifiche (anno operativo)", s1, ("uid", "data", "testo", "origine")))
    blocks.append(sig_block("compiti_aperti", "Compiti aperti", s2, ("uid", "data", "testo", "origine", "stato_operativo", "scadenza")))
    blocks.append(sig_block("compiti_scaduti", "Compiti scaduti non consegnati secondo Classroom (14 giorni)", s3,
                            ("uid", "data", "testo", "stato_operativo", "scadenza")))
    blocks.append(sig_block("lezioni", "Lezioni (anno operativo)", s4, ("uid", "data", "ora", "testo")))
    blocks.append(sig_block("comunicazioni", "Comunicazioni della materia", s7, ("uid", "data", "testo", "classe_principale")))
    refs = m15["riferimenti"]
    blocks.append(_blk("materiali", "Materiali identificati (non acquisiti)", "dati" if refs else "vuoto_valido",
                       {"sintesi": m15["sintesi"], "riferimenti": refs}, pv("cloud.school.materials_catalog/1", m15["provenance"]["catalog_version"],
                                                                              [r["id"] for r in refs])))
    vm = next(m for m in m14["voti"]["per_materia"] if m["materia"] == k)
    blocks.append(_blk("voti", "Voti", {"fonte_non_letta": "non_letto", "nessun_voto": "vuoto_valido"}.get(vm["stato"], "dati"), vm,
                       pv("cloud.school.records/1", m14["provenance"]["records_version"]),
                       nota="medie: politiche nominate, corrispondenza con ClasseViva non verificata"))
    blocks.append(_blk("assenze", "Assenze", "non_disponibile", None, nota="le assenze non hanno materia nella fonte: pagina 'assenze'"))
    blocks.append(_blk("studio", "Studio", "dati", {"disponibilita_modalita": m12["disponibilita"],
                                                   "strumenti": ["school_study_assistant_v1", "school_study_session_v1"]},
                       pv("cloud.school.study_assistant/1", m12["provenance"]["study_assistant_version"])))
    blocks += [_inactive("pianificazione"), _inactive("profilo"), _inactive("dichiarazioni")]
    blocks.append(_blk("qualita", "Qualita' e provenienza", "dati", {"copertura": d8["copertura"], "gap": d8["gap"],
                       "ambiguita": [{k_: a[k_] for k_ in ("codice", "sezione", "n", "tocca_segnali")} for a in d8["ambiguita"]],
                       "freschezza": d8["freshness"]["per_sezione"]}, pv("cloud.school.status/1", d8["provenance"]["status_version"])))
    charts = _vote_charts(m14, k)
    return blocks, charts


def _vote_charts(m14, k=None):
    vp = _prov("cloud.school.records/1", m14["provenance"]["records_version"])
    pol = m14["politiche"]["elenco"][RC.PRIMARY_POLICY]["formula"]
    rows = [m for m in m14["voti"]["per_materia"] if k is None or m["materia"] == k]
    charts = []
    series = [{"nome": m["materia"], "punti": [{"x": p["data"], "y": p["valore"], "uid": p["uid"]} for p in (m["serie"] or [])]} for m in rows]
    charts.append(_chart("andamento_voti", "Andamento voti", "linea", "data", "decimalValue", series, vp))
    prog = [{"nome": m["materia"], "punti": [{"x": p["data"], "y": p["progressiva"][RC.PRIMARY_POLICY], "uid": p["uid"]}
                                             for p in (m["serie"] or [])]} for m in rows]
    charts.append(_chart("andamento_media", "Andamento media (%s)" % RC.PRIMARY_POLICY, "linea", "data", "media", prog, vp, pol, False))
    bars = [{"nome": RC.PRIMARY_POLICY, "punti": [{"x": m["materia"], "y": ((m["medie"] or {}).get(RC.PRIMARY_POLICY) or {}).get("valore"),
                                                   "uid": None} for m in rows]}]
    charts.append(_chart("media_per_materia", "Media per materia (%s)" % RC.PRIMARY_POLICY, "barre", "materia", "media", bars, vp, pol, False))
    dist = [{"nome": m["materia"], "punti": [{"x": b, "y": c, "uid": None} for b, c in (m["distribuzione"] or {}).get("conteggi", {}).items()]}
            for m in rows if m["distribuzione"]]
    charts.append(_chart("distribuzione_voti", "Distribuzione voti (voti inclusi)", "barre_impilate", "fascia", "numero", dist, vp))
    if k is None:
        c = m14["voti"]["complessivo"]
        v = ((c["medie"] or {}).get(RC.PRIMARY_POLICY) or {}).get("valore")
        charts.append(_chart("media_complessiva", "Media complessiva (%s)" % RC.PRIMARY_POLICY, "indicatore", None, "media",
                             [{"nome": "complessiva", "punti": [{"x": None, "y": v, "uid": None}]}], vp, pol, False))
    return [c for c in charts if c]


def _page_verifiche(S):
    rows, hints, unread = [], [], []
    for k in SC.AGENT_SUBJECTS:
        d8 = S.m8(k)
        s1 = _sig(d8, "S1")
        if s1["valore"] is None:
            unread.append(k)
        val = s1["valore"] or {}
        target = {"presente": rows, "assente_con_indizi_non_datati": hints}.get(val.get("stato"))
        for e in (s1.get("evidenze_dettaglio") or []) if target is not None else []:
            target.append({"materia": k, "uid": e["uid"], "data": e.get("data"), "testo": e.get("testo"), "origine": e.get("origine"),
                           "affidabilita": s1["affidabilita"]})
    rows.sort(key=lambda r: (r["data"] or "", r["materia"], r["uid"]))
    vp = _prov("cloud.school.status/1", ST.status_version(), [r["uid"] for r in rows])
    empty = "non_letto" if len(unread) == len(SC.AGENT_SUBJECTS) else "vuoto_valido"
    nota = ("verifiche non determinabili (fonte non letta) per: %s" % ", ".join(unread)) if unread else None
    blocks = [_blk("verifiche_future", "Verifiche future note", "dati" if rows else empty, rows, vp, nota=nota),
              _blk("indizi_non_datati", "Indizi di verifica senza data (da verificare)", "dati" if hints else empty, hints,
                   _prov("cloud.school.status/1", ST.status_version(), [r["uid"] for r in hints]))]
    cal = _chart("calendario_verifiche", "Calendario verifiche", "calendario", "data", "materia",
                 [{"nome": k, "punti": [{"x": r["data"], "y": k, "uid": r["uid"]} for r in rows if r["materia"] == k]} for k in SC.AGENT_SUBJECTS], vp)
    return blocks, [c for c in [cal] if c]


def _page_compiti(S):
    aperti, scaduti, states = [], [], set()
    for k in SC.AGENT_SUBJECTS:
        d8 = S.m8(k)
        for sid, target in (("S2", aperti), ("S3", scaduti)):
            s = _sig(d8, sid)
            states.add("non_letto" if s["valore"] is None else "ok")
            for e in s.get("evidenze_dettaglio") or []:
                target.append({"materia": k, "uid": e["uid"], "scadenza": e.get("data"), "testo": e.get("testo"), "origine": e.get("origine"),
                               "stato_operativo": e.get("stato_operativo"), "affidabilita": s["affidabilita"]})
    for lst in (aperti, scaduti):
        lst.sort(key=lambda r: (r["scadenza"] or "", r["materia"], r["uid"]))
    unread = "non_letto" in states
    st = lambda lst: "dati" if lst else ("non_letto" if unread else "vuoto_valido")     # noqa: E731
    return [_blk("compiti_aperti", "Compiti aperti", st(aperti), aperti, _prov("cloud.school.status/1", ST.status_version(), [r["uid"] for r in aperti])),
            _blk("compiti_scaduti", "Scaduti non consegnati secondo Classroom (14 giorni)", st(scaduti), scaduti,
                 _prov("cloud.school.status/1", ST.status_version(), [r["uid"] for r in scaduti]),
                 nota="Classroom non dice se la consegna era richiesta")], []


def _page_voti(S):
    m14 = S.m14()
    vp = _prov("cloud.school.records/1", m14["provenance"]["records_version"], [g["uid"] for g in (m14["voti"]["elenco"] or [])])
    src = _src_state(m14["voti"]["stato_fonte"]["stato"])
    c = m14["voti"]["complessivo"]
    blocks = [_blk("complessivo", "Complessivo", src if src != "dati" else ("vuoto_valido" if c["stato"] == "nessun_voto" else "dati"), c, vp,
                   nota="medie = politiche nominate (%s primaria); corrispondenza con ClasseViva non verificata" % RC.PRIMARY_POLICY),
              _blk("per_materia", "Per materia", src, [{"materia": m["materia"], "stato": m["stato"], "n": m["n_totali"], "inclusi": m["n_inclusi"],
                                                       "medie": {p: (m["medie"] or {}).get(p, {}).get("valore") if m["medie"] else None
                                                                 for p in RC.POLICIES}} for m in m14["voti"]["per_materia"]], vp),
              _blk("per_periodo", "Per periodo", src if m14["voti"]["per_periodo"] else ("vuoto_valido" if src == "dati" or src == "vuoto_valido" else src),
                   m14["voti"]["per_periodo"], vp),
              _blk("politiche", "Politiche di media", "dati", m14["politiche"], vp)]
    return blocks, _vote_charts(m14)


def _page_assenze(S):
    m14 = S.m14()
    a = m14["assenze"]
    src = _src_state(a["stato_fonte"]["stato"])
    vp = _prov("cloud.school.records/1", m14["provenance"]["records_version"], [e["uid"] for e in (a["eventi"] or [])])
    st = "non_letto" if src == "non_letto" else ("dati" if a["eventi"] else "vuoto_valido")
    blocks = [_blk("conteggi", "Assenze e ritardi (anno operativo)", st, a["conteggi"], vp),
              _blk("eventi", "Eventi", st, a["eventi"], vp, nota=a.get("nota"))]
    charts = []
    if a["conteggi"]:
        tipi = sorted({t for c in a["conteggi"]["per_mese"].values() for t in c})
        charts.append(_chart("assenze_per_mese", "Assenze e ritardi per mese", "barre_impilate", "mese", "numero",
                             [{"nome": t, "punti": [{"x": m, "y": c.get(t, 0), "uid": None} for m, c in a["conteggi"]["per_mese"].items()]}
                              for t in tipi], vp))
    return blocks, [c for c in charts if c]


def _page_studio(S):
    blocks = []
    for k in SC.AGENT_SUBJECTS:
        m12 = S.m12(k)
        objs = [{"uid": c["uid"], "data": c["campi"].get("data"), "testo": c["campi"].get("testo_dichiarato")}
                for m in m12["modalita"] if m["modalita"] == "quadro_obiettivo" for c in m["contenuti"]]
        blocks.append(_blk("studio_%s" % k, k, "dati", {"obiettivi": objs, "disponibilita_modalita": m12["disponibilita"],
                                                        "informazione_insufficiente": [i["codice"] for i in m12["informazione_insufficiente"]]},
                           _prov("cloud.school.study_assistant/1", m12["provenance"]["study_assistant_version"], [o["uid"] for o in objs])))
    blocks += [_blk("sessioni", "Sessioni di studio", "dati", {"strumento": "school_study_session_v1",
                    "modalita": ["spiega", "interroga", "esercitati", "correggi", "ripassa", "prepara_obiettivo"],
                    "nota": "lavorano sul contenuto fornito o richiamato da M12; conoscenza generale sempre etichettata"},
                    _prov("cloud.school.study_session/1", SS.study_session_version())), _inactive("store_materiali"), _inactive("pianificazione")]
    return blocks, []


def _counts(sintesi):
    return {k: v for k, v in sintesi.items() if k not in ("pagine", "mostrati")}


def _page_materiali(S):
    g = S.m15(None, limite=1)
    per = []
    for k in SC.AGENT_SUBJECTS:
        s = S.m15(k, limite=1)["sintesi"]
        per.append({"materia": k, "totale": s["totale"], "per_attribuzione": s["per_attribuzione"], "con_associazioni_m5": s["con_associazioni_m5"]})
    vp = _prov("cloud.school.materials_catalog/1", g["provenance"]["catalog_version"])
    linked = []
    for m in S.m9()["materie"]:
        for v in m["voci"]:
            if v["regola"] == "P1":
                r = S.m15(None, obiettivo=v["oggetto"], limite=MC.LIMIT_MAX)
                linked.append({"materia": m["materia"], "obiettivo": v["oggetto"], "data": v["data"],
                               "materiali": [{"id": x["id"], "titolo": x["titolo"], "attribuzione": x["attribuzione"]["stato"],
                                              "associazioni": x["associazioni_m5"]} for x in r["riferimenti"]]})
    blocks = [_blk("sintesi", "Materiali (anno operativo)", "dati" if g["sintesi"]["totale"] else "vuoto_valido", _counts(g["sintesi"]), vp),
              _blk("per_materia", "Per materia", "dati", per, vp),
              _blk("collegati_agli_obiettivi", "Collegati alle verifiche (via M5)", "dati" if linked else "vuoto_valido", linked, vp),
              _blk("disponibili", "Disponibili in locale", "non_attivo", nota=INACTIVE["store_materiali"])]
    chart = _chart("materiali_per_materia", "Materiali identificati per materia", "barre_impilate", "materia", "numero",
                   [{"nome": st, "punti": [{"x": p["materia"], "y": p["per_attribuzione"].get(st, 0), "uid": None} for p in per]}
                    for st in ("determinata", "possibile")], vp)
    return blocks, [c for c in [chart] if c]


def _hist_state(m14):
    srcs = {_src_state(m14[x]["stato_fonte"]["stato"]) for x in ("voti", "assenze")}
    return "non_letto" if srcs == {"non_letto"} else "vuoto_valido"


def _page_storico(S):
    m14 = S.m14()
    st = S.m15(None, perimetro="storico", limite=1)
    per = [{"materia": k, "anni": S.m8(k)["storico"]["anni"]} for k in SC.AGENT_SUBJECTS]
    return [_blk("contesto_per_materia", "Elementi di anni precedenti (conteggi)", "dati" if any(p["anni"] for p in per) else "vuoto_valido", per,
                 _prov("cloud.school.status/1", ST.status_version()), nota="separato dall'anno operativo: non usato in priorita' o azioni"),
            _blk("voti_assenze", "Voti e assenze di anni precedenti", "dati" if m14["storico"]["conteggi"] else _hist_state(m14),
                 m14["storico"]["conteggi"], _prov("cloud.school.records/1", m14["provenance"]["records_version"])),
            _blk("materiali", "Materiali di anni precedenti", "dati" if st["sintesi"]["totale"] else "vuoto_valido", _counts(st["sintesi"]),
                 _prov("cloud.school.materials_catalog/1", st["provenance"]["catalog_version"]))], []


def _page_qualita(S):
    fresh = S.fresh()
    kinds = {k: {"motivo": e["motivo"], "stale": e["stale"], "eta_ore": e.get("eta_ore"), "legacy": bool((e.get("ultimo_run") or {}).get("legacy"))}
             for k, e in sorted(fresh["per_kind"].items())}
    per = []
    for k in SC.AGENT_SUBJECTS:
        d8 = S.m8(k)
        m9 = next(m for m in S.m9()["materie"] if m["materia"] == k)
        per.append({"materia": k, "copertura": {c: (v.get("stato") if isinstance(v, dict) else v) for c, v in d8["copertura"].items() if c != "classroom"},
                    "affidabilita_priorita": m9["affidabilita"], "regole_non_valutate": [r["regola"] for r in m9["regole_non_valutate"]],
                    "gap": [g["codice"] for g in d8["gap"]], "gap_m7": sorted({g["codice"] for g in d8["gap_m7"]}),
                    "ambiguita": sorted({a["codice"] for a in d8["ambiguita"]})})
    m14, m15 = S.m14(), S.m15(None, limite=1)
    return [_blk("freschezza", "Freschezza delle fonti", "dati", {"soglia_ore": fresh["soglia_ore"], "per_kind": kinds,
                 "qualcosa_stale": fresh["qualcosa_stale"]}, _prov("registry.freshness (M3)", R.SCHEMA_VERSION),
                 nota="'legacy' = run precedenti a M3: nota di provenienza, non un declassamento"),
            _blk("per_materia", "Copertura, lacune e ambiguita' per materia", "dati", per, _prov("cloud.school.status/1", ST.status_version())),
            _blk("voti_assenze", "Voti e assenze", "dati", {"gap": [g["codice"] for g in m14["gap"]],
                 "voti": m14["voti"]["stato_fonte"]["stato"], "assenze": m14["assenze"]["stato_fonte"]["stato"]},
                 _prov("cloud.school.records/1", m14["provenance"]["records_version"])),
            _blk("materiali", "Materiali", "dati", {"gap": [g["codice"] for g in m15["gap"]], "per_attribuzione": m15["sintesi"]["per_attribuzione"]},
                 _prov("cloud.school.materials_catalog/1", m15["provenance"]["catalog_version"])),
            _inactive("dichiarazioni")], []


# ------------------------------------------------------------------------------------------------ costruzione
def build(reg, now_dt, manual=None, pagina="home", materia=None, soglia_ore=48, *, subjects, source_path=None, _sources=None):
    """cloud.school.dashboard/1: UNA pagina della dashboard. Sola lettura; `now_dt` letto UNA volta dal chiamante.
    `_sources`: contratti sorgente gia' calcolati con gli STESSI registro, istante, manuale, materie e soglia (PageSession)."""
    if pagina not in PAGES:
        raise ValueError("pagina non valida: %r (pagine: %s)" % (pagina, ", ".join(PAGES)))
    key = None
    if pagina == "materia":
        if not (isinstance(materia, str) and materia.strip()):
            raise ValueError("la pagina 'materia' richiede una materia")
        key = SC.resolve_subject(materia.strip(), subjects)
    elif materia is not None:
        raise ValueError("materia ammessa solo con pagina='materia'")
    if isinstance(soglia_ore, bool) or not isinstance(soglia_ore, (int, float)) or soglia_ore <= 0:
        raise ValueError("soglia_ore non valida: %r" % (soglia_ore,))
    now_dt = D.parse_now(now_dt)
    if _sources is None:
        S = _Sources(reg, now_dt, manual, subjects, soglia_ore, source_path)
    else:
        S = _sources
        if (S.reg, S.now, S.manual, S.subjects, S.soglia, S.path) != (reg, now_dt, manual, subjects, soglia_ore, source_path):
            raise ValueError("fonti condivise calcolate con parametri diversi: non riutilizzabili")
        S._used = set()
    fn = {"home": _page_home, "attenzione": _page_attenzione, "materie": _page_materie, "verifiche": _page_verifiche,
          "compiti": _page_compiti, "voti": _page_voti, "assenze": _page_assenze, "studio": _page_studio, "materiali": _page_materiali,
          "storico": _page_storico, "qualita_dati": _page_qualita}
    blocks, charts = _page_materia(S, key) if pagina == "materia" else fn[pagina](S)
    used = {}
    for k_, v in sorted(((k, S._c[k]) for k in S._used if k in S._c), key=lambda kv: str(kv[0])):
        if isinstance(v, dict) and "schema" in v:
            used[v["schema"]] = (v.get("provenance") or {})
    versions = {s: {k_: p[k_] for k_ in sorted(p) if k_.endswith("_version")} for s, p in sorted(used.items())}
    td = now_dt.astimezone(D._zone()).date().isoformat()
    return {
        "schema": SCHEMA,
        "natura": NATURA,
        "pagina": pagina,
        "materia": key,
        "riferimento_temporale": {"now": now_dt.isoformat(), "data_riferimento": td},
        "navigazione": [{"pagina": p, "richiede_materia": p == "materia"} for p in PAGES],
        "blocchi": blocks,
        "grafici": charts,
        "grafici_assenti": "un grafico compare solo con almeno un punto reale" if not charts else None,
        "stati_blocco": list(BLOCK_STATES),
        "provenance": {"schema": SCHEMA, "dashboard_version": dashboard_version(), "contratti_usati": versions, "sorgente": source_path},
        "regole_agente": RULES_FOR_AGENTS,
    }


class PageSession:
    """Piu' pagine dallo STESSO istante, dalla STESSA copia in memoria del Registry e dallo STESSO manuale: i contratti
    sorgente si calcolano una volta e si condividono fra le pagine. Vale solo finche' la sessione e' aperta (nessuna cache
    oltre): ogni pagina e' identica a quella di build_from_path, provenienza compresa (solo i contratti che usa)."""

    def __init__(self, path, now, manual_path=None, soglia_ore=48, *, subjects):
        self.reg = R.Registry.from_snapshot(path)
        try:
            self.manual = SC.load_manual(manual_path)["dati"]
            self.now = D.parse_now(now)
        except Exception:
            self.reg.close()
            raise
        self.path, self.subjects, self.soglia = os.path.abspath(path), subjects, soglia_ore
        self._sources = _Sources(self.reg, self.now, self.manual, subjects, soglia_ore, self.path)
        self._layers = {}                        # strati di M7 indipendenti dalla materia (school_context4.shared_layers)

    def build(self, pagina, materia=None):
        with C4.shared_layers(self._layers):
            return build(self.reg, self.now, manual=self.manual, pagina=pagina, materia=materia, soglia_ore=self.soglia,
                         subjects=self.subjects, source_path=self.path, _sources=self._sources)

    def close(self):
        self.reg.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def build_from_path(path, now, manual_path=None, **kw):
    """Registro aperto come COPIA in memoria (Registry.from_snapshot, immutable=1): nessun byte del file viene scritto."""
    reg = R.Registry.from_snapshot(path)
    try:
        return build(reg, now, manual=SC.load_manual(manual_path)["dati"], source_path=os.path.abspath(path), **kw)
    finally:
        reg.close()
