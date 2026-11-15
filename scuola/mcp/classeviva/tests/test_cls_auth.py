import http.server
import json
import os
import sys
import threading
import unittest
import urllib.parse
import urllib.request

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import cls_auth as A        # noqa: E402
import cls_registry as CL   # noqa: E402

AT, RT, CODE, CSEC, CID = "AT-SEGRETO-1", "RT-SEGRETO-2", "CODE-3", "CSECRET-4", "id.apps.googleusercontent.com"
NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class FakeKeychain:
    def __init__(self, with_client=True, fail_set=False, fail_get_after_set=False):
        self.d = {}
        if with_client:
            self.d[(A.PROBE_SERVICE, "client_id")] = CID
            self.d[(A.PROBE_SERVICE, "client_secret")] = CSEC
        self.fail_set, self.fail_get_after_set = fail_set, fail_get_after_set
        self.log = []

    def get(self, s, a):
        if self.fail_get_after_set and a == "preflight":
            return None
        return self.d.get((s, a))

    def set(self, s, a, v, label=""):
        self.log.append(("set", s, a))
        if self.fail_set and a == "refresh_token":
            raise A.AuthError("keychain", "no")
        self.d[(s, a)] = v

    def delete(self, s, a):
        self.log.append(("delete", s, a))
        return self.d.pop((s, a), None) is not None


class Google(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
        S = self.server
        S.requests.append((self.path, form))
        if self.path == "/revoke":
            return self._j(200, {})
        if S.token_error:
            return self._j(400, S.token_error)
        if form.get("grant_type") == "refresh_token":
            ok = form.get("refresh_token") == RT and form.get("client_secret") == CSEC
            if not ok:
                return self._j(400, {"error": "invalid_grant"})
            body = {"access_token": AT, "expires_in": 3599, "scope": " ".join(S.scope), "token_type": "Bearer"}
            if S.rotate:
                body["refresh_token"] = "RT-NUOVO-9"
            return self._j(200, body)
        ok = (form.get("code") == CODE and A.pkce_challenge(form.get("code_verifier", "")) == S.challenge)
        if not ok:
            return self._j(400, {"error": "invalid_grant"})
        body = {"access_token": AT, "expires_in": 3599, "scope": " ".join(S.scope), "token_type": "Bearer"}
        if S.with_rt:
            body["refresh_token"] = RT
        self._j(200, body)

    def _j(self, st, p):
        b = json.dumps(p).encode()
        self.send_response(st); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)


class Mock:
    def __init__(self, scope=None, with_rt=True, token_error=None, rotate=False):
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), Google)
        s = self.srv
        s.requests, s.challenge = [], None
        s.scope = scope if scope is not None else list(A.ALLOWED_SCOPES.values())
        s.with_rt, s.token_error, s.rotate = with_rt, token_error, rotate
        base = "http://127.0.0.1:%d" % s.server_address[1]
        self.endpoints = {"auth": base + "/auth", "token": base + "/token", "revoke": base + "/revoke"}
        threading.Thread(target=s.serve_forever, daemon=True).start()
        self.params = None
        self.opened = 0

    def close(self):
        self.srv.shutdown(); self.srv.server_close()

    def paths(self, p):
        return [r for r in self.srv.requests if r[0] == p]

    def browser(self, mode="ok"):
        def open_url(url):
            self.opened += 1
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
            self.params = q
            self.srv.challenge = q["code_challenge"]

            def go():
                extra = {"error": "access_denied"} if mode == "denied" else {"code": CODE}
                extra["state"] = "SBAGLIATO" if mode == "badstate" else q["state"]
                NOPROXY.open(q["redirect_uri"] + "?" + urllib.parse.urlencode(extra), timeout=5).read()
            if mode != "none":
                threading.Thread(target=go, daemon=True).start()
            return True
        return open_url


def post(url, data, timeout=30):      # POST senza proxy per i test
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    try:
        with NOPROXY.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


CW = A.ALLOWED_SCOPES["coursework"]
SUB = A.ALLOWED_SCOPES["submissions"]
ALL_BUT_CW = [v for v in A.ALLOWED_SCOPES.values() if v != CW]       # cio' che Google restituisce davvero


class FakeClassroom:
    """Transport finto per cls_client: registra le richieste; `cw_status`/`courses` simulano l'esito di courseWork.list."""

    def __init__(self, cw_status=200, courses=({"id": "C1"},), archived=(), courses_status=200, raise_exc=None):
        self.calls, self.cw_status, self.courses, self.archived = [], cw_status, list(courses), list(archived)
        self.courses_status, self.raise_exc = courses_status, raise_exc

    def __call__(self, method, url, headers, timeout=30):
        self.calls.append((method, url, headers.get("Authorization")))
        if self.raise_exc:
            raise self.raise_exc
        u = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(u.query)
        if u.path.endswith("/courses"):
            if self.courses_status != 200:
                return self.courses_status, {"error": {"status": "PERMISSION_DENIED", "message": "no"}}, {}
            return 200, {"courses": self.courses if q.get("courseStates") == ["ACTIVE"] else self.archived}, {}
        if u.path.endswith("/courseWork"):
            if self.cw_status != 200:
                return self.cw_status, {"error": {"code": self.cw_status, "status": "PERMISSION_DENIED",
                                                  "message": "Request had insufficient authentication scopes.",
                                                  "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}, {}
            return 200, {"courseWork": []}, {}
        return 404, None, {}

    def cw_calls(self):
        return [c for c in self.calls if urllib.parse.urlparse(c[1]).path.endswith("/courseWork")]


class Provider(unittest.TestCase):
    def setUp(self):
        self.m = Mock(); self.addCleanup(self.m.close)
        self.kc = FakeKeychain(); self.kc.d[(A.TOKEN_SERVICE, "refresh_token")] = RT
        self.now = [0.0]

    def prov(self):
        return A.token_provider(self.kc, post=post, endpoints=self.m.endpoints, clock=lambda: self.now[0])

    def test_refreshes_and_caches_in_memory(self):
        p = self.prov()
        self.assertEqual(p(), AT)
        self.assertEqual(p(), AT)
        self.assertEqual(len(self.m.paths("/token")), 1)
        self.now[0] = 4000
        p()
        self.assertEqual(len(self.m.paths("/token")), 2)
        p(True)
        self.assertEqual(len(self.m.paths("/token")), 3)

    def test_access_token_never_stored_in_keychain(self):
        self.prov()()
        self.assertNotIn(AT, json.dumps({str(k): v for k, v in self.kc.d.items()}))

    def test_missing_refresh_token(self):
        del self.kc.d[(A.TOKEN_SERVICE, "refresh_token")]
        with self.assertRaises(A.AuthError) as cm:
            self.prov()()
        self.assertEqual(cm.exception.kind, "no_refresh_token")
        self.assertEqual(self.m.srv.requests, [])

    def test_invalid_grant_explains_what_to_do_without_leaking(self):
        self.kc.d[(A.TOKEN_SERVICE, "refresh_token")] = "REVOCATO"
        with self.assertRaises(A.AuthError) as cm:
            self.prov()()
        self.assertEqual(cm.exception.kind, "invalid_grant")
        self.assertIn("authorize", str(cm.exception))
        self.assertNotIn("REVOCATO", str(cm.exception))
        self.assertNotIn(CSEC, str(cm.exception))

    def test_rotated_refresh_token_is_stored(self):
        self.m.srv.rotate = True
        self.prov()()
        self.assertEqual(self.kc.d[(A.TOKEN_SERVICE, "refresh_token")], "RT-NUOVO-9")

    def test_missing_client(self):
        self.kc = FakeKeychain(with_client=False); self.kc.d[(A.TOKEN_SERVICE, "refresh_token")] = RT
        with self.assertRaises(A.AuthError) as cm:
            self.prov()()
        self.assertEqual(cm.exception.kind, "no_client")


class Authorize(unittest.TestCase):
    def go(self, mode="ok", kc=None, names=tuple(A.ALLOWED_SCOPES), mock_kw=None, timeout=10, api=None):
        m = Mock(**(mock_kw or {})); self.addCleanup(m.close)
        kc = kc or FakeKeychain()
        self.api = api or FakeClassroom()
        lines = []
        res = A.authorize("utente@scuola.example", names, keychain=kc, out=lines.append, open_url=m.browser(mode),
                          timeout=timeout, post=post, endpoints=m.endpoints, transport=self.api)
        return m, kc, res, "\n".join(lines)

    def test_success_stores_refresh_token_only_and_leaks_nothing(self):
        m, kc, res, out = self.go()
        self.assertTrue(res["ok"])
        self.assertEqual(kc.d[(A.TOKEN_SERVICE, "refresh_token")], RT)
        for s in (AT, RT, CODE, CSEC):
            self.assertNotIn(s, out)
            self.assertNotIn(s, json.dumps(res))
        self.assertNotIn((A.TOKEN_SERVICE, "preflight"), kc.d)           # prova rimossa

    def test_auth_url_is_offline_readonly_pkce(self):
        m, _, _, _ = self.go()
        q = m.params
        self.assertEqual(sorted(q["scope"].split()), sorted(A.ALLOWED_SCOPES.values()))
        self.assertEqual((q["access_type"], q["prompt"], q["code_challenge_method"]), ("offline", "consent", "S256"))
        self.assertNotIn("include_granted_scopes", q)
        self.assertEqual(q["login_hint"], "utente@scuola.example")

    def test_subset_of_scopes(self):
        m, kc, res, _ = self.go(names=("courses",), mock_kw={"scope": [A.ALLOWED_SCOPES["courses"]]})
        self.assertTrue(res["ok"])
        self.assertEqual(m.params["scope"], A.ALLOWED_SCOPES["courses"])

    def test_unknown_or_write_scope_rejected_before_browser(self):
        m = Mock(); self.addCleanup(m.close)
        with self.assertRaises(A.AuthError):
            A.authorize("a@b.c", ["courses", "drive"], keychain=FakeKeychain(), out=lambda s: None,
                        open_url=m.browser(), post=post, endpoints=m.endpoints)
        self.assertEqual(m.opened, 0)

    def test_keychain_preflight_failure_stops_before_consent(self):
        m, kc, res, out = self.go(kc=FakeKeychain(fail_get_after_set=True))
        self.assertEqual(res["outcome"], "keychain_error")
        self.assertEqual(m.opened, 0)
        self.assertIn("nulla e' stato autorizzato", out)

    def test_missing_client_stops_before_consent(self):
        m, _, res, _ = self.go(kc=FakeKeychain(with_client=False))
        self.assertEqual(res["outcome"], "no_client")
        self.assertEqual(m.opened, 0)

    def test_consent_denied_and_state_mismatch_store_nothing(self):
        for mode, outcome in (("denied", "consent_error"), ("badstate", "state_mismatch")):
            m, kc, res, _ = self.go(mode)
            self.assertEqual(res["outcome"], outcome)
            self.assertNotIn((A.TOKEN_SERVICE, "refresh_token"), kc.d)
            self.assertEqual(m.paths("/token"), [])

    def test_extra_scope_granted_revokes_and_stores_nothing(self):
        m, kc, res, _ = self.go(mock_kw={"scope": list(A.ALLOWED_SCOPES.values()) + ["https://www.googleapis.com/auth/drive.readonly"]})
        self.assertEqual(res["outcome"], "scope_mismatch")
        self.assertNotIn((A.TOKEN_SERVICE, "refresh_token"), kc.d)
        self.assertEqual(m.paths("/revoke")[0][1]["token"], RT)

    def test_fewer_scopes_than_requested_is_mismatch(self):
        m, kc, res, _ = self.go(mock_kw={"scope": [A.ALLOWED_SCOPES["courses"]]})
        self.assertEqual(res["outcome"], "scope_mismatch")
        self.assertNotIn((A.TOKEN_SERVICE, "refresh_token"), kc.d)

    # ---- scope equivalenti (alias) ------------------------------------------------------------------
    def test_alias_google_returns_submissions_for_coursework_is_accepted(self):
        m, kc, res, out = self.go(mock_kw={"scope": ALL_BUT_CW})            # caso reale del 2026-10-31
        self.assertTrue(res["ok"], out)
        self.assertEqual(kc.d[(A.TOKEN_SERVICE, "refresh_token")], RT)
        self.assertIn("alias equivalente", out)
        self.assertEqual(m.paths("/revoke"), [])

    def test_alias_only_coursework_requested_submissions_returned(self):
        m, kc, res, out = self.go(names=("courses", "coursework"),
                                  mock_kw={"scope": [A.ALLOWED_SCOPES["courses"], SUB]})
        self.assertTrue(res["ok"], out)

    def test_compare_scopes_alias_both_directions_and_strict_otherwise(self):
        self.assertEqual(A.compare_scopes([CW, SUB], [SUB]), ([], []))
        self.assertEqual(A.compare_scopes([CW], [SUB]), ([], []))
        self.assertEqual(A.compare_scopes([SUB], [CW]), ([], []))
        T = A.ALLOWED_SCOPES["topics"]
        self.assertEqual(A.compare_scopes([CW, T], [SUB]), ([T], []))
        self.assertEqual(A.compare_scopes([T], [T, SUB]), ([], [SUB]))         # alias NON richiesto = extra

    # ---- scope mancanti reali / extra ---------------------------------------------------------------
    def test_real_missing_scope_with_alias_present_still_mismatch(self):
        got = [s for s in ALL_BUT_CW if s != A.ALLOWED_SCOPES["announcements"]]
        m, kc, res, out = self.go(mock_kw={"scope": got})
        self.assertEqual(res["outcome"], "scope_mismatch")
        self.assertEqual(res["missing"], [A.ALLOWED_SCOPES["announcements"]])
        self.assertNotIn((A.TOKEN_SERVICE, "refresh_token"), kc.d)
        self.assertEqual(m.paths("/revoke")[0][1]["token"], RT)
        self.assertEqual(self.api.calls, [])                               # nessuna chiamata API se gli scope non tornano

    def test_both_aliases_missing_is_mismatch(self):
        got = [s for s in A.ALLOWED_SCOPES.values() if s not in (CW, SUB)]
        _, kc, res, _ = self.go(mock_kw={"scope": got})
        self.assertEqual(res["outcome"], "scope_mismatch")
        self.assertEqual(res["missing"], sorted([CW, SUB]))
        self.assertNotIn((A.TOKEN_SERVICE, "refresh_token"), kc.d)

    def test_unrequested_alias_returned_is_extra(self):
        m, kc, res, _ = self.go(names=("courses",), mock_kw={"scope": [A.ALLOWED_SCOPES["courses"], SUB]})
        self.assertEqual(res["outcome"], "scope_mismatch")
        self.assertEqual(res["extra"], [SUB])
        self.assertEqual(m.paths("/revoke")[0][1]["token"], RT)

    def test_extra_write_scope_alongside_alias_revokes(self):
        m, kc, res, _ = self.go(mock_kw={"scope": ALL_BUT_CW + ["https://www.googleapis.com/auth/classroom.coursework.me"]})
        self.assertEqual(res["outcome"], "scope_mismatch")
        self.assertEqual(res["extra"], ["https://www.googleapis.com/auth/classroom.coursework.me"])
        self.assertNotIn((A.TOKEN_SERVICE, "refresh_token"), kc.d)

    # ---- verifica reale courseWork.list -------------------------------------------------------------
    def test_coursework_check_makes_get_with_new_access_token(self):
        m, kc, res, out = self.go(mock_kw={"scope": ALL_BUT_CW})
        self.assertTrue(res["ok"])
        self.assertIn("courseWork.list OK", out)
        cw = self.api.cw_calls()
        self.assertEqual(len(cw), 1)
        self.assertEqual(cw[0][0], "GET")
        self.assertEqual(cw[0][2], "Bearer " + AT)
        self.assertIn("/courses/C1/courseWork", cw[0][1])
        self.assertTrue(all(c[0] == "GET" for c in self.api.calls))
        self.assertNotIn(AT, out)

    def test_coursework_check_uses_archived_course_if_no_active(self):
        _, _, res, _ = self.go(api=FakeClassroom(courses=(), archived=({"id": "OLD"},)))
        self.assertTrue(res["ok"])
        self.assertIn("/courses/OLD/courseWork", self.api.cw_calls()[0][1])

    def test_coursework_check_skipped_when_not_requested(self):
        _, _, res, _ = self.go(names=("courses",), mock_kw={"scope": [A.ALLOWED_SCOPES["courses"]]})
        self.assertTrue(res["ok"])
        self.assertEqual(self.api.calls, [])

    def test_coursework_check_order_scope_then_rt_then_api_then_keychain(self):
        _, kc, _, _ = self.go(mock_kw={"scope": ALL_BUT_CW, "with_rt": False})
        self.assertEqual(self.api.calls, [])                               # senza refresh token non si chiama l'API
        self.assertNotIn(("set", A.TOKEN_SERVICE, "refresh_token"), kc.log)

    # ---- revoca in caso di fallimento della verifica ------------------------------------------------
    def _assert_check_failed_revoked(self, m, kc, res, out):
        self.assertEqual(res["outcome"], "coursework_check_failed", out)
        self.assertFalse(res["ok"])
        self.assertNotIn((A.TOKEN_SERVICE, "refresh_token"), kc.d)
        self.assertNotIn(("set", A.TOKEN_SERVICE, "refresh_token"), kc.log)
        rv = m.paths("/revoke")
        self.assertEqual(len(rv), 1)
        self.assertEqual(rv[0][1]["token"], RT)
        for s in (AT, RT, CODE, CSEC):
            self.assertNotIn(s, out)
            self.assertNotIn(s, json.dumps(res))

    def test_coursework_403_scope_insufficient_revokes_and_stores_nothing(self):
        m, kc, res, out = self.go(mock_kw={"scope": ALL_BUT_CW}, api=FakeClassroom(cw_status=403))
        self._assert_check_failed_revoked(m, kc, res, out)
        self.assertIn("scope_insufficient", res["detail"])

    def test_coursework_5xx_after_retries_revokes(self):
        m, kc, res, out = self.go(mock_kw={"scope": ALL_BUT_CW}, api=FakeClassroom(cw_status=503))
        self._assert_check_failed_revoked(m, kc, res, out)

    def test_no_courses_cannot_verify_revokes(self):
        m, kc, res, out = self.go(api=FakeClassroom(courses=()))
        self._assert_check_failed_revoked(m, kc, res, out)
        self.assertEqual(self.api.cw_calls(), [])

    def test_courses_list_error_revokes(self):
        m, kc, res, out = self.go(api=FakeClassroom(courses_status=403))
        self._assert_check_failed_revoked(m, kc, res, out)

    def test_network_error_during_check_revokes(self):
        m, kc, res, out = self.go(api=FakeClassroom(raise_exc=OSError("rete giu'")))
        self._assert_check_failed_revoked(m, kc, res, out)
        self.assertIn("OSError", res["detail"])

    def test_no_refresh_token_returned(self):
        m, kc, res, _ = self.go(mock_kw={"with_rt": False})
        self.assertEqual(res["outcome"], "no_refresh_token")
        self.assertEqual(len(m.paths("/revoke")), 1)

    def test_keychain_write_failure_revokes(self):
        m, kc, res, _ = self.go(kc=FakeKeychain(fail_set=True))
        self.assertEqual(res["outcome"], "keychain_error")
        self.assertEqual(m.paths("/revoke")[0][1]["token"], RT)

    def test_timeout(self):
        _, _, res, out = self.go("none", timeout=1)
        self.assertEqual(res["outcome"], "timeout")
        self.assertIn("Accesso bloccato", out)


class StatusRevoke(unittest.TestCase):
    def test_status_online_reports_scopes_not_tokens(self):
        m = Mock(); self.addCleanup(m.close)
        kc = FakeKeychain(); kc.d[(A.TOKEN_SERVICE, "refresh_token")] = RT
        lines = []
        res = A.status(kc, online=True, out=lines.append, post=post, endpoints=m.endpoints)
        self.assertEqual(sorted(res["scope"]), sorted(A.ALLOWED_SCOPES.values()))
        self.assertNotIn(AT, "\n".join(lines)); self.assertNotIn(RT, "\n".join(lines))

    def test_status_offline_makes_no_network_calls(self):
        m = Mock(); self.addCleanup(m.close)
        A.status(FakeKeychain(), online=False, out=lambda s: None, post=post, endpoints=m.endpoints)
        self.assertEqual(m.srv.requests, [])

    def test_revoke_deletes_keychain_item(self):
        m = Mock(); self.addCleanup(m.close)
        kc = FakeKeychain(); kc.d[(A.TOKEN_SERVICE, "refresh_token")] = RT
        res = A.revoke(kc, out=lambda s: None, post=post, endpoints=m.endpoints)
        self.assertTrue(res["ok"])
        self.assertNotIn((A.TOKEN_SERVICE, "refresh_token"), kc.d)
        self.assertEqual(m.paths("/revoke")[0][1]["token"], RT)


class MacKeychainCommands(unittest.TestCase):
    class R:
        def __init__(self, rc=0, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    def test_set_uses_stdin_not_argv(self):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw)); return self.R()
        A.MacKeychain(run).set("svc", "acct", 'val"ue\\x')
        argv, kw = calls[0]
        self.assertEqual(argv, ["security", "-i"])
        self.assertNotIn("val", " ".join(argv))
        self.assertIn('-w "val\\"ue\\\\x"', kw["input"])
        self.assertIn("add-generic-password -U", kw["input"])

    def test_set_rejects_newlines_and_reports_errors(self):
        with self.assertRaises(A.AuthError):
            A.MacKeychain(lambda *a, **k: self.R()).set("s", "a", "x\ny")
        with self.assertRaises(A.AuthError):
            A.MacKeychain(lambda *a, **k: self.R(1, "", "boom")).set("s", "a", "x")
        with self.assertRaises(A.AuthError):
            A.MacKeychain(lambda *a, **k: self.R(0, "", "security: error: x")).set("s", "a", "x")

    def test_get_and_missing(self):
        self.assertEqual(A.MacKeychain(lambda *a, **k: self.R(0, "tok\n")).get("s", "a"), "tok")
        self.assertIsNone(A.MacKeychain(lambda *a, **k: self.R(44)).get("s", "a"))

        def nofile(*a, **k):
            raise FileNotFoundError()
        self.assertIsNone(A.MacKeychain(nofile).get("s", "a"))


class StaticSafety(unittest.TestCase):
    def test_all_allowed_scopes_are_readonly_classroom(self):
        for s in A.ALLOWED_SCOPES.values():
            self.assertTrue(s.startswith("https://www.googleapis.com/auth/classroom."))
            self.assertTrue(s.endswith(".readonly"), s)
            for bad in ("rosters", "profile", "drive", "guardian", "push-notifications"):
                self.assertNotIn(bad, s)

    def test_aliases_are_only_the_documented_readonly_pair(self):
        self.assertEqual(A.SCOPE_ALIASES, ((CW, SUB),))
        for pair in A.SCOPE_ALIASES:
            for s in pair:
                self.assertIn(s, A.ALLOWED_SCOPES.values())

    def test_coursework_check_is_get_only(self):
        import inspect
        src = inspect.getsource(A.verify_coursework)
        for verb in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertNotIn(verb, src)

    def test_adapter_scopes_match_authorized_scopes(self):
        self.assertEqual(set(CL.SCOPES.values()), set(A.ALLOWED_SCOPES.values()))

    def test_no_secret_in_cli_arguments_or_prints(self):
        src = open(os.path.join(os.path.dirname(HERE), "cls_auth.py")).read()
        self.assertNotIn("print(rt", src)
        self.assertNotIn("print(tok", src)


if __name__ == "__main__":
    unittest.main()
