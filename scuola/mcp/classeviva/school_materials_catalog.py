"""CATALOGO MATERIALI (M15 v1): `cloud.school.materials_catalog/1`. Modulo PURO e SOLA LETTURA.

Identificazione e associazione dei materiali GIA' citati dalle fonti, senza acquisirli: un riferimento a un file non e' il file, e un
compito con allegato non dice che quel materiale sia necessario o sufficiente.

Fonti (sola lettura, nessuna nuova logica di dominio):
  * Registry, Classroom: allegati di `material`, `course_work`, `announcement` (normalizzazione esistente `cls_registry.normalized`
    -> `materials`: tipo, id, titolo, url, share_mode);
  * Registry, ClasseViva: allegati delle circolari (`notice.attachments`: fileName, attachNum) -> ambito generale, mai di una materia;
  * corso -> materia: SOLO conferme di `school_manual.json` (`cls_subjects.confirmed_map`) e regola PER ELEMENTO di M5
    (`cvv_cls_links.item_subject`); proposte di corso (`cls_subjects.classify/proposed_for`) solo come `possibile`;
  * associazioni a obiettivi: SOLO collegamenti M5 esistenti (`cvv_cls_links.collect/compute`, contratto pubblico) con il loro stato.

Due assi SEMPRE separati per ogni riferimento:
  * `stato_materiale` (disponibilita'): identificato_non_acquisito | non_disponibile; `acquisito` e `fornito_utente` sono RISERVATI a
    M19 e non sono mai prodotti qui;
  * `attribuzione` (materia): determinata | possibile | non_determinabile | generale, con l'appartenenza al CORSO riportata a parte
    (la semantica per corso di M4/school_agent non diventa mai attribuzione per elemento). `ambiguo` = attribuzione non determinata.
Perimetro: anno operativo (anno scolastico di `now`) OPPURE storico, mai mescolati. Stesso input -> stesso JSON; nessun orologio;
nessuna scrittura; nessun download.
"""
from __future__ import annotations

import collections
import hashlib
import os

import cls_derive as D
import cls_registry as CL
import cls_subjects as CS
import cvv_client as cv
import cvv_cls_links as L
import cvv_registry as CR
import it_dates as ID
import registry as R
import school_common as CM
import school_context as SC
import school_context3 as C3

SCHEMA = "cloud.school.materials_catalog/1"
CATALOG_REV = 1

MATERIAL_STATES = ("identificato_non_acquisito", "non_disponibile")
RESERVED_STATES = ("acquisito", "fornito_utente")                    # M19: mai prodotti dal catalogo
ATTRIBUTION_STATES = ("determinata", "possibile", "non_determinabile", "generale")
RESOURCE_TYPES = ("drive_file", "youtube", "link", "form", "gem", "notebook", "allegato_classeviva", "unknown")
PARENT_KINDS = ("material", "course_work", "announcement", "notice")
PERIMETERS = ("operativo", "storico")
LIMIT_DEFAULT, LIMIT_MAX = 50, 200
TEXT_CAP = 160

FORBIDDEN_WORDS = ("necessario", "necessaria", "sufficiente", "indispensabile", "obbligatorio", "programma della verifica",
                   "da studiare", "devi studiare", "sei pronto", "sei indietro")
NATURA = ("catalogo DERIVATO e in SOLA LETTURA dei materiali citati dalle fonti scolastiche: identificazione, attribuzione e "
          "associazione con il loro stato; nessuna acquisizione, nessun contenuto letto, nessun giudizio su utilita' o completezza")
RULES_FOR_AGENTS = [
    "Un riferimento nel catalogo e' un materiale IDENTIFICATO, non acquisito: il suo contenuto non e' stato letto.",
    "Non dire che un materiale serve, basta o copre il programma: il catalogo non lo sa.",
    "L'attribuzione per elemento (determinata/possibile/non_determinabile) e l'appartenenza al corso sono cose diverse: riportale separate.",
    "Un'associazione a un obiettivo esiste solo tramite un collegamento M5 e ne conserva lo stato (mai certo).",
    "Gli allegati delle circolari sono generali: non attribuirli a una materia.",
    "Il perimetro operativo e lo storico sono separati: non mescolarli.",
]


def catalog_version():
    return cv.interp_version(CATALOG_REV, list(MATERIAL_STATES), list(RESERVED_STATES), list(ATTRIBUTION_STATES), list(RESOURCE_TYPES),
                             list(PARENT_KINDS), list(PERIMETERS), LIMIT_DEFAULT, LIMIT_MAX, TEXT_CAP, list(FORBIDDEN_WORDS),
                             CL.interp_version(), L.link_version())


# ------------------------------------------------------------------------------------------------ helper
def _sy(day):
    try:
        return ID.school_year_label(str(day)[:10]) if day else None
    except ValueError:
        return None


def _rid(parent_uid, typ, ident, idx):
    """Id deterministico del riferimento: padre + tipo + identificativo della risorsa (o posizione se manca)."""
    key = ident if ident else "pos%d" % idx
    return "%s#%s:%s" % (parent_uid, typ, key if typ != "link" else hashlib.sha1(key.encode("utf-8")).hexdigest()[:12])


def _kind_state(fresh, source, kind, reg):
    e = fresh["per_kind"].get("%s/%s" % (source, kind)) or {"motivo": "mai_sincronizzato"}
    n = len(reg.list_records(source=source, kind=kind))
    if e["motivo"] in ("mai_sincronizzato", "nessuna_lettura_riuscita"):
        return {"stato": "non_letto", "n_registro": n if n else None}
    return {"stato": "presente" if n else "vuoto_valido", "n_registro": n}


# ------------------------------------------------------------------------------------------------ raccolta
def collect(reg, subjects, manual):
    """Tutti i riferimenti a materiali (ogni anno), con padre, attribuzione, appartenenza al corso e associazioni M5."""
    confirmed = CS.confirmed_map(manual or {})
    courses = {}
    for f in reg.list_records(source=CL.SOURCE, kind="course"):
        c = reg.get_record(CL.SOURCE, "course", f["source_uid"])
        courses[str(c["raw"].get("id"))] = c
    report = CS.classify([c for c in courses.values() if c["source_status"] == R.PRESENT], subjects, manual or {})
    proposed = collections.defaultdict(set)
    for k in SC.AGENT_SUBJECTS:
        for cid in CS.proposed_for(report, k):
            if k not in confirmed.get(cid, ()):
                proposed[cid].add(k)
    topics = {}
    for f in reg.list_records(source=CL.SOURCE, kind="topic", status=R.PRESENT):
        raw = reg.get_record(CL.SOURCE, "topic", f["source_uid"])["raw"]
        topics[(str(raw.get("courseId")), raw.get("topicId"))] = raw.get("name")

    res5 = L.compute(L.collect(reg, subjects, manual))
    assoc = collections.defaultdict(list)
    for x in res5["collegamenti"] + res5["non_determinabili"]:
        assoc[x["b"]["uid"]].append({"tipo": "collegamento" if "link_id" in x else "non_determinabile",
                                     "id": x.get("link_id"), "relazione": x["relazione"], "stato": x["stato"],
                                     "regola": x["interpretazione"]["regola"],
                                     "con": {"uid": x["a"]["uid"], "tipo": x["a"]["tipo"], "data": x["a"]["dati"].get("data"),
                                             "materia": x["a"]["interpretazione"]["materia"]}})
    for x in res5["conflitti"]:
        assoc[x["elemento"]["uid"]].append({"tipo": "conflitto", "id": x["conflitto_id"], "relazione": x["relazione"],
                                           "stato": "conflitto", "regola": x["interpretazione"]["regola"],
                                           "con": [{"uid": c["lato"]["uid"], "tipo": c["lato"]["tipo"]} for c in x["candidati"]]})

    refs = []
    for kind in PARENT_KINDS[:3]:
        for f in reg.list_records(source=CL.SOURCE, kind=kind):
            full = reg.get_record(CL.SOURCE, kind, f["source_uid"])
            raw = full["raw"]
            n = CL.normalized(kind, raw)
            mats = n.get("materials") or []
            if not mats:
                continue
            cid = str(raw.get("courseId"))
            created = D._local_date(raw.get("creationTime"))
            day = created.isoformat() if created else None
            tname = topics.get((cid, raw.get("topicId"))) if raw.get("topicId") else None
            title = n.get("title") if kind != "announcement" else None
            text = (n.get("text") or n.get("description") or "") if kind == "announcement" else (n.get("description") or "")
            published = raw.get("state") == "PUBLISHED" and full["source_status"] == R.PRESENT
            if cid in confirmed:
                s = L.item_subject(confirmed[cid], tname)
                attr = {"stato": "determinata" if s["stato"] == "determinata" else "possibile", "materia": s["key"],
                        "candidati": s["candidati"], "criterio": s["criterio"], "regola": "M5 item_subject"}
            elif cid in proposed:
                attr = {"stato": "possibile", "materia": None, "candidati": sorted(proposed[cid]),
                        "criterio": "corso NON confermato, proposto per la materia (cls_subjects): mai determinata", "regola": "proposta corso"}
            else:
                attr = {"stato": "non_determinabile", "materia": None, "candidati": [],
                        "criterio": "corso non confermato e non proposto per nessuna materia del sistema", "regola": "nessuna"}
            course = courses.get(cid)
            parent = {"uid": n["uid"], "tipo": kind, "titolo": title, "testo": CM.cap_text(text, TEXT_CAP)[0] or None,
                      "testo_troncato": CM.cap_text(text, TEXT_CAP)[1], "data": day, "anno": _sy(day),
                      "hash": full["content_hash"], "registry_version": full["version"], "stato_fonte": raw.get("state"),
                      "stato_registro": full["source_status"],
                      "corso": {"id": cid, "nome": (course["raw"].get("name") if course else None),
                                "confermato_per": sorted(confirmed.get(cid, ())), "proposto_per": sorted(proposed.get(cid, ())),
                                "condiviso": len(confirmed.get(cid, ())) > 1},
                      "topic": tname}
            for i, m in enumerate(mats):
                typ = m.get("type") if m.get("type") in RESOURCE_TYPES else "unknown"
                ident = m.get("id") if typ != "link" else m.get("url")
                ok = published and (m.get("id") or m.get("url"))
                refs.append({"id": _rid(n["uid"], typ, ident, i), "sorgente": "classroom", "tipo_risorsa": typ,
                             "titolo": m.get("title"), "url": m.get("url"), "drive_file_id": m.get("id") if typ == "drive_file" else None,
                             "share_mode": m.get("share_mode"),
                             "stato_materiale": "identificato_non_acquisito" if ok else "non_disponibile",
                             "motivo_stato": None if ok else ("elemento non pubblicato o scomparso dalla fonte" if not published
                                                              else "riferimento senza identificativo"),
                             "attribuzione": attr, "padre": parent, "associazioni_m5": sorted(assoc.get(n["uid"], []),
                                                                                           key=lambda a: (a["tipo"], str(a["id"])))})

    for f in reg.list_records(source=CR.SOURCE, kind="notice"):
        full = reg.get_record(CR.SOURCE, "notice", f["source_uid"])
        raw = full["raw"]
        atts = [a for a in (raw.get("attachments") or []) if isinstance(a, dict)]
        if not atts:
            continue
        n = CR.normalized("notice", raw)
        day = str(n.get("date") or "")[:10] or None
        parent = {"uid": n["uid"], "tipo": "notice", "titolo": (raw.get("cntTitle") or "").strip() or None, "testo": None,
                  "testo_troncato": False, "data": day, "anno": _sy(day), "hash": full["content_hash"], "registry_version": full["version"],
                  "stato_fonte": raw.get("cntStatus"), "stato_registro": full["source_status"], "corso": None, "topic": None}
        for i, a in enumerate(atts):
            ok = full["source_status"] == R.PRESENT and a.get("attachNum") is not None
            refs.append({"id": _rid(n["uid"], "allegato_classeviva", str(a.get("attachNum")) if a.get("attachNum") is not None else None, i),
                         "sorgente": "classeviva", "tipo_risorsa": "allegato_classeviva", "titolo": a.get("fileName"), "url": None,
                         "drive_file_id": None, "share_mode": None,
                         "stato_materiale": "identificato_non_acquisito" if ok else "non_disponibile",
                         "motivo_stato": None if ok else "circolare scomparsa dalla fonte o allegato senza numero",
                         "attribuzione": {"stato": "generale", "materia": None, "candidati": [], "regola": "A1 M6",
                                          "criterio": "allegato di circolare: ambito generale, mai di una materia"},
                         "padre": parent, "associazioni_m5": []})
    refs.sort(key=lambda r: r["id"])
    return refs


def _compact(r):
    """Forma compatta: stessi fatti essenziali, padre ridotto (uid, tipo, titolo, data, hash a 12, corso), associazioni ridotte."""
    p, a = r["padre"], r["attribuzione"]
    out = {"id": r["id"], "tipo_risorsa": r["tipo_risorsa"], "titolo": r["titolo"], "url": r["url"], "stato_materiale": r["stato_materiale"],
           "attribuzione": {k: a[k] for k in ("stato", "materia", "candidati")},
           "padre": {"uid": p["uid"], "tipo": p["tipo"], "titolo": p["titolo"] or p["testo"], "data": p["data"], "hash": p["hash"][:12],
                     "corso": (p["corso"] or {}).get("nome")},
           "associazioni_m5": [{"relazione": x["relazione"], "stato": x["stato"],
                                "con": x["con"]["uid"] if isinstance(x["con"], dict) else [c["uid"] for c in x["con"]]}
                               for x in r["associazioni_m5"]]}
    if r["motivo_stato"]:
        out["motivo_stato"] = r["motivo_stato"]
    return out


# ------------------------------------------------------------------------------------------------ costruzione
def build(reg, now_dt, manual=None, materia=None, obiettivo=None, perimetro="operativo", limite=None, pagina=1, dettaglio=False,
          soglia_ore=48, *, subjects, source_path=None):
    """cloud.school.materials_catalog/1. Sola lettura; `now_dt` letto UNA volta dal chiamante."""
    key = None
    if materia is not None:
        if not (isinstance(materia, str) and materia.strip()):
            raise ValueError("materia non valida: %r" % (materia,))
        key = SC.resolve_subject(materia.strip(), subjects)
    if obiettivo is not None and not (isinstance(obiettivo, str) and obiettivo.strip()):
        raise ValueError("obiettivo non valido: %r (uid esatto)" % (obiettivo,))
    if perimetro not in PERIMETERS:
        raise ValueError("perimetro non valido: %r (%s)" % (perimetro, " | ".join(PERIMETERS)))
    if limite is None:
        limite = LIMIT_DEFAULT
    if isinstance(limite, bool) or not isinstance(limite, int) or limite < 1:
        raise ValueError("limite non valido: %r (intero >= 1)" % (limite,))
    if isinstance(pagina, bool) or not isinstance(pagina, int) or pagina < 1:
        raise ValueError("pagina non valida: %r (intero >= 1)" % (pagina,))
    if isinstance(soglia_ore, bool) or not isinstance(soglia_ore, (int, float)) or soglia_ore <= 0:
        raise ValueError("soglia_ore non valida: %r" % (soglia_ore,))
    if not isinstance(dettaglio, bool):
        raise ValueError("dettaglio non valido: %r (true | false)" % (dettaglio,))
    norm_lim = min(limite, LIMIT_MAX)
    now_dt = D.parse_now(now_dt)
    td = now_dt.astimezone(D._zone()).date().isoformat()
    year = _sy(td)
    fresh = reg.freshness(now_dt.isoformat(), stale_after_hours=soglia_ore)

    refs = collect(reg, subjects, manual)
    in_scope = [r for r in refs if (r["padre"]["anno"] == year) == (perimetro == "operativo")]
    other = collections.Counter(r["padre"]["anno"] for r in refs if (r["padre"]["anno"] == year) != (perimetro == "operativo"))
    if key is not None:
        sel = [r for r in in_scope if r["attribuzione"]["materia"] == key
               or (r["attribuzione"]["stato"] == "possibile" and key in r["attribuzione"]["candidati"])]
    else:
        sel = list(in_scope)
    if obiettivo is not None:
        ob = obiettivo.strip()
        sel = [r for r in sel if any((a["con"]["uid"] if isinstance(a["con"], dict) else None) == ob
                                     or (isinstance(a["con"], list) and ob in [c["uid"] for c in a["con"]]) for a in r["associazioni_m5"])]
    sel.sort(key=lambda r: (r["padre"]["data"] or "", r["id"]), reverse=True)

    def count(f):
        return dict(sorted(collections.Counter(f(r) for r in sel).items()))
    start = (pagina - 1) * norm_lim
    page = sel[start:start + norm_lim]
    gaps = []
    for (src, kind) in (("classroom", "material"), ("classroom", "course_work"), ("classroom", "announcement"), (CR.SOURCE, "notice")):
        st = _kind_state(fresh, src, kind, reg)
        if st["stato"] == "non_letto":
            gaps.append({"codice": "fonte_non_letta", "fonte": "%s/%s" % (src, kind), "messaggio": "fonte mai letta: catalogo incompleto"})
    if any(r["attribuzione"]["stato"] in ("possibile", "non_determinabile") for r in sel):
        gaps.append({"codice": "attribuzione_ambigua", "n": sum(1 for r in sel if r["attribuzione"]["stato"] in ("possibile", "non_determinabile")),
                     "messaggio": "riferimenti con attribuzione non determinata (corsi condivisi senza topic o corsi non confermati)"})
    gaps.append({"codice": "contenuto_non_acquisito", "messaggio": "nessun materiale e' acquisito a questo livello (M19): titoli e "
                                                                 "link sono riferimenti, non contenuti"})
    if any(r["tipo_risorsa"] == "drive_file" and not r["titolo"] for r in sel):
        gaps.append({"codice": "titolo_assente", "messaggio": "riferimenti Drive senza titolo nella fonte: non ricostruito"})
    sources = {"%s/%s" % (s, k): _kind_state(fresh, s, k, reg)
               for s, k in (("classroom", "material"), ("classroom", "course_work"), ("classroom", "announcement"), (CR.SOURCE, "notice"))}
    return {
        "schema": SCHEMA,
        "natura": NATURA,
        "riferimento_temporale": {"now": now_dt.isoformat(), "data_riferimento": td, "anno_operativo": year},
        "ambito": {"materia": key, "obiettivo": obiettivo.strip() if obiettivo else None, "perimetro": perimetro,
                   "limite": norm_lim, "limite_richiesto": limite, "pagina": pagina, "dettaglio": dettaglio},
        "sintesi": {"totale": len(sel), "pagine": (len(sel) + norm_lim - 1) // norm_lim if sel else 0, "mostrati": len(page),
                    "per_stato_materiale": count(lambda r: r["stato_materiale"]),
                    "per_attribuzione": count(lambda r: r["attribuzione"]["stato"]),
                    "per_tipo_risorsa": count(lambda r: r["tipo_risorsa"]),
                    "per_tipo_padre": count(lambda r: r["padre"]["tipo"]),
                    "con_associazioni_m5": sum(1 for r in sel if r["associazioni_m5"]),
                    "fuori_perimetro": dict(sorted((str(k), v) for k, v in other.items()))},
        "riferimenti": page if dettaglio else [_compact(r) for r in page],
        "fonti": sources,
        "freshness": {"classroom": C3.section_freshness(fresh, [("classroom", k) for k in ("material", "course_work", "announcement",
                                                                                       "course", "topic")])["stato"],
                      "circolari": C3.section_freshness(fresh, [(CR.SOURCE, "notice")])["stato"], "soglia_ore": fresh["soglia_ore"]},
        "gap": gaps,
        "stati": {"materiale": list(MATERIAL_STATES), "riservati_m19": list(RESERVED_STATES), "attribuzione": list(ATTRIBUTION_STATES),
                  "ambiguo": "attribuzione possibile o non_determinabile",
                  "nota": "realmente disponibile / fornito dall'utente arriveranno solo con M19; qui solo identificato o non disponibile"},
        "provenance": {"schema": SCHEMA, "catalog_version": catalog_version(), "classroom_interp_version": CL.interp_version(),
                       "link_version": L.link_version(), "schema_registro": R._version_of(reg.db), "sorgente": source_path},
        "regole_agente": RULES_FOR_AGENTS,
    }


def build_from_path(path, now, manual_path=None, **kw):
    """Registro aperto come COPIA in memoria (Registry.from_snapshot, immutable=1): nessun byte del file viene scritto."""
    reg = R.Registry.from_snapshot(path)
    try:
        return build(reg, now, manual=SC.load_manual(manual_path)["dati"], source_path=os.path.abspath(path), **kw)
    finally:
        reg.close()
