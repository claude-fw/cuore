#!/usr/bin/env python3
"""Probe OAuth minimo per Google Classroom (SOLA LETTURA, un solo scope).

Scopo: verificare se l'account scolastico puo' autorizzare l'accesso
read-only a Google Classroom e, se si', leggere l'elenco dei corsi.

Cosa fa:
  1. legge client id / client secret dal Keychain di macOS
     (servizio ``cloud.classroom-probe``);
  2. avvia il flusso OAuth "app desktop" con PKCE (S256) su 127.0.0.1
     (porta casuale), chiedendo SOLO lo scope ``classroom.courses.readonly``;
  3. scambia il codice con un access token (solo in memoria, nessun
     refresh token richiesto: niente ``access_type=offline``);
  4. chiama ``GET /v1/courses`` (paginato) e stampa id/nome/sezione/stato;
  5. REVOCA il token e termina.

Cosa NON fa: non scrive su Classroom (nessuna chiamata diversa da GET),
non salva token ne' dati su disco, non usa Drive, non tocca registro,
ClasseViva o scheduler. Solo libreria standard.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import secrets
import subprocess
import sys
import threading  # noqa: F401  (usato dai test)
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass

SCOPE = "https://www.googleapis.com/auth/classroom.courses.readonly"
KEYCHAIN_SERVICE = "cloud.classroom-probe"
KEYCHAIN_ACCOUNTS = ("client_id", "client_secret")


@dataclass(frozen=True)
class Endpoints:
    auth: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token: str = "https://oauth2.googleapis.com/token"
    revoke: str = "https://oauth2.googleapis.com/revoke"
    courses: str = "https://classroom.googleapis.com/v1/courses"


DEFAULT_ENDPOINTS = Endpoints()

# Interpretazioni (PROBABILI, non verificate) degli errori piu' tipici.
HINTS = {
    "access_denied": "Il consenso e' stato negato o annullato nella schermata Google.",
    "admin_policy_enforced": "Probabile: l'amministratore del dominio scolastico blocca le app di terze parti.",
    "org_internal": "Probabile: l'app e' limitata a un'altra organizzazione.",
    "invalid_client": "Client id/secret non validi o client di tipo sbagliato (serve 'App desktop').",
    "invalid_grant": "Codice di autorizzazione non valido/scaduto o PKCE non corrispondente.",
    "invalid_scope": "Scope non riconosciuto/non consentito per questo client.",
    "redirect_uri_mismatch": "Redirect non accettato: il client deve essere di tipo 'App desktop'.",
    "access_not_configured": "Probabile: API Classroom non abilitata nel progetto Cloud.",
}


# --------------------------------------------------------------------- PKCE
def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_verifier() -> str:
    return secrets.token_urlsafe(64)  # 86 caratteri, ammesso 43..128 (RFC 7636)


# ----------------------------------------------------------------- Keychain
def keychain_get(account: str, service: str = KEYCHAIN_SERVICE):
    """Legge un valore dal Keychain; None se assente o se `security` non c'e'."""
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    value = r.stdout.rstrip("\n")
    return value or None


# ---------------------------------------------------------- callback server
class _Callback(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        self.server.result = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        body = ("<html><body style='font-family:-apple-system,sans-serif;padding:3em'>"
                "<h2>Risposta ricevuta</h2><p>Puoi chiudere questa scheda e tornare al Terminale.</p>"
                "</body></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # nessun log (l'URL contiene il codice)
        pass


def _http(method: str, url: str, *, data: dict | None = None, headers: dict | None = None,
          timeout: int = 30):
    """Ritorna (status, json|None). Non solleva su errori HTTP."""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    try:
        return status, json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        return status, None


def _result(outcome: str, **kw) -> dict:
    kw["outcome"] = outcome
    kw.setdefault("ok", False)
    return kw


def _describe_error(status, payload) -> str:
    """Estrae il messaggio d'errore Google SENZA ripetere altro contenuto."""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):  # errore API
            reasons = []
            for d in err.get("details") or []:
                if isinstance(d, dict) and d.get("reason"):
                    reasons.append(str(d["reason"]))
            return (f"HTTP {status} · code={err.get('code')} · status={err.get('status')} · "
                    f"message={err.get('message')}" + (f" · reason={','.join(reasons)}" if reasons else ""))
        if err:  # errore OAuth
            return f"HTTP {status} · error={err} · error_description={payload.get('error_description')}"
    return f"HTTP {status} · risposta non interpretabile"


def run(client_id: str, client_secret: str, account: str, *, out=print, open_url=None,
        timeout: int = 180, revoke: bool = True, endpoints: Endpoints = DEFAULT_ENDPOINTS) -> dict:
    """Esegue il test. Ritorna un dict con `outcome`, `ok`, `courses`, ecc."""
    verifier = new_verifier()
    state = secrets.token_urlsafe(24)

    try:
        srv = http.server.HTTPServer(("127.0.0.1", 0), _Callback)
    except OSError as e:
        out(f"ERRORE: impossibile aprire la porta locale 127.0.0.1: {e}")
        return _result("local_server_error", error=str(e))
    srv.result = None
    srv.timeout = 1
    redirect_uri = f"http://127.0.0.1:{srv.server_address[1]}/callback"

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
        "login_hint": account,
    }
    auth_url = endpoints.auth + "?" + urllib.parse.urlencode(params)

    out(f"Scope richiesto (unico): {SCOPE}")
    out(f"Account suggerito: {account}")
    out("Apro il browser per il consenso Google...")
    opened = False
    try:
        opened = bool((open_url or webbrowser.open)(auth_url))
    except Exception as e:  # noqa: BLE001
        out(f"(apertura automatica del browser non riuscita: {e})")
    if not opened:
        out("Apri manualmente questo indirizzo nel browser:")
        out(auth_url)
    out(f"In attesa della risposta di Google (max {timeout}s)...")

    deadline = time.monotonic() + timeout
    try:
        while srv.result is None and time.monotonic() < deadline:
            srv.handle_request()
    finally:
        srv.server_close()

    cb = srv.result
    if cb is None:
        out("ESITO: nessuna risposta entro il tempo limite.")
        out("Se il browser ha mostrato 'Accesso bloccato' / 'App bloccata', copia il testo ESATTO "
            "della schermata: indica una restrizione del dominio.")
        return _result("timeout")
    if cb.get("state") != state:
        out("ESITO: ERRORE di sicurezza: parametro 'state' non corrispondente. Interrompo senza scambiare il codice.")
        return _result("state_mismatch")
    if "error" in cb:
        err = cb.get("error")
        desc = cb.get("error_description")
        out(f"ESITO: consenso NON concesso. error={err}" + (f" · error_description={desc}" if desc else ""))
        if err in HINTS:
            out(f"Interpretazione (probabile): {HINTS[err]}")
        return _result("consent_error", error=err, error_description=desc)
    code = cb.get("code")
    if not code:
        out("ESITO: risposta senza codice di autorizzazione.")
        return _result("no_code")

    out("Consenso ricevuto. Scambio il codice con un token (solo in memoria)...")
    status, tok = _http("POST", endpoints.token, data={
        "code": code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
        "code_verifier": verifier,
    })
    if status != 200 or not isinstance(tok, dict) or "access_token" not in tok:
        err = tok.get("error") if isinstance(tok, dict) else None
        out("ESITO: scambio del codice FALLITO. " + _describe_error(status, tok))
        if err in HINTS:
            out(f"Interpretazione (probabile): {HINTS[err]}")
        return _result("token_error", status=status, error=err)

    access = tok["access_token"]
    to_revoke = tok.get("refresh_token") or access
    granted = sorted(set((tok.get("scope") or "").split()))
    result = _result("unknown", granted_scopes=granted)

    def _revoke():
        if not revoke:
            return
        st, _ = _http("POST", endpoints.revoke, data={"token": to_revoke},
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
        result["revoked"] = (st == 200)
        out("Token revocato." if st == 200 else f"ATTENZIONE: revoca token non confermata (HTTP {st}). "
            "Puoi revocarlo da https://myaccount.google.com/permissions")

    try:
        out(f"Scope concessi da Google: {', '.join(granted) or '(non indicati)'}")
        if granted != [SCOPE]:
            out("ESITO: gli scope concessi NON coincidono con quello richiesto. Non procedo con le chiamate API.")
            result["outcome"] = "scope_mismatch"
            return result

        courses, page_token, pages = [], None, 0
        while True:
            q = {"pageSize": "100"}
            if page_token:
                q["pageToken"] = page_token
            status, data = _http("GET", endpoints.courses + "?" + urllib.parse.urlencode(q),
                                 headers={"Authorization": f"Bearer {access}"})
            pages += 1
            if status != 200 or not isinstance(data, dict):
                out("ESITO: chiamata courses.list FALLITA. " + _describe_error(status, data))
                result.update(outcome="api_error", status=status)
                return result
            courses.extend(data.get("courses") or [])
            page_token = data.get("nextPageToken")
            if not page_token or pages >= 20:
                break

        result.update(outcome="ok", ok=True, courses=[
            {k: c.get(k) for k in ("id", "name", "section", "courseState")} for c in courses])
        out(f"ESITO: OAuth RIUSCITO. Corsi restituiti: {len(courses)}")
        for c in result["courses"]:
            out(f"  - id={c['id']} · nome={c['name']!r} · sezione={c['section']!r} · stato={c['courseState']}")
        if not courses:
            out("  (nessun corso: l'accesso funziona, l'elenco e' semplicemente vuoto)")
        return result
    finally:
        _revoke()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Probe OAuth Google Classroom (solo courses.readonly)")
    ap.add_argument("--account", help="account Google da usare (es. nome@example.invalid)")
    ap.add_argument("--check", action="store_true", help="verifica solo che il Keychain sia pronto; nessuna rete")
    ap.add_argument("--no-open", action="store_true", help="non aprire il browser: stampa l'indirizzo")
    ap.add_argument("--timeout", type=int, default=180, help="secondi di attesa del consenso (default 180)")
    args = ap.parse_args(argv)

    cid = keychain_get("client_id")
    csec = keychain_get("client_secret")
    print(f"Keychain '{KEYCHAIN_SERVICE}': client_id {'OK' if cid else 'MANCANTE'} · "
          f"client_secret {'OK' if csec else 'MANCANTE'}")
    if not (cid and csec):
        print("Esegui prima: ./setup_keychain.sh")
        return 2
    if args.check:
        print(f"Scope che verra' richiesto: {SCOPE}")
        return 0
    if not args.account or "@" not in args.account:
        print("Serve --account nome@dominio")
        return 2
    res = run(cid, csec, args.account, timeout=args.timeout,
              open_url=(lambda u: False) if args.no_open else None)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
