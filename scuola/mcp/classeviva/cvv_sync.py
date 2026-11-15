"""Sincronizzazione MANUALE ClasseViva -> registro locale Scuola (Fase 2B).

DEFAULT = SIMULAZIONE. Il database reale viene creato/scritto SOLO con commit=True (booleano vero e proprio).
Il percorso e' sempre registry.default_path() (nessun parametro lo cambia dal tool MCP).

Ordine con commit=True (ogni passo ferma tutto se fallisce):
  1. UNA lettura live per tipo: piano e scrittura usano gli STESSI record;
  2. piano in memoria contro la baseline (stessa logica del dry-run verificato);
  3. protezione expect_create: se i record da creare non sono esattamente quelli attesi, non scrive;
  4. lock non bloccante (nessuna sincronizzazione concorrente);
  5. cartella privata 0700 (solo quelle create qui); file 0600 (garantito da registry.py);
  6. backup SOLO se il piano modifica righe esistenti (update/missing/state_change/reappeared) o se serve la migrazione di
     schema (snapshot dedicato 'premigrazione'); ogni snapshot e' VERIFICATO (integrity_check + stesso numero di righe) e,
     se la verifica fallisce, la scrittura NON parte. Retention: solo un piano, i file si eliminano solo con prune_backups=true;
  7. scrittura per tipo (una transazione ciascuno): un tipo in errore non blocca gli altri;
  8. verifica post-scrittura a connessione chiusa, rileggendo il file da zero.
Mai: scritture su ClasseViva o Classroom; cancellazioni; scheduler.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import urllib.parse

import cvv_dryrun as D
import cvv_registry as CR
import registry as R

DIR_MODE, FILE_MODE = 0o700, 0o600
RETENTION_KEEP_LAST = 10          # snapshot piu' recenti sempre conservati
RETENTION_KEEP_DAYS = 30          # e comunque quelli piu' giovani di 30 giorni; 'premigrazione' mai potati
MODIFYING = ("update", "missing", "state_change", "reappeared")


class SyncBusy(Exception):
    pass


class FetchFailed(Exception):
    pass


class _Lock:
    """Lock esclusivo non bloccante (flock) su un file accanto al database."""
    def __init__(self, path):
        self.path, self.fd = path, None

    def __enter__(self):
        import fcntl
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, FILE_MODE)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise SyncBusy("un'altra sincronizzazione e' in corso (lock %s)" % self.path)
        return self

    def __exit__(self, *a):
        import fcntl
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)


def ensure_private_dir(path):
    """Crea le cartelle MANCANTI con permessi 0700; quelle gia' esistenti non vengono toccate. Ritorna le create."""
    path = os.path.abspath(path)
    missing = []
    cur = path
    while not os.path.isdir(cur):
        missing.append(cur)
        cur = os.path.dirname(cur)
    created = []
    for d in reversed(missing):
        os.mkdir(d, DIR_MODE)
        os.chmod(d, DIR_MODE)          # indipendente da umask
        created.append(d)
    return created


def _mode(path):
    return oct(os.stat(path).st_mode & 0o777)


def _table_counts(conn):
    return {t: conn.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
            for t in ("records", "versions", "events", "runs", "projections")}


def _backup(reg, dirpath, label=None):
    """Snapshot del registro (API di backup di SQLite) + VERIFICA: integrity_check e stesso numero di righe per tabella.
    Ritorna (percorso, dettaglio verifica). Se la verifica fallisce solleva RuntimeError: la scrittura non deve partire."""
    bdir = os.path.join(dirpath, "backups")
    ensure_private_dir(bdir)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = "registry-%s%s" % ((label + "-") if label else "", stamp)
    dest = os.path.join(bdir, base + ".sqlite3")
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(bdir, "%s-%d.sqlite3" % (base, n))
    out = sqlite3.connect(dest)
    try:
        reg.db.backup(out)
    finally:
        out.close()
    os.chmod(dest, FILE_MODE)
    chk = sqlite3.connect("file:%s?immutable=1" % urllib.parse.quote(dest), uri=True)
    try:
        ic = chk.execute("PRAGMA integrity_check").fetchone()[0]
        cb = _table_counts(chk)
    finally:
        chk.close()
    ca = _table_counts(reg.db)
    ok = ic == "ok" and cb == ca
    detail = {"integrity_check": ic, "righe_originale": ca, "righe_snapshot": cb, "ok": ok}
    if not ok:
        raise RuntimeError("snapshot non verificato: %s" % detail)
    return dest, detail


def retention_plan(dirpath, keep_last=RETENTION_KEEP_LAST, keep_days=RETENTION_KEEP_DAYS, now=None):
    """Piano di retention degli snapshot (NON elimina nulla). Si conservano i 'keep_last' piu' recenti e quelli piu' giovani
    di 'keep_days'; gli snapshot 'premigrazione' non si potano mai. Considera solo registry-*.sqlite3 in backups/."""
    bdir = os.path.join(dirpath, "backups")
    now = now or dt.datetime.now(dt.timezone.utc).timestamp()
    files = sorted((f for f in (os.listdir(bdir) if os.path.isdir(bdir) else [])
                    if f.startswith("registry-") and f.endswith(".sqlite3")),
                   key=lambda f: (os.stat(os.path.join(bdir, f)).st_mtime, f), reverse=True)
    keep, prune = [], []
    for i, f in enumerate(files):
        age_days = (now - os.stat(os.path.join(bdir, f)).st_mtime) / 86400
        if "premigrazione" in f or i < keep_last or age_days <= keep_days:
            keep.append(f)
        else:
            prune.append(f)
    return {"politica": {"ultimi": keep_last, "giorni": keep_days, "premigrazione": "mai potati"},
            "totale": len(files), "da_conservare": len(keep), "da_potare": prune}


def prune_backups(dirpath, plan):
    """Elimina SOLO i file indicati nel piano (e solo se sono snapshot registry-*.sqlite3 dentro backups/)."""
    bdir = os.path.join(dirpath, "backups")
    done = []
    for f in plan["da_potare"]:
        if f.startswith("registry-") and f.endswith(".sqlite3") and "premigrazione" not in f and os.sep not in f:
            os.remove(os.path.join(bdir, f))
            done.append(f)
    return done


def _totals(plan_letture):
    t = {"create": 0, "update": 0, "state_change": 0, "reappeared": 0, "missing": 0, "unchanged": 0}
    for e in plan_letture.values():
        if e.get("stato") == "ok":
            for k in t:
                t[k] += e["conteggi"].get(k, 0)
    return t


def _verify(A, path, fetched, written_kinds, run_ids=None):
    """Rilegge il file DA ZERO (connessione nuova, sola lettura) e confronta con i record letti da ClasseViva."""
    checks = []

    def chk(name, ok, detail=None):
        checks.append({"controllo": name, "ok": bool(ok), "dettaglio": detail})

    d = os.path.dirname(path)
    chk("file esiste", os.path.isfile(path))
    chk("permessi file 0600", _mode(path) == oct(FILE_MODE), _mode(path))
    chk("permessi cartella 0700", _mode(d) == oct(DIR_MODE), _mode(d))
    wal = path + "-wal"
    chk("nessun -wal residuo", not (os.path.exists(wal) and os.path.getsize(wal) > 0))
    raw_conn = sqlite3.connect("file:%s?immutable=1" % urllib.parse.quote(path), uri=True)
    try:
        ic = raw_conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        raw_conn.close()
    chk("PRAGMA integrity_check", ic == "ok", ic)
    reg = R.Registry.from_snapshot(path)
    try:
        chk("hash ricalcolati dai RAW (verify_integrity)", reg.verify_integrity() == [], reg.verify_integrity() or "OK")
        per_kind = {}
        for kind in written_kinds:
            raws = fetched[kind]
            # stessa identita'/riparazione usata dalla sincronizzazione (assenze riconciliate, risposte Classroom degradate),
            # risolta sul registro appena scritto; adattatori senza verify_items: comportamento invariato
            items = A.verify_items(kind, raws, reg) if hasattr(A, "verify_items") else A.to_items(kind, raws)
            excl = A.VOLATILE.get(kind, ())
            bad, vol_bad = [], []
            for it in items:
                rec = reg.get_record(A.SOURCE, kind, it["source_uid"])
                if rec is None:
                    bad.append(it["legacy_uid"] or it["source_uid"])
                    continue
                if R.strip_volatile(rec["raw"], excl) != R.strip_volatile(it["raw"], excl):
                    bad.append(it["legacy_uid"] or it["source_uid"])
                want_vol = {k: it["raw"][k] for k in excl if not k.startswith(R.DEEP) and k in it["raw"]} or None
                if rec["volatile"] != want_vol:
                    vol_bad.append(it["legacy_uid"] or it["source_uid"])
                if rec["source_status"] != R.PRESENT or rec["legacy_uid"] != it["legacy_uid"]:
                    bad.append(it["legacy_uid"] or it["source_uid"])
            present = len(reg.list_records(source=A.SOURCE, kind=kind, status=R.PRESENT))
            vers1 = sum(1 for r in reg.list_records(source=A.SOURCE, kind=kind) if r["current_version"] == 1)
            per_kind[kind] = {"letti_da_classeviva": len(items), "present_nel_registro": present,
                              "alla_versione_1": vers1, "raw_diversi_o_mancanti": sorted(set(bad)),
                              "stato_volatile_diverso": vol_bad}
            chk("%s: tutti i record letti sono nel registro con RAW identico" % kind, not bad and present >= len(items), per_kind[kind])
            chk("%s: stato volatile registrato" % kind, not vol_bad)
        for kind, rid in (run_ids or {}).items():
            row = reg.db.execute("SELECT mode, finished_at, duration_ms, outcome FROM runs WHERE run_id=?", (rid,)).fetchone()
            chk("%s: run registrato con modalita', fine, durata ed esito" % kind,
                row is not None and row["mode"] == "commit" and row["finished_at"] is not None
                and row["duration_ms"] is not None and row["outcome"] in ("ok", "ok_con_rifiuti", "rifiutato"),
                dict(row) if row else None)
        chk("storico dei run protetto da trigger", len(reg.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name IN ('runs_no_update','runs_no_delete')").fetchall()) == 2)
        events = reg.db.execute("SELECT event, count(*) c FROM events GROUP BY event").fetchall()
        runs = reg.db.execute("SELECT status, count(*) c FROM runs GROUP BY status").fetchall()
        excl_n = [r["hash_excludes"] for r in reg.db.execute(
            "SELECT DISTINCT hash_excludes FROM versions WHERE kind='notice'")]
        if "notice" in written_kinds and fetched["notice"]:
            chk("comunicazioni: readStatus escluso dal content_hash", excl_n == ['["readStatus"]'], excl_n)
        info = {"eventi": {r["event"]: r["c"] for r in events}, "run": {r["status"]: r["c"] for r in runs},
                "totale_record": len(reg.list_records()), "per_kind": per_kind,
                "file": sorted(os.listdir(d))}
    finally:
        reg.close()
    return {"tutto_ok": all(c["ok"] for c in checks), "controlli": checks, "stato_registro": info}


def run(fetchers, kinds=None, commit=False, expect_create=None, db_path=None, adapter=None, migrate=False,
        prune_backups_=False):
    A = adapter or CR
    if not isinstance(commit, bool):
        raise ValueError("commit deve essere un booleano vero (true/false), ricevuto %r: nessuna scrittura" % (commit,))
    if expect_create is not None and (isinstance(expect_create, bool) or not isinstance(expect_create, int)):
        raise ValueError("expect_create deve essere un intero")
    for nm, v in (("migrate", migrate), ("prune_backups", prune_backups_)):
        if not isinstance(v, bool):
            raise ValueError("%s deve essere un booleano vero (true/false), ricevuto %r" % (nm, v))
    kinds = list(kinds) if kinds else [k for k in getattr(A, "ALL_KINDS", D.ALL_KINDS) if k in fetchers]
    bad = [k for k in kinds if k not in A.KINDS or k not in fetchers]
    if bad:
        raise ValueError("kind non valido: %s" % ", ".join(bad))
    path = db_path or R.default_path()

    # 1. UNA lettura live per tipo (piano e scrittura useranno questi stessi record)
    fetched, failed = {}, {}
    for kind in kinds:
        try:
            fetched[kind] = list(fetchers[kind][0]())
        except Exception as e:      # noqa: BLE001 - isolamento per tipo
            failed[kind] = "%s: %s" % (type(e).__name__, str(e)[:250])

    def prefetch(kind):
        if kind in failed:
            def boom():
                raise FetchFailed(failed[kind])
            return boom
        return lambda: fetched[kind]

    # 2. piano in memoria (stessa logica del dry-run verificato; non scrive)
    plan = D.run({k: (prefetch(k), fetchers[k][1]) for k in kinds}, kinds=kinds, simulate=False,
                 baseline_path=path, adapter=A)
    out = {"mode": "COMMIT" if commit else "SYNC SIMULATO (commit=false)", "commit": commit, "sorgente": A.SOURCE,
           "scritture": {"sqlite_reale": False, "classeviva": False, "classroom": False},
           "baseline": plan.get("baseline")}
    if "errore_baseline" in plan:
        out.update(errore_baseline=plan["errore_baseline"], nota=plan["nota"], esito="INTERROTTO: nessuna scrittura")
        return out
    letture = plan["RISULTATO_REALE"]["letture"]
    tot = _totals(letture)
    out["piano"] = letture
    out["riepilogo"] = {"record_da_creare": tot["create"], "record_da_aggiornare": tot["update"],
                        "solo_stato_cambiato": tot["state_change"], "ricomparsi": tot["reappeared"],
                        "da_marcare_missing": tot["missing"], "invariati": tot["unchanged"],
                        "nuove_versioni_raw": tot["create"] + tot["update"],
                        "kind_con_errore_di_lettura": failed or None}
    out["verifica_nessuna_scrittura_nel_piano"] = plan["verifica_nessuna_scrittura"]
    disk_v = R.stored_version(path) if os.path.isfile(path) else None
    out["schema"] = {"su_disco": disk_v, "atteso": R.SCHEMA_VERSION,
                     "migrazione_necessaria": disk_v is not None and disk_v < R.SCHEMA_VERSION}

    if not commit:
        out["esito"] = "SIMULAZIONE: nulla scritto (il database reale non e' stato creato ne' modificato)"
        out["per_scrivere"] = ("ripeti con commit=true e expect_create=%d (la scrittura non parte se i record da creare "
                               "non sono esattamente %d)" % (tot["create"], tot["create"]))
        return out

    # 3. protezione expect_create
    if expect_create is not None and expect_create != tot["create"]:
        out.update(mode="COMMIT RIFIUTATO", esito="nessuna scrittura: record da creare %d, attesi %d: rifai il piano"
                   % (tot["create"], expect_create))
        return out

    # 3b. schema: una migrazione non e' mai implicita
    if out["schema"]["migrazione_necessaria"] and not migrate:
        out.update(mode="COMMIT RIFIUTATO",
                   esito="nessuna scrittura: il registro e' allo schema v%s, atteso v%s. Ripeti con migrate=true "
                         "(snapshot dedicato e verificato prima della migrazione)" % (disk_v, R.SCHEMA_VERSION))
        return out
    if disk_v is not None and disk_v > R.SCHEMA_VERSION:
        out.update(mode="COMMIT RIFIUTATO", esito="nessuna scrittura: schema del registro piu' recente del codice (v%s)" % disk_v)
        return out

    # 4-8. scrittura
    written = [k for k in kinds if k not in failed]
    try:
        created_dirs = ensure_private_dir(os.path.dirname(path))
        with _Lock(os.path.join(os.path.dirname(path), "registry.lock")):
            existed = os.path.isfile(path)
            migrazione = None
            if existed and out["schema"]["migrazione_necessaria"]:
                # snapshot VERIFICATO dello stato v1 (copia con l'API di backup), poi migrazione esplicita
                src = sqlite3.connect(path, timeout=30)
                try:
                    snap_reg = R.Registry.__new__(R.Registry)
                    snap_reg.db = src
                    pre, pre_chk = _backup(snap_reg, os.path.dirname(path), label="premigrazione")
                finally:
                    src.close()
                migrazione = {"snapshot_premigrazione": pre, "verifica_snapshot": pre_chk, "risultato": R.migrate_file(path)}
            reg = R.Registry(path)
            try:
                backup = None
                if existed and any(tot[k] for k in MODIFYING):
                    backup, backup_chk = _backup(reg, os.path.dirname(path))
                esiti = {}
                for kind in kinds:
                    if kind in failed:
                        reg.record_failed_run(A.SOURCE, kind, failed[kind])
                        esiti[kind] = {"stato": "errore", "errore": failed[kind],
                                       "nota": "nessun record toccato, nessun record marcato come scomparso"}
                        continue
                    raws, window = fetched[kind], fetchers[kind][1]
                    r = A.sync_kind(reg, kind, lambda raws=raws: raws, window=window)
                    esiti[kind] = {"stato": r["status"], "run_id": r["run_id"],
                                   "conteggi": {k: v for k, v in (r["counts"] or {}).items() if v},
                                   "avvisi": r.get("warnings"), "errore": r.get("error")}
            finally:
                reg.close()
    except SyncBusy as e:
        out.update(mode="COMMIT RIFIUTATO", esito=str(e))
        return out
    except Exception as e:      # noqa: BLE001
        out.update(mode="COMMIT FALLITO", esito="%s: %s" % (type(e).__name__, str(e)[:250]),
                   nota="ogni tipo e' una transazione: lo stato del registro e' coerente; rilancia il dry-run")
        return out

    out["scritture"]["sqlite_reale"] = True
    out["percorso_registro"] = path
    out["file_creato_ora"] = not existed
    out["cartelle_create_0700"] = created_dirs
    out["backup_prima_della_scrittura"] = backup
    out["migrazione_schema"] = migrazione
    plan_ret = retention_plan(os.path.dirname(path))
    out["retention_snapshot"] = dict(plan_ret, eseguita=None)
    if prune_backups_:
        out["retention_snapshot"]["eseguita"] = prune_backups(os.path.dirname(path), plan_ret)
    out["esito_scrittura"] = esiti
    ok_kinds = [k for k in written if esiti[k]["stato"] == "ok"]
    planned = {k: letture[k]["conteggi"] for k in ok_kinds if letture[k].get("stato") == "ok"}
    out["coerenza_piano_scrittura"] = all(
        {k: v for k, v in planned[k].items() if v} == esiti[k]["conteggi"] for k in planned)
    try:
        out["verifica_post_scrittura"] = _verify(A, path, fetched, ok_kinds, {k: esiti[k]["run_id"] for k in ok_kinds})
    except Exception as e:      # noqa: BLE001
        out["verifica_post_scrittura"] = {"tutto_ok": False, "errore": "%s: %s" % (type(e).__name__, str(e)[:250])}
    out["esito"] = "SCRITTO E VERIFICATO" if out["verifica_post_scrittura"]["tutto_ok"] and out["coerenza_piano_scrittura"] \
        else "SCRITTO: VERIFICA NON SUPERATA, controlla verifica_post_scrittura"
    return out


# ---------------------------------------------------------------------------------------------------------------------
# MIGRAZIONE ESPLICITA SENZA SINCRONIZZAZIONE (M3)
# ---------------------------------------------------------------------------------------------------------------------
_V1_RUN_COLS = "run_id, source, kind, started_at, status, error, counts"


def _fingerprint(conn):
    """Impronta dei CONTENUTI per tabella (hash delle righe in ordine stabile). Per 'runs' solo le colonne presenti in v1;
    per 'meta' si escludono le chiavi che la migrazione deve cambiare."""
    import hashlib
    cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)")]
    run_cols = ", ".join(c.strip() for c in _V1_RUN_COLS.split(",") if c.strip() in cols)
    q = {"records": "SELECT * FROM records ORDER BY rowid", "versions": "SELECT * FROM versions ORDER BY rowid",
         "events": "SELECT * FROM events ORDER BY rowid", "projections": "SELECT * FROM projections ORDER BY rowid",
         "runs": "SELECT %s FROM runs ORDER BY rowid" % run_cols,
         "meta": "SELECT key, value FROM meta WHERE key NOT IN ('schema_version','migrated_v1_to_v2_at') ORDER BY key"}
    out = {}
    for t, sql in q.items():
        h = hashlib.sha256()
        n = 0
        for row in conn.execute(sql):
            h.update(repr(tuple(row)).encode("utf-8"))
            n += 1
        out[t] = {"righe": n, "sha256": h.hexdigest()}
    return out


def migrate_only(db_path=None, confirm=False):
    """Migrazione di schema ESPLICITA, senza lettura live e senza alcun run: snapshot 'premigrazione' verificato ->
    migrazione v1->v2 -> verifica a connessione nuova (versione, righe e contenuti invariati, nuove colonne, trigger,
    integrity_check). Senza confirm=True non scrive nulla (mostra solo cosa farebbe). Non tocca i dati applicativi."""
    if not isinstance(confirm, bool):
        raise ValueError("confirm deve essere un booleano vero (true/false), ricevuto %r: nessuna scrittura" % (confirm,))
    path = db_path or R.default_path()
    out = {"mode": "MIGRAZIONE" if confirm else "ANTEPRIMA (confirm=false)", "percorso_registro": path,
           "scritture": {"sqlite_reale": False, "classeviva": False, "classroom": False}}
    if not os.path.isfile(path):
        out["esito"] = "nessuna scrittura: registro assente"
        return out
    v = R.stored_version(path)
    out["schema"] = {"su_disco": v, "atteso": R.SCHEMA_VERSION}
    if v == R.SCHEMA_VERSION:
        out["esito"] = "nessuna migrazione necessaria: registro gia' allo schema v%s" % v
        return out
    if v != 1:
        out["esito"] = "nessuna scrittura: schema v%s non migrabile da questo codice" % v
        return out
    if not confirm:
        out["esito"] = "ANTEPRIMA: nulla scritto. Con confirm=true: snapshot 'premigrazione' verificato, poi migrazione v1->v2"
        return out
    try:
        with _Lock(os.path.join(os.path.dirname(path), "registry.lock")):
            if R.stored_version(path) != 1:
                out["esito"] = "nessuna scrittura: lo schema e' cambiato mentre si attendeva il lock"
                return out
            src = sqlite3.connect(path, timeout=30)
            try:
                before = _fingerprint(src)
                snap_reg = R.Registry.__new__(R.Registry)
                snap_reg.db = src
                pre, pre_chk = _backup(snap_reg, os.path.dirname(path), label="premigrazione")
            finally:
                src.close()
            out["prima"] = before
            out["snapshot_premigrazione"] = pre
            out["verifica_snapshot"] = pre_chk
            out["migrazione"] = R.migrate_file(path)
    except SyncBusy as e:
        out["esito"] = str(e)
        return out
    except Exception as e:      # noqa: BLE001
        out["esito"] = "MIGRAZIONE FALLITA: %s: %s" % (type(e).__name__, str(e)[:250])
        out["nota"] = "la migrazione e' una sola transazione (rollback in caso di errore); lo snapshot, se creato, resta"
        return out
    out["scritture"]["sqlite_reale"] = True
    checks = []

    def chk(name, ok, detail=None):
        checks.append({"controllo": name, "ok": bool(ok), "dettaglio": detail})

    ver = sqlite3.connect("file:%s?immutable=1" % urllib.parse.quote(path), uri=True)
    try:
        after = _fingerprint(ver)
        chk("schema v%s" % R.SCHEMA_VERSION, R._version_of(ver) == R.SCHEMA_VERSION, R._version_of(ver))
        chk("PRAGMA integrity_check", ver.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
        chk("righe e contenuti identici a prima, tabella per tabella (runs: colonne v1)", after == before,
            {t: {"prima": before[t], "dopo": after[t]} for t in before if before[t] != after[t]} or "identici")
        runs_cols = [r[1] for r in ver.execute("PRAGMA table_info(runs)")]
        new = ("finished_at", "duration_ms", "mode", "outcome")
        chk("nuove colonne di runs presenti", all(c in runs_cols for c in new), runs_cols)
        chk("nuove colonne di runs tutte NULL (storico non riscritto)", ver.execute(
            "SELECT count(*) FROM runs WHERE finished_at IS NOT NULL OR duration_ms IS NOT NULL OR mode IS NOT NULL "
            "OR outcome IS NOT NULL").fetchone()[0] == 0)
        chk("trigger runs_no_update/runs_no_delete presenti", len(ver.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name IN ('runs_no_update','runs_no_delete')").fetchall()) == 2)
        chk("righe totali per tabella", True, _table_counts(ver))
    finally:
        ver.close()
    reg = R.Registry.from_snapshot(path)
    try:
        chk("verify_integrity (hash ricalcolati dai RAW)", reg.verify_integrity() == [])
        chk("record presenti", True, len(reg.list_records()))
    finally:
        reg.close()
    d = os.path.dirname(path)
    chk("permessi file 0600", _mode(path) == oct(FILE_MODE), _mode(path))
    chk("nessun -wal residuo", not (os.path.exists(path + "-wal") and os.path.getsize(path + "-wal") > 0))
    out["dopo"] = after
    out["verifica_post_migrazione"] = {"tutto_ok": all(c["ok"] for c in checks), "controlli": checks}
    out["retention_snapshot"] = dict(retention_plan(d), eseguita=None)
    out["esito"] = "MIGRATO E VERIFICATO" if out["verifica_post_migrazione"]["tutto_ok"] else "MIGRATO: VERIFICA NON SUPERATA"
    return out
