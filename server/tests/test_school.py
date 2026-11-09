"""/school/admin: school administrators, teachers by e-mail, students, seats, usage, add-ons and EAN invoicing."""

import pytest

from app import jobs, school
from tests.helpers import full_user, otp_login


async def email_login(client, email: str) -> dict:
    r = await client.post("/api/auth/otp/start", json={"email": email})
    assert r.status_code == 200, r.text
    r = await client.post("/api/auth/otp/verify", json={"email": email, "code": r.json()["dev_code"]})
    assert r.status_code == 200, r.text
    return r.json()


def _class(owner_conn, name: str = "9.B", code: str = "ABCD-2345") -> int:
    """A class without GitHub calls: the organisation and class rows are written directly."""
    org = owner_conn.execute("""insert into github_orgs (installation_id, login) values (42, 'skole')
                                on conflict (installation_id) do update set login = excluded.login returning id""").fetchone()[0]
    return owner_conn.execute("""insert into classes (org_id, name, slug, join_code, template_repo, student_team, teacher_team)
                                 values (%s, %s, %s, %s, 'skole/tpl', 's', 's-laerere') returning id""",
                              (org, name, name.lower().replace(".", ""), code)).fetchone()[0]


def _uid(owner_conn, email: str) -> int:
    return owner_conn.execute("select id from users where email = %s", (email,)).fetchone()[0]


@pytest.fixture
async def admin(make_client):
    c = make_client()
    await full_user(c, "11111111")  # first user: Gær admin, so also school administrator
    return c


async def test_only_school_admins(admin, make_client):
    assert (await admin.get("/api/school/admin/overview")).status_code == 200
    user = make_client()
    await email_login(user, "elev@skole.dk")
    for path in ("/api/school/admin/overview", "/api/school/admin/teachers", "/api/school/admin/students",
                 "/api/school/admin/usage", "/api/school/admin/subscription", "/api/school/admin/settings"):
        assert (await user.get(path)).status_code == 403, path
    assert (await user.post("/api/school/admin/teachers", json={"email": "x@skole.dk"})).status_code == 403


async def test_teacher_invited_by_email_gets_the_role_at_first_visit(admin, make_client, owner_conn):
    r = await admin.post("/api/school/admin/teachers", json={"email": "Mette@Skole.dk", "school_admin": True})
    assert r.json() == {"invited": True}
    invites = (await admin.get("/api/school/admin/teachers")).json()["invites"]
    assert [(i["email"], i["role"]) for i in invites] == [("mette@skole.dk", "school_admin")]

    mette = make_client()
    await email_login(mette, "mette@skole.dk")
    me = (await mette.get("/api/class/me")).json()
    assert me["is_teacher"] and me["is_school_admin"] and me["can_create_classes"]
    assert (await mette.get("/api/school/admin/overview")).status_code == 200
    assert (await admin.get("/api/school/admin/teachers")).json()["invites"] == []
    tables = [r[0] for r in owner_conn.execute(
        "select table_name from audit_log where action = 'role.changed' order by id").fetchall()]
    assert tables == ["teachers", "school_admins"]

    # A school administrator can take the role away again, but not from themselves or from a Gær admin.
    uid = _uid(owner_conn, "mette@skole.dk")
    assert (await mette.delete(f"/api/school/admin/teachers/{uid}")).status_code == 409
    admin_id = owner_conn.execute("select id from users where role = 'admin'").fetchone()[0]
    assert (await mette.delete(f"/api/school/admin/teachers/{admin_id}")).status_code == 409
    assert (await admin.delete(f"/api/school/admin/teachers/{uid}")).status_code == 200
    me = (await mette.get("/api/class/me")).json()
    assert not me["is_teacher"] and not me["is_school_admin"]


async def test_existing_user_becomes_teacher_at_once(admin, make_client, owner_conn):
    t = make_client()
    await email_login(t, "ida@skole.dk")
    assert (await admin.post("/api/school/admin/teachers", json={"email": "ida@skole.dk"})).json()["invited"] is False
    me = (await t.get("/api/class/me")).json()
    assert me["is_teacher"] and not me["is_school_admin"]
    teachers = (await admin.get("/api/school/admin/teachers")).json()["teachers"]
    assert {(x["email"], x["school_admin"]) for x in teachers} >= {("ida@skole.dk", False)}


async def test_seats_addons_and_ean(admin, make_client, owner_conn):
    _class(owner_conn)
    # Plan and prices are set by a Gær admin.
    r = await admin.put("/api/school/admin/subscription", json={"plan": "Skole", "seats": 1, "storage_gb": 10,
                                                               "price_text": "1.000 kr. pr. måned",
                                                               "prices": {"seats": "250 kr. pr. måned"}})
    assert r.status_code == 200, r.text
    a, b = make_client(), make_client()
    await email_login(a, "a@skole.dk")
    await email_login(b, "b@skole.dk")
    assert (await a.post("/api/class/join", json={"code": "ABCD-2345"})).status_code == 200
    r = await b.post("/api/class/join", json={"code": "ABCD-2345"})
    assert r.status_code == 409 and "elevpladser" in r.json()["detail"]

    # No add-ons without an EAN number; then order 25 more seats.
    assert (await admin.post("/api/school/admin/addons", json={"addon": "seats"})).status_code == 409
    assert (await admin.put("/api/school/admin/subscription/billing", json={"ean": "123"})).status_code == 422
    assert (await admin.put("/api/school/admin/subscription/billing",
                            json={"ean": "5790000123456", "invoice_reference": "IT-afd."})).status_code == 200
    order = (await admin.post("/api/school/admin/addons", json={"addon": "seats"})).json()
    assert (await b.post("/api/class/join", json={"code": "ABCD-2345"})).status_code == 200
    view = (await admin.get("/api/school/admin/subscription")).json()
    assert view["seats"] == {"used": 2, "total": 26}
    seats = next(c for c in view["catalog"] if c["key"] == "seats")
    assert seats["price"] == "250 kr. pr. måned" and len(seats["active"]) == 1
    assert view["contact"]["id"] == owner_conn.execute("select id from users where role = 'admin'").fetchone()[0]

    # Cancelling would leave more students than seats.
    r = await admin.post(f"/api/school/admin/addons/{order['id']}/cancel")
    assert r.status_code == 409
    # A non-repeatable add-on can only be active once.
    assert (await admin.post("/api/school/admin/addons", json={"addon": "support"})).status_code == 200
    assert (await admin.post("/api/school/admin/addons", json={"addon": "support"})).status_code == 409

    csv = (await admin.get("/api/school/admin/billing.csv")).content.decode("utf-8-sig")
    assert "5790000123456" in csv and "Flere elevpladser" in csv and "Prioriteret support" in csv
    actions = {r[0] for r in owner_conn.execute("select action from audit_log").fetchall()}
    assert {"school.addon.ordered", "school.billing.exported"} <= actions


async def test_plan_is_for_gaer_admins_only(admin, make_client, owner_conn):
    lone = make_client()
    await email_login(lone, "lone@skole.dk")
    await admin.post("/api/school/admin/teachers", json={"email": "lone@skole.dk", "school_admin": True})
    r = await lone.put("/api/school/admin/subscription", json={"plan": "Gratis", "seats": 9999, "storage_gb": 1})
    assert r.status_code == 403
    assert (await lone.get("/api/school/admin/subscription")).json()["can_edit_plan"] is False


async def test_student_email_domain_and_teachers_creating_classes(admin, make_client, owner_conn):
    _class(owner_conn)
    r = await admin.put("/api/school/admin/settings", json={"student_email_domain": "@elev.skole.dk",
                                                           "teachers_create_classes": False})
    assert r.status_code == 200, r.text
    assert (await admin.get("/api/school/admin/settings")).json()["student_email_domain"] == "elev.skole.dk"
    outsider, insider = make_client(), make_client()
    await email_login(outsider, "sara@gmail.com")
    await email_login(insider, "sara@elev.skole.dk")
    assert (await outsider.post("/api/class/join", json={"code": "ABCD-2345"})).status_code == 403
    assert (await insider.post("/api/class/join", json={"code": "ABCD-2345"})).status_code == 200

    t = make_client()
    await email_login(t, "ida@skole.dk")
    await admin.post("/api/school/admin/teachers", json={"email": "ida@skole.dk"})
    assert (await t.get("/api/class/me")).json()["can_create_classes"] is False


async def test_students_move_remove_and_remind(admin, make_client, owner_conn):
    a_id, b_id = _class(owner_conn, "9.B", "ABCD-2345"), _class(owner_conn, "8.A", "WXYZ-6789")
    s = make_client()
    await email_login(s, "noah@skole.dk")
    await s.post("/api/class/join", json={"code": "ABCD-2345"})
    uid = _uid(owner_conn, "noah@skole.dk")

    rows = (await admin.get("/api/school/admin/students?status=no_github")).json()
    assert rows["total"] == 1 and rows["items"][0]["classes"][0]["status"] == "waiting_account"
    assert (await admin.get(f"/api/school/admin/students?class_id={b_id}")).json()["total"] == 0

    assert (await admin.post("/api/school/admin/students/remind")).json() == {"queued": 1}
    assert await jobs.run_pending() == 1
    assert owner_conn.execute("select result->>'sent' from jobs where kind = 'school.remind_github'").fetchone()[0] == "true"

    r = await admin.post(f"/api/school/admin/students/{uid}/move", json={"from_class_id": a_id, "to_class_id": b_id})
    assert r.status_code == 200, r.text
    assert [c["name"] for c in (await admin.get("/api/school/admin/students")).json()["items"][0]["classes"]] == ["8.A"]
    assert (await admin.delete(f"/api/school/admin/students/{uid}")).status_code == 200
    assert (await admin.get("/api/school/admin/students")).json()["total"] == 0


async def test_class_teachers_and_archive(admin, make_client, owner_conn):
    cid = _class(owner_conn)
    t = make_client()
    await email_login(t, "ida@skole.dk")
    tid = _uid(owner_conn, "ida@skole.dk")
    assert (await admin.post(f"/api/school/admin/classes/{cid}/teachers", json={"user_id": tid})).status_code == 400
    await admin.post("/api/school/admin/teachers", json={"email": "ida@skole.dk"})
    assert (await admin.post(f"/api/school/admin/classes/{cid}/teachers", json={"user_id": tid})).status_code == 200
    assert (await t.get(f"/api/class/classes/{cid}")).status_code == 200  # now her class
    assert (await admin.delete(f"/api/school/admin/classes/{cid}/teachers/{tid}")).status_code == 409  # the only teacher
    classes = (await admin.get("/api/school/admin/classes")).json()
    assert [x["id"] for x in classes[0]["teachers"]] == [tid]
    assert (await admin.post(f"/api/school/admin/classes/{cid}/archive")).status_code == 200
    assert (await admin.get("/api/school/admin/classes")).json()[0]["archived_at"] is not None


async def test_usage_and_school_ai_cap(admin, make_client, owner_conn):
    cid = _class(owner_conn)
    s = make_client()
    await email_login(s, "sara@skole.dk")
    await s.post("/api/class/join", json={"code": "ABCD-2345"})
    uid = _uid(owner_conn, "sara@skole.dk")
    owner_conn.execute("""insert into ai_usage (user_id, surface, model, prompt_tokens, completion_tokens, estimated)
                          values (%s, 'app', 'm', 600, 400, false)""", (uid,))
    owner_conn.execute("insert into files (id, user_id, filename, content_type, size_bytes, storage_key) "
                       "values (gen_random_uuid(), %s, 'a.txt', 'text/plain', 2048, 'k1')", (uid,))
    u = (await admin.get("/api/school/admin/usage")).json()
    assert u["ai"]["used"] == 1000 and u["seats"] == {"used": 1, "total": None}
    row = next(r for r in u["per_class"] if r["id"] == cid)
    assert (row["students"], row["ai_tokens"], row["storage_bytes"]) == (1, 1000, 2048)

    r = await admin.put("/api/school/admin/limits", json={"per_user_monthly_tokens": 40000, "school_monthly_tokens": 1000})
    assert r.status_code == 200
    from app import db
    async with db.connection() as conn:
        assert await school.school_ai_left(conn) == 0
    u = (await admin.get("/api/school/admin/usage")).json()
    assert u["ai"]["per_user_monthly_tokens"] == 40000 and u["ai"]["school_monthly_tokens"] == 1000
    assert (await admin.get("/api/school/admin/usage?month=nope")).status_code == 400


async def test_gaer_admin_grants_school_admin_in_admin_users(admin, make_client, owner_conn):
    lone = make_client()
    await email_login(lone, "lone@skole.dk")
    uid = _uid(owner_conn, "lone@skole.dk")
    assert (await admin.put(f"/api/admin/users/{uid}/school-admin")).status_code == 200
    assert (await lone.get("/api/class/me")).json()["is_school_admin"] is True
    users = (await admin.get("/api/admin/users")).json()["items"]
    assert next(x for x in users if x["id"] == uid)["is_school_admin"] is True
    assert (await admin.delete(f"/api/admin/users/{uid}/school-admin")).status_code == 200
    assert (await lone.get("/api/class/me")).json()["is_school_admin"] is False


async def test_gdpr_export_has_school_roles(admin, make_client):
    lone = make_client()
    await email_login(lone, "lone@skole.dk")
    await admin.post("/api/school/admin/teachers", json={"email": "lone@skole.dk", "school_admin": True})
    import io, json, zipfile
    data = json.loads(zipfile.ZipFile(io.BytesIO((await lone.get("/api/me/export")).content)).read("data.json"))
    assert len(data["school_admin"]) == 1


async def test_old_class_links_redirect(client):
    r = await client.get("/class/student?kode=ABCD-2345")
    assert r.status_code == 301 and r.headers["location"] == "/school/student?kode=ABCD-2345"
    r = await client.get("/class")
    assert r.headers["location"] == "/school"
