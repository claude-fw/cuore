"""Registro delle azioni di CUORE (`cuore.actions/1`): richiesta -> decisione del titolare -> esito, append-only e concatenato.

L'agente (o chiunque) PROPONE un'azione che modifica dati o configurazione; solo il titolare la APPROVA o la RIFIUTA, da un
terminale interattivo, ridigitando il codice dell'azione; l'esecuzione resta fuori da qui (gli strumenti esistenti:
`import-plan.ts --apply`, sincronizzazione con `commit=true`, ...) e chi la esegue ne REGISTRA l'esito, ammesso solo per
un'azione approvata, non scaduta e non ancora eseguita. Questo modulo non esegue nulla e non tocca dati di School o Finance.

File: JSON a righe, una riga per passaggio di stato, ciascuna con `prev` (hash della riga precedente) e `hash` (sha256 della
riga canonica): una riga modificata, tolta o riordinata rompe la catena e `verify` lo dichiara. Scrittura con lock
esclusivo e O_APPEND, permessi 0600. Nessun percorso predefinito: il registro esiste solo dove viene indicato
esplicitamente (`--registro FILE`), quindi nulla viene creato finche' il titolare non decide di usarlo.

    python3 app/bridge/cuore_actions.py --registro FILE proponi importa_estratto "estratto MPS di ottobre"
    python3 app/bridge/cuore_actions.py --registro FILE elenco
    python3 app/bridge/cuore_actions.py --registro FILE approva <id>      # solo da terminale interattivo
    python3 app/bridge/cuore_actions.py --registro FILE esito <id> eseguita --riferimento <sha256 del rapporto>
    python3 app/bridge/cuore_actions.py --registro FILE verifica
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sys
import uuid

SCHEMA = "cuore.actions/1"
APPROVAL_TTL = dt.timedelta(hours=24)       # un'approvazione vale per un'esecuzione entro 24 ore, poi va rinnovata
# tipo -> (area, cosa modifica). Vocabolario chiuso: un'azione non elencata non si puo' nemmeno proporre.
ACTION_TYPES = {
    "sincronizza_school": ("school", "scrittura nel Registry (sincronizzazione con commit)"),
    "ripristina_registry": ("school", "sostituzione del Registry con uno snapshot verificato"),
    "importa_estratto": ("finance", "importazione reale di un estratto (import-plan.ts --apply)"),
    "decidi_revisione": ("finance", "decisione su una voce della coda di verifica (classificazione, collegamento)"),
    "modifica_automazione": ("core", "LaunchAgent, pianificazione o impostazioni energetiche del Mac"),
    "collega_fonte": ("core", "collegamento di una nuova fonte a CUORE (es. Finance nel registro degli eventi)"),
    "commit_locale": ("core", "commit locale nel repository CUORE"),
}
STEPS = ("richiesta", "approvata", "rifiutata", "eseguita", "fallita", "annullata")
NEXT = {None: ("richiesta",), "richiesta": ("approvata", "rifiutata", "annullata"),
        "approvata": ("eseguita", "fallita", "annullata")}
BY = ("agente", "titolare", "esecutore")
WHO_MAY = {"richiesta": ("agente", "titolare"), "approvata": ("titolare",), "rifiutata": ("titolare",),
           "annullata": ("titolare", "agente"), "eseguita": ("esecutore", "titolare"), "fallita": ("esecutore", "titolare")}
FIELDS = ("schema", "seq", "prev", "at", "azione", "passo", "tipo", "da", "motivo", "riferimento")
_TEXT = re.compile(r"^[^\n\r\t]{1,200}$")
_REF = re.compile(r"^[\w.:/@+-]{1,128}$")
UTC = dt.timezone.utc


class ActionError(ValueError):
    """Passaggio non ammesso (stato, autore, scadenza, testo): niente viene scritto."""


def _canon(rec):
    return json.dumps({k: rec[k] for k in FIELDS}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(rec):
    return hashlib.sha256(_canon(rec).encode()).hexdigest()


def _parse(path):
    """(righe, problemi). Righe illeggibili, catena rotta, sequenza o passaggi non ammessi = problemi (mai corretti)."""
    recs, problems = [], []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return [], []
    prev, state, approved_at = None, {}, {}
    for n, line in enumerate(lines, 1):
        try:
            r = json.loads(line)
            if not isinstance(r, dict) or set(r) != set(FIELDS) | {"hash"} or r["schema"] != SCHEMA:
                raise ValueError
        except ValueError:
            problems.append("riga %d: non nel formato %s" % (n, SCHEMA))
            prev = None
            continue
        if r["seq"] != len(recs) + 1 or r["prev"] != prev or r["hash"] != _digest(r):
            problems.append("riga %d: catena interrotta (riga modificata, tolta o riordinata)" % n)
        a, p = r["azione"], r["passo"]
        if r["tipo"] not in ACTION_TYPES or p not in NEXT.get(state.get(a), ()) or r["da"] not in WHO_MAY.get(p, ()):
            problems.append("riga %d: passaggio non ammesso (%s -> %s da %s)" % (n, state.get(a), p, r["da"]))
        if p == "approvata":
            approved_at[a] = r["at"]
        if p == "eseguita" and a in approved_at and _t(r["at"]) - _t(approved_at[a]) > APPROVAL_TTL:
            problems.append("riga %d: esecuzione dopo la scadenza dell'approvazione" % n)
        state[a] = p
        recs.append(r)
        prev = r["hash"]
    return recs, problems


def _t(v):
    return dt.datetime.fromisoformat(v)


def read(path):
    """Stato delle azioni: {schema, azioni: [{id, tipo, area, stato, richiesta, ultimo, passi, scaduta}], integro, problemi}."""
    recs, problems = _parse(path)
    acts = {}
    for r in recs:
        a = acts.setdefault(r["azione"], {"id": r["azione"], "tipo": r["tipo"], "area": ACTION_TYPES.get(r["tipo"], ("?",))[0],
                                          "motivo": r["motivo"], "richiesta": r["at"], "richiesta_da": r["da"], "passi": []})
        a["passi"].append({"passo": r["passo"], "at": r["at"], "da": r["da"], "riferimento": r["riferimento"]})
        a["stato"], a["ultimo"] = r["passo"], r["at"]
    return {"schema": SCHEMA, "azioni": sorted(acts.values(), key=lambda a: (a["richiesta"], a["id"])),
            "integro": not problems, "problemi": problems}


def _append(path, azione, passo, tipo, da, motivo=None, riferimento=None, now=None, check=None):
    """Scrive un passaggio sotto lock esclusivo, dopo aver riletto e verificato il registro (un registro non integro
    non accetta nuove righe: prima va esaminato)."""
    now = (now or dt.datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    if motivo is not None and not _TEXT.match(motivo):
        raise ActionError("motivo: una riga, da 1 a 200 caratteri")
    if riferimento is not None and not _REF.match(riferimento):
        raise ActionError("riferimento: identificativo breve (hash, nome di file o di piano), senza spazi")
    if da not in BY:
        raise ActionError("autore non ammesso")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        recs, problems = _parse(path)
        if problems:
            raise ActionError("registro non integro: %s" % problems[0])
        cur = None
        for r in recs:
            if r["azione"] == azione:
                cur = r
        state = cur["passo"] if cur else None
        if passo not in NEXT.get(state, ()):
            raise ActionError("passaggio non ammesso: %s -> %s" % (state or "nessuna richiesta", passo))
        if da not in WHO_MAY[passo]:
            raise ActionError("%s non puo' registrare '%s'" % (da, passo))
        if cur is not None and tipo != cur["tipo"]:
            raise ActionError("tipo diverso da quello richiesto")
        if check:
            check(recs, now)
        rec = {"schema": SCHEMA, "seq": len(recs) + 1, "prev": recs[-1]["hash"] if recs else None,
               "at": now.isoformat(), "azione": azione, "passo": passo, "tipo": tipo, "da": da,
               "motivo": motivo if passo == "richiesta" else None, "riferimento": riferimento}
        rec["hash"] = _digest(rec)
        os.write(fd, (json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n").encode())
        os.fsync(fd)
        return rec
    finally:
        os.close(fd)


def propose(path, tipo, motivo, da="agente", riferimento=None, now=None):
    if tipo not in ACTION_TYPES:
        raise ActionError("tipo di azione non previsto: %s" % tipo)
    return _append(path, "act-" + uuid.uuid4().hex[:12], "richiesta", tipo, da, motivo, riferimento, now)


def _current(path, azione):
    st = next((a for a in read(path)["azioni"] if a["id"] == azione), None)
    if st is None:
        raise ActionError("azione sconosciuta")
    return st


def decide(path, azione, approve, now=None):
    """Decisione del titolare (l'interfaccia che la chiama deve garantire che sia davvero il titolare: vedi `main`)."""
    st = _current(path, azione)
    return _append(path, azione, "approvata" if approve else "rifiutata", st["tipo"], "titolare", now=now)


def cancel(path, azione, da="titolare", now=None):
    st = _current(path, azione)
    return _append(path, azione, "annullata", st["tipo"], da, now=now)


def record_outcome(path, azione, ok, riferimento=None, da="esecutore", now=None):
    """Esito di un'esecuzione avvenuta fuori da qui: ammesso solo dopo un'approvazione valida (non scaduta)."""
    st = _current(path, azione)

    def not_expired(recs, t):
        appr = [r for r in recs if r["azione"] == azione and r["passo"] == "approvata"]
        if appr and t - _t(appr[-1]["at"]) > APPROVAL_TTL:
            raise ActionError("approvazione scaduta (oltre %d ore): serve una nuova richiesta"
                              % int(APPROVAL_TTL.total_seconds() // 3600))
    return _append(path, azione, "eseguita" if ok else "fallita", st["tipo"], da, riferimento=riferimento, now=now,
                   check=not_expired)


def approved_and_valid(path, azione, now=None):
    """True solo per un'azione approvata, non ancora eseguita e non scaduta: il controllo che un esecutore fa PRIMA di agire."""
    now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    doc = read(path)
    st = next((a for a in doc["azioni"] if a["id"] == azione), None)
    return bool(doc["integro"] and st and st["stato"] == "approvata" and now - _t(st["ultimo"]) <= APPROVAL_TTL)


def main(argv=None, stdin=None, out=None):
    out = out or sys.stdout
    ap = argparse.ArgumentParser(description="Registro delle azioni di CUORE (richiesta, decisione del titolare, esito).")
    ap.add_argument("--registro", required=True, help="file del registro (nessun percorso predefinito)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("proponi")
    p.add_argument("tipo", choices=sorted(ACTION_TYPES))
    p.add_argument("motivo")
    p.add_argument("--riferimento")
    sub.add_parser("elenco")
    sub.add_parser("verifica")
    for name in ("approva", "rifiuta", "annulla"):
        sub.add_parser(name).add_argument("azione")
    e = sub.add_parser("esito")
    e.add_argument("azione")
    e.add_argument("esito", choices=("eseguita", "fallita"))
    e.add_argument("--riferimento")
    a = ap.parse_args(argv)
    stdin = stdin or sys.stdin
    try:
        if a.cmd == "proponi":
            rec = propose(a.registro, a.tipo, a.motivo, da="titolare" if stdin.isatty() else "agente", riferimento=a.riferimento)
            print(json.dumps({"azione": rec["azione"], "stato": rec["passo"], "da": rec["da"]}, ensure_ascii=False), file=out)
        elif a.cmd in ("elenco", "verifica"):
            doc = read(a.registro)
            if a.cmd == "verifica":
                doc = {"integro": doc["integro"], "problemi": doc["problemi"], "azioni": len(doc["azioni"])}
            print(json.dumps(doc, ensure_ascii=False, indent=1), file=out)
            return 0 if doc["integro"] else 3
        elif a.cmd in ("approva", "rifiuta"):
            # la decisione e' del titolare: solo da un terminale interattivo e ridigitando il codice dell'azione. Un agente
            # che esegue comandi senza terminale non puo' approvare; non e' una barriera crittografica, e' una procedura.
            if not stdin.isatty():
                print("decisione rifiutata: serve un terminale interattivo (la decisione e' del titolare)", file=sys.stderr)
                return 77
            st = _current(a.registro, a.azione)
            code = a.azione[-6:]
            print("%s: %s (%s) - %s\nRidigita il codice %s per confermare: " % (a.cmd.upper(), st["tipo"],
                  ACTION_TYPES[st["tipo"]][1], st["motivo"], code), end="", file=out)
            out.flush()
            if stdin.readline().strip() != code:
                print("codice diverso: nessuna decisione registrata", file=sys.stderr)
                return 1
            rec = decide(a.registro, a.azione, a.cmd == "approva")
            print(json.dumps({"azione": rec["azione"], "stato": rec["passo"]}, ensure_ascii=False), file=out)
        elif a.cmd == "annulla":
            rec = cancel(a.registro, a.azione, da="titolare" if stdin.isatty() else "agente")
            print(json.dumps({"azione": rec["azione"], "stato": rec["passo"]}, ensure_ascii=False), file=out)
        elif a.cmd == "esito":
            rec = record_outcome(a.registro, a.azione, a.esito == "eseguita", riferimento=a.riferimento)
            print(json.dumps({"azione": rec["azione"], "stato": rec["passo"]}, ensure_ascii=False), file=out)
    except ActionError as e:
        print("rifiutato: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.exit(main())
