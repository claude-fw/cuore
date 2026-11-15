"""Adattatore ClasseViva -> registro locale (registry.py). Riusa i normalizzatori esistenti di cvv_client SENZA modificarli.

Percorso di un record:   RAW (record esatto dell'API)  ->  registro (identita', hash, versioni, stato)
                         ->  NORMALIZZATO (cvv_client.normalize_*).

'kind' del registro = tipo di record dell'API (agenda_event, grade, absence, notice, lesson), NON la lettura interpretata:
una "verifica" e' un agenda_event che la normalizzazione classifica come tale.
"""
from __future__ import annotations

import cvv_client as cv
import registry as reg

SOURCE = "classeviva"

# Per ogni kind: normalizzatore esistente e versione della logica interpretativa (None = non ancora versionata).
KINDS = {
    "agenda_event": {"normalize": cv.normalize_event, "interp_version": lambda: None},
    "grade": {"normalize": cv.normalize_grade, "interp_version": cv.grade_interp_version},
    "absence": {"normalize": cv.normalize_absence, "interp_version": cv.absence_interp_version},
    "notice": {"normalize": cv.normalize_notice, "interp_version": cv.notice_interp_version},
    # Fase 1: lezioni svolte. Opt-in: NON fa parte di ALL_KINDS (cvv_dryrun), quindi i default restano invariati.
    "lesson": {"normalize": cv.normalize_lesson, "interp_version": cv.lesson_interp_version},
}

# Campi volatili: cambiano senza che cambi il contenuto della fonte. Esclusi dall'hash, registrati come STATO.
# 'readStatus' = la comunicazione e' stata letta: succede quando la apri tu, non quando la scuola la modifica.
# (Decisione da confermare; il raw resta comunque integro e il valore e' conservato.)
VOLATILE = {"notice": ("readStatus",)}


def source_uid_of(uid):
    """'cvv:123' -> '123'; 'cvv:lesson:9' -> 'lesson:9'; gli ID di riserva 'sec:...' restano com'e'."""
    return uid[4:] if uid.startswith("cvv:") else uid


def legacy_uid_of(source_uid):
    """Inverso: l'ID storico (cvv:<id>)."""
    return source_uid if source_uid.startswith("sec:") else "cvv:" + source_uid


def to_items(kind, raws):
    """Record grezzi di ClasseViva -> item per il registro. L'ID viene dal normalizzatore esistente (compatibilita')."""
    normalize = KINDS[kind]["normalize"]
    items = []
    for raw in raws:
        n = normalize(raw)
        items.append({"source_uid": source_uid_of(n["uid"]), "legacy_uid": n["uid"],
                      "date": n.get("date") or None, "raw": raw})
    return items


# ---- Assenze: identita' stabile quando ClasseViva ri-emette l'evento con un evtId nuovo ----------------------------
# VERIFICATO sui dati reali: dopo la giustificazione ClasseViva ha restituito alcune assenze gia' registrate con
# un evtId NUOVO; evtDate, evtCode, evtHPos, evtValue, hoursAbsence, webJustifStatus identici, cambiati solo evtId,
# isJustified, justifReasonCode/Desc. L'identita' per evtId le trattava come 9 nuove assenze + 9 scomparse (doppioni).
# Regola (deterministica, mai fusioni ambigue): un'assenza letta il cui evtId non e' noto si riconcilia con un record
# registrato SOLO se (1) il record non e' stato ritrovato per ID in questa lettura, (2) hanno la stessa chiave naturale
# (evtDate, evtCode, evtHPos), (3) la corrispondenza e' unica in entrambi i sensi (un solo candidato registrato, una sola
# assenza letta con quella chiave), (4) il nuovo evtId e' numericamente successivo al vecchio (ClasseViva assegna ID
# crescenti). Allora il record conserva source_uid e legacy_uid (nessun doppione, nessun record nuovo): il RAW
# con il nuovo evtId diventa una nuova versione e l'evento 'reidentified' ne conserva la provenienza. Nelle letture
# successive il record si ritrova per l'evtId della versione corrente. Corrispondenze multiple o non plausibili =
# AMBIGUE: l'assenza letta non viene registrata e i candidati non vengono marcati come scomparsi (avviso). Un'assenza
# senza data o codice non e' identificabile per chiave: non viene registrata (avviso), nessun valore inventato.
ABSENCE_KEY_FIELDS = ("evtDate", "evtCode", "evtHPos")


def absence_key(raw):
    """Chiave naturale di un'assenza, o None se mancano data o codice."""
    d, c = raw.get("evtDate"), raw.get("evtCode")
    if not isinstance(d, str) or len(d) < 10 or not isinstance(c, str) or not c:
        return None
    return (d[:10], c, raw.get("evtHPos"))


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _key_text(k):
    return "%s/%s/ora %s" % k


def resolve_absences(registry, raws):
    """Record grezzi delle assenze -> (item, source_uid da non marcare scomparsi, rapporto). Sola lettura del registro."""
    existing = [registry.get_record(SOURCE, "absence", r["source_uid"])
                for r in registry.list_records(source=SOURCE, kind="absence")]
    by_suid = {rec["source_uid"]: rec for rec in existing}
    by_cur_id = {}
    for rec in existing:
        if rec["raw"].get("evtId") is not None:
            by_cur_id.setdefault(str(rec["raw"]["evtId"]), rec)
    report = {"riconciliate": [], "ambigue": [], "incomplete": []}
    items, matched, pending, hold = [], set(), [], set()

    def item(rec_or_none, raw, provenance=None):
        n = cv.normalize_absence(raw)
        suid = rec_or_none["source_uid"] if rec_or_none else source_uid_of(n["uid"])
        legacy = (rec_or_none["legacy_uid"] or n["uid"]) if rec_or_none else n["uid"]
        it = {"source_uid": suid, "legacy_uid": legacy, "date": n.get("date") or None, "raw": raw}
        if provenance:
            it["provenance"] = provenance
        return it

    for raw in raws:
        eid = raw.get("evtId")
        rec = by_cur_id.get(str(eid)) if eid is not None else None
        if rec is None:
            rec = by_suid.get(source_uid_of(cv.normalize_absence(raw)["uid"]))
        if rec is None:
            pending.append(raw)
        elif rec["source_uid"] in matched:      # due assenze lette sullo stesso record: mai fondere
            report["ambigue"].append({"evtId": eid, "motivo": "piu' assenze lette corrispondono allo stesso record registrato",
                                      "candidati": [rec["legacy_uid"] or rec["source_uid"]]})
            hold.add(rec["source_uid"])
        else:
            matched.add(rec["source_uid"])
            items.append(item(rec, raw))

    orphans, read_keys = {}, {}
    for rec in existing:
        k = absence_key(rec["raw"])
        if rec["source_uid"] not in matched and k is not None:
            orphans.setdefault(k, []).append(rec)
    for raw in pending:
        k = absence_key(raw)
        if k is not None:
            read_keys[k] = read_keys.get(k, 0) + 1
    for raw in pending:
        eid, k = raw.get("evtId"), absence_key(raw)
        if k is None:
            report["incomplete"].append({"evtId": eid, "motivo": "data o codice mancanti: identita' non verificabile"})
            continue
        cands = orphans.get(k, [])
        if not cands:
            items.append(item(None, raw))                       # vera nuova assenza
            continue
        old = cands[0]
        old_id, new_id = _int(old["raw"].get("evtId")), _int(eid)
        if len(cands) == 1 and read_keys[k] == 1 and old_id is not None and new_id is not None and new_id > old_id:
            changed = sorted(f for f in set(old["raw"]) | set(raw) if f != "evtId" and old["raw"].get(f) != raw.get(f))
            prov = {"regola": "assenza_rinumerata", "chiave": dict(zip(ABSENCE_KEY_FIELDS, k)),
                    "evtId_precedente": old["raw"].get("evtId"), "evtId_nuovo": eid,
                    "versione_precedente": old["version"], "campi_cambiati": changed}
            report["riconciliate"].append({"record": old["legacy_uid"] or old["source_uid"], "evtId_nuovo": eid,
                                           "chiave": _key_text(k), "campi_cambiati": changed})
            items.append(item(old, raw, prov))
            continue
        why = ("piu' assenze registrate con la stessa chiave" if len(cands) > 1 else
               "piu' assenze lette con la stessa chiave" if read_keys[k] > 1 else
               "evtId nuovo non successivo a quello registrato (corrispondenza non plausibile)")
        report["ambigue"].append({"evtId": eid, "chiave": _key_text(k), "motivo": why,
                                  "candidati": sorted(c["legacy_uid"] or c["source_uid"] for c in cands)})
        hold.update(c["source_uid"] for c in cands)
    return items, hold, report


def _absence_warnings(report):
    w = []
    if report["riconciliate"]:
        w.append("%d assenze ri-emesse da ClasseViva con un evtId nuovo (stessa data, codice e ora): identita' mantenuta, "
                 "nessun doppione (evento 'reidentified'): %s" % (len(report["riconciliate"]), ", ".join(
                     "%s->%s" % (r["record"], r["evtId_nuovo"]) for r in report["riconciliate"])))
    if report["ambigue"]:
        w.append("%d assenze con identita' AMBIGUA: non registrate, candidati non marcati come scomparsi (da verificare): %s"
                 % (len(report["ambigue"]), "; ".join("evtId %s (%s)" % (a["evtId"], a["motivo"]) for a in report["ambigue"])))
    if report["incomplete"]:
        w.append("%d assenze incomplete (senza data o codice): non registrate, nessun valore inventato"
                 % len(report["incomplete"]))
    return w


def sync_kind(registry, kind, fetch, window=None, **kw):
    """fetch() -> lista di record GREZZI. Errori (rete, parsing) -> run 'error', nessun record toccato.
    Assenze: identita' riconciliata (resolve_absences) prima dell'ingest; esito in res['riconciliazione'] e negli avvisi."""
    if kind != "absence":
        return registry.run_source(SOURCE, kind, lambda: to_items(kind, fetch()), window=window,
                                   volatile=VOLATILE.get(kind, ()), **kw)
    hold, report = set(), {}

    def items():
        its, held, rep = resolve_absences(registry, fetch())
        hold.update(held)          # riempito PRIMA dell'ingest: run_source chiama fetch() e poi ingest(hold=hold)
        report.update(rep)
        return its
    res = registry.run_source(SOURCE, kind, items, window=window, volatile=VOLATILE.get(kind, ()),
                              hold=hold, **kw)
    if report and any(report.values()):
        res["riconciliazione"] = report
        if res.get("warnings") is None:
            res["warnings"] = []
        res["warnings"].extend(_absence_warnings(report))
    return res


def verify_items(kind, raws, registry):
    """Item con cui la verifica post-scrittura confronta il registro: stessa identita' usata dalla sincronizzazione
    (per le assenze riconciliate il source_uid e' quello del record, non l'evtId nuovo). Le ambigue/incomplete,
    non registrate per scelta, sono escluse."""
    if kind != "absence":
        return to_items(kind, raws)
    return resolve_absences(registry, raws)[0]


def normalized(kind, raw):
    return KINDS[kind]["normalize"](raw)
