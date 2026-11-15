"""DRY-RUN LIVE del registro locale. Non scrive NULLA: ne' il registro SQLite reale, ne' ClasseViva, ne' Classroom.

Il registro su cui lavora e' SEMPRE in memoria. Se esiste un registro reale, ne viene caricata una copia in sola lettura
(Registry.from_snapshot: immutable, nessun file creato); se non esiste, la baseline e' vuota e viene dichiarato.

Il risultato ha DUE sezioni inequivocabili:
  RISULTATO_REALE            cosa farebbe il registro con i dati letti ORA da ClasseViva;
  SIMULAZIONE_NON_ACCADUTA   scenari inventati su una COPIA dei record appena letti, per mostrare i percorsi che oggi i
                             dati reali non attraversano (update, missing, anomalia...). Mai accaduto davvero.
"""
from __future__ import annotations

import collections
import os

import cvv_registry as CR   # adapter di default (ClasseViva)
import registry as R

ALL_KINDS = ("agenda_event", "grade", "absence", "notice")
SIM_FIELD = {"lesson": "lessonArg", "notice": "cntTitle", "absence": "webJustifStatus", "agenda_event": "notes"}   # campo non-ID da alterare
SIM_BANNER = ("SCENARIO NON ACCADUTO: modifiche finte applicate a una COPIA in memoria dei record appena letti. "
              "Nulla di quanto segue e' successo su ClasseViva o nel registro.")


def _fs_state(path):
    out = {}
    for p in (path, path + "-wal", path + "-shm", path + "-journal"):
        try:
            st = os.stat(p)
            out[p] = {"esiste": True, "byte": st.st_size, "mtime_ns": st.st_mtime_ns}
        except OSError:
            out[p] = {"esiste": False}
    return out


def _legacy(A, reg, kind, uids):
    return [reg.get_record(A.SOURCE, kind, u)["legacy_uid"] or getattr(A, "UID_PREFIX", "") + u for u in uids]


def _present_raws(A, reg, kind):
    return [reg.get_record(A.SOURCE, kind, r["source_uid"])["raw"]
            for r in reg.list_records(source=A.SOURCE, kind=kind, status=R.PRESENT)]


def _da_verificare(A, reg, kind):
    groups = collections.OrderedDict()
    for raw in _present_raws(A, reg, kind):
        n = A.normalized(kind, raw)
        for motivo in (n.get("interpreted") or {}).get("da_verificare") or []:
            g = groups.setdefault(motivo, {"n": 0, "esempi": []})
            g["n"] += 1
            if len(g["esempi"]) < 3:
                g["esempi"].append(n["uid"])
    return groups


def _entry(A, reg, kind, res):
    if res["status"] != "ok":
        return {"stato": "errore", "errore": res["error"],
                "nota": "nessun record toccato e nessun record marcato come scomparso"}
    k = res["keys"]
    uid = {}
    for cat, keys in k.items():
        if not keys:
            continue
        if cat in ("create", "unchanged"):
            uid[cat] = {"n": len(keys), "esempi": _legacy(A, reg, kind, keys[:3])}
        else:
            uid[cat] = _legacy(A, reg, kind, keys)
    out = {"stato": "ok", "letti": res["items_seen"], "conteggi": res["counts"], "uid": uid,
           "avvisi": res["warnings"],
           "da_verificare": _da_verificare(A, reg, kind),
           "registro_dopo": {"present": len(reg.list_records(source=A.SOURCE, kind=kind, status=R.PRESENT)),
                             "missing_in_source": len(reg.list_records(source=A.SOURCE, kind=kind, status=R.MISSING))}}
    if res.get("riconciliazione"):        # assenze: riconciliazioni d'identita' e casi ambigui (cvv_registry.resolve_absences)
        out["riconciliazione"] = res["riconciliazione"]
    if kind == "agenda_event":
        c = collections.Counter(A.normalized(kind, raw)["kind"] for raw in _present_raws(A, reg, kind))
        out["classificazione_dedotta"] = dict(c)
        out["nota"] = "verifica/compito/evento sono DEDOTTI dai normalizzatori; la materia non e' risolta in questo dry-run"
    return out


def _mutate(A, kind, raw):
    f = getattr(A, "SIM_FIELD", SIM_FIELD).get(kind)
    if f is None or f not in raw:
        return None
    v = raw[f]
    if isinstance(v, bool):
        new = not v
    elif isinstance(v, int):
        new = 1 - v if v in (0, 1) else v + 1
    elif isinstance(v, str):
        new = v + " (SIMULATO)"
    else:
        new = "SIMULATO"
    return f, v, new


def _simulate_kind(A, reg, kind, window):
    """Scenari inventati su una COPIA; l'originale in memoria resta com'e'."""
    raws = _present_raws(A, reg, kind)
    if len(raws) < 2:
        return {"eseguita": False,
                "motivo": "campione insufficiente (%d record reali): nessun dato viene inventato per riempire lo scenario"
                          % len(raws)}
    sim = reg.clone_memory()
    fetch_now = lambda cur: (lambda: [dict(r) for r in cur])    # noqa: E731
    steps, cur = [], [dict(r) for r in raws]

    def step(name, desc, res, extra=None):
        d = {"passo": "[SIMULATO] " + name, "descrizione": desc, "conteggi": {k: v for k, v in res["counts"].items() if v},
             "avvisi": res["warnings"]}
        d.update(extra or {})
        steps.append(d)

    step("lettura identica", "la fonte restituisce gli stessi record",
         A.sync_kind(sim, kind, fetch_now(cur), window=window))
    changes = []
    m = _mutate(A, kind, cur[0])
    if m:
        cur[0][m[0]] = m[2]
        changes.append("primo record: %s %r -> %r" % m)
    if "readStatus" in cur[1] and kind == "notice":
        cur[1]["readStatus"] = not cur[1]["readStatus"]
        changes.append("secondo record: readStatus invertito (solo stato, nessuna nuova versione attesa)")
    if changes:
        res = A.sync_kind(sim, kind, fetch_now(cur), window=window)
        step("dato cambiato / stato letto", "; ".join(changes), res,
             {"versioni_primo_record": [v["version"] for v in sim.get_versions(
                 A.SOURCE, kind, A.source_uid_of(A.normalized(kind, cur[0])["uid"]))]})
    gone = cur.pop()
    res = A.sync_kind(sim, kind, fetch_now(cur), window=window)
    step("record sparito", "l'ultimo record non e' piu' restituito dalla fonte", res,
         {"stato_record_sparito": sim.get_record(A.SOURCE, kind, A.source_uid_of(A.normalized(kind, gone)["uid"]))["source_status"],
          "raw_ancora_presente": True})
    res = A.sync_kind(sim, kind, fetch_now([]), window=window)
    step("lettura vuota", "la fonte restituisce 0 record (errore o anomalia)", res)
    return {"eseguita": True, "passi": steps, "integrita_copia": sim.verify_integrity() or "OK"}


def run(fetchers, kinds=None, simulate=True, second_read=False, baseline_path=None, adapter=None):
    """fetchers: {kind: (fetch() -> lista di record GREZZI, finestra (da, a) ISO | None)}.
    adapter: modulo-adattatore della fonte (default ClasseViva; es. cls_registry per Classroom)."""
    A = adapter or CR
    kinds = list(kinds) if kinds else [k for k in getattr(A, "ALL_KINDS", ALL_KINDS) if k in fetchers]
    bad = [k for k in kinds if k not in A.KINDS or k not in fetchers]
    if bad:
        raise ValueError("kind non valido: %s (ammessi: %s)" % (", ".join(bad), ", ".join(sorted(A.KINDS))))
    path = baseline_path or R.default_path()
    before = _fs_state(path)
    head = {"mode": "DRY-RUN", "sorgente": A.SOURCE,
            "scritture": {"sqlite_reale": False, "classeviva": False, "classroom": False},
            "registro_di_lavoro": "SOLO IN MEMORIA"}
    try:
        reg = R.Registry.from_snapshot(path) if before[path]["esiste"] else R.Registry(":memory:")
    except Exception as e:     # noqa: BLE001 - una baseline illeggibile NON va sostituita con una vuota
        head["errore_baseline"] = "%s: %s" % (type(e).__name__, str(e)[:250])
        head["nota"] = "interrotto: una baseline illeggibile non viene sostituita con una vuota (confronto falsato)"
        return head
    assert reg.path == ":memory:"
    disk_v = R.stored_version(path) if before[path]["esiste"] else None
    head["baseline"] = {"percorso": path, "esiste": before[path]["esiste"], "record_caricati": len(reg.list_records()),
                        "schema": {"su_disco": disk_v, "atteso": R.SCHEMA_VERSION,
                                   "migrazione_necessaria": disk_v is not None and disk_v < R.SCHEMA_VERSION,
                                   "nota": "la copia in memoria e' portata a v2 senza toccare il file"},
                        "nota": None if before[path]["esiste"] else
                        "il registro reale non esiste ancora: confronto con baseline VUOTA (ogni record risulta 'create')"}

    reale = {"letture": {}}
    for kind in kinds:
        fetch, window = fetchers[kind]
        reale["letture"][kind] = _entry(A, reg, kind, A.sync_kind(reg, kind, fetch, window=window))
    if second_read:
        reale["secondo_giro"] = {"nota": "seconda lettura LIVE nello stesso registro in memoria: mostra solo cambiamenti avvenuti davvero"}
        for kind in kinds:
            if reale["letture"][kind]["stato"] != "ok":
                continue
            fetch, window = fetchers[kind]
            res = A.sync_kind(reg, kind, fetch, window=window)
            reale["secondo_giro"][kind] = ({"stato": "errore", "errore": res["error"]} if res["status"] != "ok" else
                                           {"stato": "ok", "conteggi": {k: v for k, v in res["counts"].items() if v},
                                            "cambiamenti_reali": any(res["counts"][k] for k in
                                                                     ("update", "state_change", "missing", "reappeared", "create"))})
    problems = reg.verify_integrity()
    reale["integrita"] = problems or "OK"
    head["RISULTATO_REALE"] = reale

    if simulate:
        sims = {}
        for kind in kinds:
            if reale["letture"][kind]["stato"] != "ok":
                sims[kind] = {"eseguita": False, "motivo": "lettura reale non riuscita"}
                continue
            sims[kind] = _simulate_kind(A, reg, kind, fetchers[kind][1])
        head["SIMULAZIONE_NON_ACCADUTA"] = {"AVVISO": SIM_BANNER, "per_kind": sims}
    else:
        head["SIMULAZIONE_NON_ACCADUTA"] = {"eseguita": False}

    after = _fs_state(path)
    head["verifica_nessuna_scrittura"] = {
        "file_reale_o_ausiliari_creati_o_modificati": before != after,
        "stato_prima_uguale_a_dopo": before == after,
        "file_controllati": sorted(after)}
    return head
