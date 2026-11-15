"""Dati SINTETICI (inventati, nessun dato reale) con la forma dei record documentati dalla Classroom API v1,
e un 'Classroom finto' in memoria (solo GET) per testare client, adattatore, dry-run e sync senza rete."""
import copy
import urllib.parse

TOKEN = "TOKEN-SINTETICO-NON-REALE"

COURSE_A = {"id": "1001", "name": "Matematica SINTETICO 3A", "section": "3A", "room": "Aula 1", "ownerId": "9001",
            "creationTime": "2026-09-29T07:00:00.000Z", "updateTime": "2026-10-08T08:00:00.000Z",
            "enrollmentCode": "synth1", "courseState": "ACTIVE", "alternateLink": "https://classroom.example/c/1001"}
COURSE_B = {"id": "1002", "name": "Storia SINTETICO 2A", "section": "2A", "ownerId": "9002",
            "creationTime": "2025-09-01T07:00:00.000Z", "updateTime": "2026-06-10T08:00:00.000Z",
            "courseState": "ARCHIVED", "alternateLink": "https://classroom.example/c/1002"}
COURSE_C = {"id": "1003", "name": "Corso rifiutato SINTETICO", "ownerId": "9003",
            "creationTime": "2026-09-29T07:00:00.000Z", "courseState": "DECLINED"}

WORK = [
    {"courseId": "1001", "id": "w1", "title": "Esercizi cap. 3 (SINT.)", "description": "Fare 1-10", "state": "PUBLISHED",
     "workType": "ASSIGNMENT", "maxPoints": 10, "creationTime": "2026-10-29T09:00:00Z", "updateTime": "2026-10-29T09:00:00Z",
     "dueDate": {"year": 2026, "month": 11, "day": 27}, "dueTime": {"hours": 22, "minutes": 59},
     "alternateLink": "https://classroom.example/w1", "topicId": "t1",
     "materials": [{"driveFile": {"driveFile": {"id": "d1", "title": "dispensa.pdf", "alternateLink": "https://drive.example/d1",
                                                "thumbnailUrl": "https://thumb.example/d1"}, "shareMode": "VIEW"}},
                   {"link": {"url": "https://example.org/x", "title": "Link utile"}}]},
    {"courseId": "1001", "id": "w2", "title": "Verifica scritta (SINT.)", "state": "PUBLISHED", "workType": "ASSIGNMENT",
     "creationTime": "2026-10-30T09:00:00Z", "updateTime": "2026-10-30T09:00:00Z",
     "dueDate": {"year": 2026, "month": 6, "day": 5}, "dueTime": {"hours": 21, "minutes": 59}},
    {"courseId": "1001", "id": "w3", "title": "Domanda aperta (SINT.)", "state": "PUBLISHED",
     "workType": "SHORT_ANSWER_QUESTION", "creationTime": "2026-10-31T09:00:00Z", "updateTime": "2026-10-31T09:00:00Z"},
    {"courseId": "1002", "id": "w9", "title": "Ricerca (SINT.)", "state": "PUBLISHED", "workType": "ASSIGNMENT",
     "creationTime": "2026-03-03T09:00:00Z", "updateTime": "2026-03-03T09:00:00Z", "dueDate": {"year": 2026, "month": 3, "day": 20}},
]
MATERIALS = [
    {"courseId": "1001", "id": "m1", "title": "Slide limiti (SINT.)", "state": "PUBLISHED",
     "creationTime": "2026-10-18T09:00:00Z", "updateTime": "2026-10-18T09:00:00Z",
     "materials": [{"youtubeVideo": {"id": "yt1", "title": "Video", "alternateLink": "https://youtu.example/yt1"}},
                   {"form": {"formUrl": "https://forms.example/f1", "title": "Quiz"}}]},
    {"courseId": "1002", "id": "m2", "title": "Fonti (SINT.)", "state": "PUBLISHED",
     "creationTime": "2026-02-20T09:00:00Z", "updateTime": "2026-02-20T09:00:00Z"},
]
ANNOUNCEMENTS = [
    {"courseId": "1001", "id": "a1", "text": "Domani niente lezione (SINT.)", "state": "PUBLISHED",
     "creationTime": "2026-10-29T10:00:00Z", "updateTime": "2026-10-29T10:00:00Z"},
    {"courseId": "1001", "id": "a2", "text": "Ricordo la verifica (SINT.)", "state": "PUBLISHED",
     "creationTime": "2026-10-30T10:00:00Z", "updateTime": "2026-10-30T10:00:00Z"},
    {"courseId": "1001", "id": "a3", "text": "Materiale caricato (SINT.)", "state": "PUBLISHED",
     "creationTime": "2026-10-31T10:00:00Z", "updateTime": "2026-10-31T10:00:00Z"},
]
TOPICS = [{"courseId": "1001", "topicId": "t1", "name": "Limiti (SINT.)", "updateTime": "2026-10-18T09:00:00Z"}]
SUBMISSIONS = [
    {"courseId": "1001", "courseWorkId": "w1", "id": "s1", "userId": "5001", "state": "TURNED_IN", "late": False,
     "courseWorkType": "ASSIGNMENT", "creationTime": "2026-11-02T09:00:00Z", "updateTime": "2026-11-02T09:30:00Z"},
    {"courseId": "1001", "courseWorkId": "w3", "id": "s2", "userId": "5001", "state": "RETURNED", "late": True,
     "courseWorkType": "SHORT_ANSWER_QUESTION", "assignedGrade": 7, "creationTime": "2026-11-03T09:00:00Z",
     "updateTime": "2026-11-04T09:30:00Z"},
]


class FakeClassroom:
    """Server finto: solo GET, paginazione reale, errori iniettabili. `requests` registra (metodo, path, query, auth)."""

    def __init__(self, page=2):
        self.page = page
        self.requests = []
        self.sleeps = []
        self.fail = []              # lista di (substring_path, status, payload, volte)
        self.refreshes = []
        self.data = {
            "courses": [COURSE_A, COURSE_B, COURSE_C],
            "courseWork": WORK, "courseWorkMaterials": MATERIALS, "announcements": ANNOUNCEMENTS,
            "topics": TOPICS, "studentSubmissions": SUBMISSIONS}
        self.token_valid = TOKEN

    # token provider da passare al client
    def token_provider(self, force=False):
        if force:
            self.refreshes.append(1)
        return TOKEN

    def sleep(self, s):
        self.sleeps.append(s)

    def transport(self, method, url, headers):
        u = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(u.query)
        self.requests.append((method, u.path, q, headers.get("Authorization")))
        for f in self.fail:
            if f[0] in u.path and f[3] > 0:
                f[3] -= 1
                return f[1], f[2], f[4] if len(f) > 4 else {}
        if method != "GET":
            return 405, {"error": {"code": 405, "message": "solo GET"}}, {}
        if headers.get("Authorization") != "Bearer " + self.token_valid:
            return 401, {"error": {"code": 401, "status": "UNAUTHENTICATED", "message": "token"}}, {}
        parts = u.path.strip("/").split("/")          # v1 courses [id] kind ...
        if len(parts) == 2:
            key, items = "courses", self.data["courses"]
            if "courseStates" in q:
                items = [c for c in items if c.get("courseState") in q["courseStates"]]
            else:
                items = [c for c in items if c.get("courseState") == "ACTIVE"]
        else:
            cid = parts[2]
            key = "studentSubmissions" if len(parts) > 4 else parts[3]
            resp_key = {"courseWorkMaterials": "courseWorkMaterial", "topics": "topic"}.get(key, key)
            items = [r for r in self.data[key] if r["courseId"] == cid]
            if key == "studentSubmissions":
                assert q.get("userId") == ["me"]
            tok = (q.get("pageToken") or ["0"])[0]
            i = int(tok)
            chunk = items[i:i + self.page]
            body = {resp_key: copy.deepcopy(chunk)}
            if i + self.page < len(items):
                body["nextPageToken"] = str(i + self.page)
            return 200, body, {}
        tok = (q.get("pageToken") or ["0"])[0]
        i = int(tok)
        chunk = items[i:i + self.page]
        body = {key: copy.deepcopy(chunk)}
        if i + self.page < len(items):
            body["nextPageToken"] = str(i + self.page)
        return 200, body, {}
