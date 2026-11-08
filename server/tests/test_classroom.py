"""/class: GitHub organisation, classes and student repos, against a fake GitHub (httpx.MockTransport)."""

import hashlib
import hmac
import io
import json
import zipfile
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import config, github
from tests.helpers import full_user, otp_login

ORG = "skole"
TEMPLATE = "skole/web-skabelon"
WEBHOOK_SECRET = "hook-secret"


class FakeGitHub:
    """Just enough of api.github.com and github.com/login/oauth for the classroom flow."""

    def __init__(self):
        self.users = {"mette-code": {"id": 101, "login": "mette"}, "sara-code": {"id": 202, "login": "sara-n"},
                      "ali-code": {"id": 303, "login": "alih"}}
        self.installations_for = {"mette": {42}}
        self.members: set[str] = {"mette"}
        self.invited: set[str] = set()
        self.repos: set[str] = set()
        self.fail_collaborator = False
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method, url, path = request.method, str(request.url), request.url.path
        self.calls.append((method, path))
        auth = request.headers.get("authorization", "")
        body = json.loads(request.content) if request.content and request.headers.get("content-type", "").startswith(
            "application/json") else {}

        if url.startswith("https://github.com/login/oauth/access_token"):
            code = parse_qs(request.content.decode())["code"][0]
            if code not in self.users:
                return httpx.Response(200, json={"error": "bad_verification_code", "error_description": "bad code"})
            return httpx.Response(200, json={"access_token": f"user-{code}"})
        assert url.startswith("https://api.github.com/"), url
        if path.startswith("/app/"):
            assert auth.startswith("Bearer ey"), "the app endpoints need the app JWT"
            if path == "/app/installations/42/access_tokens":
                return httpx.Response(201, json={"token": "inst-token", "expires_at": "2099-01-01T00:00:00Z"})
            if path == "/app/installations/42":
                return httpx.Response(200, json={"id": 42, "account": {"login": ORG, "type": "Organization"}})
            return httpx.Response(404, json={"message": "Not Found"})
        if path in ("/user", "/user/installations"):
            user = self.users[auth.removeprefix("Bearer user-")]
            if path == "/user":
                return httpx.Response(200, json=user)
            return httpx.Response(200, json={"installations": [{"id": i} for i in self.installations_for.get(user["login"], set())]})

        assert auth == "Bearer inst-token", f"{method} {path} must use the installation token"
        parts = path.strip("/").split("/")
        if path == f"/repos/{TEMPLATE}" and method == "GET":
            return httpx.Response(200, json={"full_name": TEMPLATE, "is_template": True})
        if path == f"/orgs/{ORG}/teams" and method == "POST":
            return httpx.Response(201, json={"slug": body["name"]})
        if parts[:2] == ["orgs", ORG] and parts[2] == "teams" and len(parts) == 6 and parts[4] == "memberships":
            login = parts[5]
            if method == "GET":
                if login in self.members:
                    return httpx.Response(200, json={"state": "active"})
                if login in self.invited:
                    return httpx.Response(200, json={"state": "pending"})
                return httpx.Response(404, json={"message": "Not Found"})
            if login not in self.members:
                self.invited.add(login)
            return httpx.Response(200, json={"state": "active" if login in self.members else "pending"})
        if path == f"/repos/{TEMPLATE}/generate" and method == "POST":
            assert body["owner"] == ORG and body["private"] is True
            if body["name"] in self.repos:
                return httpx.Response(422, json={"message": "Name already exists on this account"})
            self.repos.add(body["name"])
            return httpx.Response(201, json={"name": body["name"]})
        if parts[0] == "repos" and len(parts) == 5 and parts[3] == "collaborators" and method == "PUT":
            if self.fail_collaborator:
                return httpx.Response(500, json={"message": "boom"})
            assert body == {"permission": "admin"}
            return httpx.Response(204)
        if parts[:2] == ["orgs", ORG] and "repos" in parts and method == "PUT":
            assert body == {"permission": "maintain"}
            return httpx.Response(204)
        if parts[0] == "repos" and parts[-1] == "transfer" and method == "POST":
            return httpx.Response(202, json={})
        return httpx.Response(404, json={"message": f"unexpected {method} {path}"})


@pytest.fixture
def gh(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    for name, value in {"GITHUB_APP_ID": "1234", "GITHUB_APP_SLUG": "gaer-test", "GITHUB_APP_PRIVATE_KEY": pem,
                        "GITHUB_CLIENT_ID": "Iv1.test", "GITHUB_CLIENT_SECRET": "secret",
                        "GITHUB_WEBHOOK_SECRET": WEBHOOK_SECRET}.items():
        monkeypatch.setenv(name, value)
    config.reload()
    fake = FakeGitHub()
    monkeypatch.setattr(github, "transport", httpx.MockTransport(fake))
    github._tokens.clear()
    yield fake
    monkeypatch.undo()
    config.reload()
    github._tokens.clear()


def _state(location: str) -> str:
    return parse_qs(urlparse(location).query)["state"][0]


async def _connect_org(teacher) -> int:
    r = await teacher.post("/api/class/github/start", json={"purpose": "install"})
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("https://github.com/apps/gaer-test/installations/new?state=")
    r = await teacher.get("/api/class/github/setup?installation_id=42&setup_action=install")
    assert r.status_code == 303 and r.headers["location"].startswith("https://github.com/login/oauth/authorize?")
    r = await teacher.get(f"/api/class/github/callback?code=mette-code&state={_state(r.headers['location'])}")
    assert r.status_code == 303 and r.headers["location"] == f"/class/teacher?forbundet={ORG}", r.headers["location"]
    me = (await teacher.get("/api/class/me")).json()
    assert me["github"]["account"]["login"] == "mette"
    return me["orgs"][0]["id"]


async def _link(student, code: str) -> str:
    r = await student.post("/api/class/github/start", json={"purpose": "link"})
    r = await student.get(f"/api/class/github/callback?code={code}&state={_state(r.json()['url'])}")
    assert r.status_code == 303
    return r.headers["location"]


def _webhook(event: str, payload: dict) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, {"x-github-event": event, "x-hub-signature-256": sig, "content-type": "application/json"}


async def _status(student) -> dict:
    return (await student.get("/api/class/me")).json()["learning"][0]


async def test_teacher_connects_org_and_student_gets_own_repo(make_client, gh, owner_conn):
    teacher = make_client()
    await otp_login(teacher, "11111111")  # first user: admin, so also a teacher
    org_id = await _connect_org(teacher)

    r = await teacher.post("/api/class/classes", json={"org_id": org_id, "name": "9.B Webudvikling", "template_repo": TEMPLATE})
    assert r.status_code == 200, r.text
    klass = r.json()
    assert klass["slug"] == "9-b-webudvikling"
    assert ("POST", f"/orgs/{ORG}/teams") in gh.calls
    assert ("PUT", f"/orgs/{ORG}/teams/9-b-webudvikling-laerere/memberships/mette") in gh.calls

    student = make_client()
    await otp_login(student, "22222222")
    r = await student.post("/api/class/join", json={"code": klass["join_code"].replace("-", "").lower()})
    assert r.status_code == 200 and r.json()["joined"] is True
    assert (await _status(student))["status"] == "waiting_account"

    assert await _link(student, "sara-code") == "/class/student?github=forbundet"
    mine = await _status(student)
    assert mine["status"] == "invited" and mine["repo_url"] is None
    assert mine["invite_url"] == f"https://github.com/orgs/{ORG}/invitation"
    assert "sara-n" in gh.invited and "9-b-webudvikling-sara-n" not in gh.repos

    # The student accepts on GitHub; the organisation webhook moves the repo forward.
    gh.members.add("sara-n")
    body, headers = _webhook("organization", {"action": "member_added", "installation": {"id": 42},
                                              "membership": {"user": {"login": "sara-n"}}})
    r = await make_client().post("/api/public/github/webhook", content=body, headers=headers)
    assert r.status_code == 200 and r.json()["students"] == 1
    mine = await _status(student)
    assert mine["status"] == "ready"
    assert mine["repo_url"] == f"https://github.com/{ORG}/9-b-webudvikling-sara-n"
    assert ("PUT", f"/repos/{ORG}/9-b-webudvikling-sara-n/collaborators/sara-n") in gh.calls
    assert ("PUT", f"/orgs/{ORG}/teams/9-b-webudvikling-laerere/repos/{ORG}/9-b-webudvikling-sara-n") in gh.calls

    detail = (await teacher.get(f"/api/class/classes/{klass['id']}")).json()
    assert [(s["github_login"], s["status"]) for s in detail["students"]] == [("sara-n", "ready")]
    overview = (await teacher.get("/api/class/me")).json()["teaching"][0]
    assert (overview["students"], overview["ready"]) == (1, 1)

    sid = detail["students"][0]["user_id"]
    r = await teacher.post(f"/api/class/classes/{klass['id']}/students/{sid}/transfer")
    assert r.status_code == 200, r.text
    assert (await _status(student))["status"] == "transferred"

    statuses = [r[0] for r in owner_conn.execute(
        "select after->>'status' from audit_log where table_name = 'student_repos' order by id").fetchall()]
    assert statuses == ["pending", "invited", "created", "ready", "transferred"]
    tables = {r[0] for r in owner_conn.execute("select table_name from audit_log where action = 'create'").fetchall()}
    assert {"github_orgs", "github_org_teachers", "github_accounts", "classes", "class_members"} <= tables


async def test_student_already_linked_is_provisioned_on_join(make_client, gh):
    teacher = make_client()
    await otp_login(teacher, "11111111")
    org_id = await _connect_org(teacher)
    klass = (await teacher.post("/api/class/classes", json={"org_id": org_id, "name": "Spil", "template_repo": TEMPLATE})).json()
    student = make_client()
    await otp_login(student, "22222222")
    await _link(student, "sara-code")
    gh.members.add("sara-n")
    await student.post("/api/class/join", json={"code": klass["join_code"]})
    assert (await _status(student))["status"] == "ready"


async def test_failures_are_recorded_and_retried(make_client, gh):
    teacher = make_client()
    await otp_login(teacher, "11111111")
    org_id = await _connect_org(teacher)
    klass = (await teacher.post("/api/class/classes", json={"org_id": org_id, "name": "Spil", "template_repo": TEMPLATE})).json()
    student = make_client()
    await otp_login(student, "22222222")
    await _link(student, "sara-code")
    gh.members.add("sara-n")
    gh.repos.add("spil-sara-n")  # a repo with that name exists already
    await student.post("/api/class/join", json={"code": klass["join_code"]})
    mine = await _status(student)
    assert mine["status"] == "failed" and "findes allerede" in mine["last_error"]

    gh.repos.clear()
    gh.fail_collaborator = True  # the repo is created, then setting rights fails
    assert (await teacher.post(f"/api/class/classes/{klass['id']}/provision")).json() == {"queued": 1}
    assert (await _status(student))["status"] == "failed"

    gh.fail_collaborator = False
    generated = sum(1 for c in gh.calls if c == ("POST", f"/repos/{TEMPLATE}/generate"))
    await teacher.post(f"/api/class/classes/{klass['id']}/provision")
    assert (await _status(student))["status"] == "ready"
    assert sum(1 for c in gh.calls if c == ("POST", f"/repos/{TEMPLATE}/generate")) == generated  # not generated twice


async def test_only_teachers_and_only_their_classes(make_client, gh, owner_conn):
    teacher = make_client()
    await full_user(teacher, "11111111")  # granting teacher is a role change: needs a second factor
    org_id = await _connect_org(teacher)
    klass = (await teacher.post("/api/class/classes", json={"org_id": org_id, "name": "Spil", "template_repo": TEMPLATE})).json()

    other = make_client()
    await otp_login(other, "22222222")
    assert (await other.post("/api/class/github/start", json={"purpose": "install"})).status_code == 403
    assert (await other.post("/api/class/classes", json={"org_id": org_id, "name": "X", "template_repo": TEMPLATE})).status_code == 403
    assert (await other.get(f"/api/class/classes/{klass['id']}")).status_code == 404
    await other.post("/api/class/join", json={"code": klass["join_code"]})
    assert (await other.get(f"/api/class/classes/{klass['id']}")).status_code == 404  # a student is not a teacher
    assert (await other.post(f"/api/class/classes/{klass['id']}/provision")).status_code == 404

    # An admin grants the teacher capability; a teacher still only sees organisations they proved access to.
    uid = owner_conn.execute("select id from users where phone = '+4522222222'").fetchone()[0]
    assert (await teacher.put(f"/api/admin/users/{uid}/teacher")).status_code == 200
    me = (await other.get("/api/class/me")).json()
    assert me["is_teacher"] is True and me["orgs"] == []
    assert (await other.post("/api/class/classes", json={"org_id": org_id, "name": "X", "template_repo": TEMPLATE})).status_code == 404
    users = (await teacher.get("/api/admin/users")).json()["items"]
    assert next(u for u in users if u["id"] == uid)["is_teacher"] is True
    assert owner_conn.execute("select count(*) from audit_log where action = 'role.changed' and table_name = 'teachers'").fetchone()[0] == 1
    assert (await other.put(f"/api/admin/users/{uid}/teacher")).status_code == 403


async def test_install_needs_proof_of_access(make_client, gh):
    teacher = make_client()
    await otp_login(teacher, "11111111")
    gh.installations_for = {}  # mette cannot reach installation 42
    r = await teacher.get("/api/class/github/setup?installation_id=42")
    r = await teacher.get(f"/api/class/github/callback?code=mette-code&state={_state(r.headers['location'])}")
    assert r.headers["location"] == "/class/teacher?fejl=ingen-adgang"
    assert (await teacher.get("/api/class/me")).json()["orgs"] == []


async def test_state_is_bound_to_the_user(make_client, gh):
    a, b = make_client(), make_client()
    await otp_login(a, "11111111")
    await otp_login(b, "22222222")
    url = (await a.post("/api/class/github/start", json={"purpose": "link"})).json()["url"]
    r = await b.get(f"/api/class/github/callback?code=sara-code&state={_state(url)}")
    assert r.headers["location"] == "/class?fejl=state"
    r = await b.get("/api/class/github/callback?code=sara-code&state=forged.abc")
    assert r.headers["location"] == "/class?fejl=state"
    assert (await b.get("/api/class/me")).json()["github"]["account"] is None


async def test_one_github_account_per_user(make_client, gh):
    a, b = make_client(), make_client()
    await otp_login(a, "11111111")
    await otp_login(b, "22222222")
    await _link(a, "sara-code")
    assert await _link(b, "sara-code") == "/class/student?fejl=konto-i-brug"


async def test_webhook_needs_signature(client, gh):
    body, headers = _webhook("ping", {})
    assert (await client.post("/api/public/github/webhook", content=body, headers=headers)).status_code == 200
    headers["x-hub-signature-256"] = "sha256=" + "0" * 64
    assert (await client.post("/api/public/github/webhook", content=body, headers=headers)).status_code == 401


async def test_uninstall_disconnects_org(make_client, gh, owner_conn):
    teacher = make_client()
    await otp_login(teacher, "11111111")
    await _connect_org(teacher)
    body, headers = _webhook("installation", {"action": "deleted", "installation": {"id": 42}})
    await make_client().post("/api/public/github/webhook", content=body, headers=headers)
    assert (await teacher.get("/api/class/me")).json()["orgs"][0]["disconnected_at"] is not None


async def test_not_configured(client):
    await otp_login(client, "11111111")
    me = (await client.get("/api/class/me")).json()
    assert me["github"] == {"configured": False, "account": None} and me["is_teacher"] is True
    assert (await client.post("/api/class/github/start", json={"purpose": "link"})).status_code == 503


async def test_gdpr_export_and_erasure(make_client, gh, owner_conn):
    teacher = make_client()
    await otp_login(teacher, "11111111")
    org_id = await _connect_org(teacher)
    klass = (await teacher.post("/api/class/classes", json={"org_id": org_id, "name": "Spil", "template_repo": TEMPLATE})).json()
    student = make_client()
    await otp_login(student, "22222222")
    await _link(student, "sara-code")
    await student.post("/api/class/join", json={"code": klass["join_code"]})

    r = await student.get("/api/me/export")
    data = json.loads(zipfile.ZipFile(io.BytesIO(r.content)).read("data.json"))
    assert data["github_account"][0]["login"] == "sara-n"
    assert data["classes"][0]["name"] == "Spil" and data["classes"][0]["status"] == "invited"

    assert (await student.post("/api/me/delete", json={"confirm_phone": "22222222"})).status_code == 200
    assert owner_conn.execute("select login from github_accounts").fetchall() == [("mette",)]
    assert owner_conn.execute("select count(*) from student_repos").fetchone()[0] == 0
    assert owner_conn.execute("select count(*) from class_members where role = 'student'").fetchone()[0] == 0
