#!/usr/bin/env python3
"""DRY-RUN del registro: mostra il percorso RAW -> registro -> normalizzato -> proiezione e gli scenari di sync.

Lavora SOLO in memoria: non crea file, non contatta ClasseViva, non scrive nulla di permanente.
Senza --input usa record SINTETICI (struttura dei campi osservata sui dati reali, valori inventati e dichiarati tali).
Con --input file.json usa i record grezzi che gli dai (lista JSON, es. il campo 'raw' di cv_noticeboard).

  python3 dryrun_registry.py                       # comunicazione sintetica
  python3 dryrun_registry.py --kind absence
  python3 dryrun_registry.py --kind notice --input raw_notices.json
"""
import argparse
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cvv_registry as CR  # noqa: E402
import registry as R  # noqa: E402

SYNTH = {
    "notice": {"pubId": 900001, "pubDT": "2026-10-29T12:00:00+02:00", "readStatus": False, "evtCode": "CF", "cntId": 1,
               "cntValidFrom": "2026-10-29", "cntValidTo": "2027-06-01", "cntValidInRange": True, "cntStatus": "active",
               "cntTitle": "Circolare di esempio", "cntNum": "1", "cntCategory": "", "cntHasChanged": False,
               "cntHasAttach": True, "needJoin": False, "needReply": False, "needFile": False, "needSign": False,
               "evento_id": "1", "dinsert_allegato": "2026-10-29 12:00:00",
               "attachments": [{"fileName": "esempio.pdf", "attachNum": 1}]},
    "absence": {"evtId": 900001, "evtCode": "ABA0", "evtDate": "2026-10-27", "evtHPos": 0, "evtValue": 0,
                "hoursAbsence": [], "isJustified": False, "justifReasonCode": None, "justifReasonDesc": None,
                "webJustifStatus": 0},
    "agenda_event": {"evtId": 900001, "evtCode": "AGNT", "subjectDesc": "MATERIA DI ESEMPIO",
                     "evtDatetimeBegin": "2026-10-31T08:00:00+02:00", "evtDatetimeEnd": "2026-10-31T09:00:00+02:00",
                     "isFullDay": False, "notes": "Verifica di esempio", "authorName": "DOCENTE ESEMPIO",
                     "classDesc": "CLASSE ESEMPIO"},
}
ID_FIELD = {"notice": "pubId", "absence": "evtId", "agenda_event": "evtId"}
CHANGE = {"notice": ("cntTitle", "Circolare di esempio (rettificata)"),
          "absence": ("webJustifStatus", 1),
          "agenda_event": ("notes", "Verifica di esempio - spostata")}


def h(title):
    print("\n" + "=" * 78 + "\n" + title + "\n" + "=" * 78)


def show(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def summary(res):
    c = res["counts"]
    s = ", ".join("%s=%d" % (k, v) for k, v in c.items() if v)
    print("  esito: %s | letti=%d | %s" % (res["status"], res["items_seen"], s or "nessun cambiamento"))
    for w in res["warnings"]:
        print("  ATTENZIONE:", w)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kind", choices=sorted(ID_FIELD), default="notice")
    ap.add_argument("--input", help="file JSON con una lista di record grezzi")
    a = ap.parse_args()
    kind = a.kind

    if a.input:
        raws = json.load(open(a.input, encoding="utf-8"))
        origin = "record reali forniti da %s" % a.input
    else:
        raws = []
        for i in range(3):
            r = copy.deepcopy(SYNTH[kind]); r[ID_FIELD[kind]] += i
            raws.append(r)
        origin = "record SINTETICI (valori inventati, struttura osservata sui dati reali)"
    if len(raws) < 2:
        sys.exit("servono almeno 2 record per mostrare gli scenari")

    reg = R.Registry(":memory:")
    print("DRY-RUN registro locale | kind=%s | %d record | %s" % (kind, len(raws), origin))
    print("Registro: in memoria (nessun file). ClasseViva: non contattato.")

    h("1. RAW -> registro (prima lettura)")
    res = CR.sync_kind(reg, kind, lambda: raws)
    summary(res)
    first = CR.source_uid_of(CR.normalized(kind, raws[0])["uid"])
    rec = reg.get_record(CR.SOURCE, kind, first)

    h("2. Percorso del primo record: RAW esatto come arriva dalla fonte")
    show(raws[0])
    h("3. Cosa conserva il registro (identita', hash, versione, stato, provenienza)")
    show({k: rec[k] for k in ("source", "kind", "source_uid", "legacy_uid", "item_date", "version", "content_hash",
                              "hash_excludes", "source_status", "first_seen", "captured_at", "volatile")})
    print("  raw conservato identico all'originale:", rec["raw"] == raws[0])
    h("4. NORMALIZZATO (derivato dal RAW con le tabelle verificate; rigenerabile)")
    show({k: v for k, v in CR.normalized(kind, rec["raw"]).items() if k != "raw"})
    h("5. Seconda lettura identica -> unchanged, nessuna nuova versione")
    summary(CR.sync_kind(reg, kind, lambda: raws))

    h("6. La fonte cambia un dato -> update (nuova versione), il RAW v1 resta intatto")
    changed = copy.deepcopy(raws)
    field, value = CHANGE[kind]
    old_value = changed[0].get(field)
    changed[0][field] = value
    if kind == "notice" and len(changed) > 1:
        changed[1]["readStatus"] = not changed[1].get("readStatus", False)
    summary(CR.sync_kind(reg, kind, lambda: changed))
    vs = reg.get_versions(CR.SOURCE, kind, first)
    print("  %s: v1=%r -> v2=%r | versioni=%s" % (field, vs[0]["raw"].get(field), vs[1]["raw"].get(field),
                                                    [v["version"] for v in vs]))
    if kind == "notice":
        r2 = CR.source_uid_of(CR.normalized(kind, raws[1])["uid"])
        x = reg.get_record(CR.SOURCE, kind, r2)
        print("  (la 2a comunicazione e' stata solo 'letta': versione=%d, stato osservato=%s -> nessuna nuova versione)"
              % (x["version"], x["volatile"]))

    h("7. Un record sparisce dalla fonte -> marcato 'missing_in_source', mai cancellato")
    gone_raw = changed[-1]
    summary(CR.sync_kind(reg, kind, lambda: changed[:-1], min_ratio=0.0))
    g = reg.get_record(CR.SOURCE, kind, CR.source_uid_of(CR.normalized(kind, gone_raw)["uid"]))
    print("  stato=%s, versioni conservate=%d, raw ancora presente=%s" % (
        g["source_status"], g["version"], g["raw"] == gone_raw))
    h("8. Lettura anomala (lista vuota) -> NON marca nulla")
    summary(CR.sync_kind(reg, kind, lambda: []))
    h("9. Il record riappare -> reappeared")
    summary(CR.sync_kind(reg, kind, lambda: changed))
    h("10. Errore di rete -> run 'error', nessun record toccato")
    def down():
        raise ConnectionError("rete assente (simulata)")
    r = CR.sync_kind(reg, kind, down)
    print("  esito: %s | %s" % (r["status"], r["error"]))
    print("  record presenti dopo l'errore:", len(reg.list_records(status="present")), "su", len(reg.list_records()))

    h("Verifica finale")
    print("  integrita' hash/raw:", reg.verify_integrity() or "OK")
    print("  eventi registrati:", [(e["source_uid"][-3:], e["event"]) for e in reg.events()])
    print("  Nessun file scritto.")


if __name__ == "__main__":
    main()
