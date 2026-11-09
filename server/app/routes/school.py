"""/api/school: the school administrator console on /school/admin. Logic: app/school.py; classes: app/classroom.py.

Everything under /api/school/admin needs a school administrator (admin role, or a row in school_admins) and every change
is audited in the same transaction. Add-ons are not paid here: an order is registered, announced to Happy Mates with the
webhook event school.addon.ordered and invoiced on the next EAN invoice.
"""

import csv
import io
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import classroom, db, jobs, mail, school, settings_store, webhooks
from app.logs import audit, changed
from app.sessions import Principal, current_user, require_admin_stepup

router = APIRouter(prefix="/api/school", tags=["school"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin"])

SCHOOL_TABLES = ("classes", "class_members", "student_repos", "teachers", "school_admins", "school_invites",
                 "school_addons", "school_subscription", "github_orgs", "github_org_teachers", "settings")


async def require_school_admin(p: Principal = Depends(current_user)) -> Principal:
    async with db.connection() as conn:
        if not await school.is_school_admin(conn, p.user_id, p.role):
            raise HTTPException(403, "Kræver skoleadministrator")
    return p


def _name(row: dict) -> str:
    return row.get("display_name") or row.get("email") or f"Bruger #{row['id']}"


# ── Overview ───────────────────────────────────────────────────────────────

@router.get("/admin/overview")
async def overview(p: Principal = Depends(require_school_admin)):
    async with db.connection() as conn:
        counts = await db.fetch_one(conn, """
            select (select count(*) from classes where archived_at is null) as classes,
                   (select count(*) from classes where archived_at is not null) as archived,
                   (select count(*) from student_repos sr join classes c on c.id = sr.class_id
                     where c.archived_at is null and sr.status in ('ready', 'transferred')) as repos_ready,
                   (select count(*) from student_repos sr join classes c on c.id = sr.class_id
                     where c.archived_at is null) as repos_total,
                   (select count(*) from student_repos sr join classes c on c.id = sr.class_id
                     where c.archived_at is null and sr.status = 'failed') as repos_failed,
                   (select count(distinct m.user_id) from class_members m join classes c on c.id = m.class_id
                     left join github_accounts ga on ga.user_id = m.user_id
                     where m.role = 'student' and c.archived_at is null and ga.user_id is null) as students_without_github,
                   (select count(*) from school_invites) as invites""")
        teachers = await _teachers(conn)
        seats = await school.seats(conn)
        recent = await db.fetch_all(conn, """
            select a.ts, a.action, a.table_name, a.row_id, a.before, a.after,
                   coalesce(u.display_name, u.email, 'System') as actor
            from audit_log a left join users u on u.id = a.actor_user_id
            where a.table_name = any(%s) and a.action <> 'admin.read'
            order by a.ts desc limit 10""", (list(SCHOOL_TABLES),))
        orgs = await db.fetch_all(conn, "select id, login, disconnected_at from github_orgs order by login")
    return {"counts": {**counts, "teachers": len(teachers), "students": seats["used"],
                       "teachers_without_github": sum(1 for t in teachers if not t["github_login"])},
            "seats": seats, "orgs": orgs, "recent": recent}


# ── Teachers and school administrators ─────────────────────────────────────

async def _teachers(conn) -> list[dict]:
    rows = await db.fetch_all(conn, """
        select u.id, u.display_name, u.email, u.role, ga.login as github_login,
               exists (select 1 from school_admins s where s.user_id = u.id) as school_admin_row,
               exists (select 1 from teachers t where t.user_id = u.id) as teacher_row,
               (select string_agg(c.name, ', ' order by c.name) from class_members m join classes c on c.id = m.class_id
                 where m.user_id = u.id and m.role = 'teacher' and c.archived_at is null) as classes,
               (select count(distinct s.user_id) from class_members m join class_members s on s.class_id = m.class_id
                 and s.role = 'student' join classes c on c.id = m.class_id
                 where m.user_id = u.id and m.role = 'teacher' and c.archived_at is null) as students,
               (select coalesce(sum(a.prompt_tokens + a.completion_tokens), 0) from ai_usage a
                 where a.ts >= date_trunc('month', now()) and a.user_id in (
                   select s.user_id from class_members m join class_members s on s.class_id = m.class_id and s.role = 'student'
                   where m.user_id = u.id and m.role = 'teacher')) as student_ai_tokens
        from users u left join github_accounts ga on ga.user_id = u.id
        where (u.role = 'admin' or exists (select 1 from teachers t where t.user_id = u.id)
               or exists (select 1 from school_admins s where s.user_id = u.id))
          and not u.impersonation_only and u.blocked_at is null
        order by coalesce(u.display_name, u.email)""")
    for r in rows:
        r["name"] = _name(r)
        r["school_admin"] = r["role"] == "admin" or r.pop("school_admin_row")
        r["gaer_admin"] = r["role"] == "admin"
        r["teacher"] = r.pop("teacher_row") or r["school_admin"]
        r["student_ai_tokens"] = int(r["student_ai_tokens"])
    return rows


@router.get("/admin/teachers")
async def list_teachers(p: Principal = Depends(require_school_admin)):
    async with db.connection() as conn:
        teachers = await _teachers(conn)
        invites = await db.fetch_all(conn, """select i.email, i.role, i.created_at, coalesce(u.display_name, u.email) as invited_by
                                              from school_invites i left join users u on u.id = i.invited_by
                                              order by i.created_at desc""")
    return {"teachers": teachers, "invites": invites}


class TeacherIn(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    school_admin: bool = False


@router.post("/admin/teachers")
async def add_teacher(body: TeacherIn, p: Principal = Depends(require_school_admin)):
    """Makes the user with this e-mail a teacher (and school administrator), or invites the e-mail if nobody has logged
    in with it yet: the role is granted the first time they open /school."""
    email = body.email.strip().lower()
    async with db.connection() as conn:
        user = await db.fetch_one(conn, "select id from users where email = %s and not impersonation_only", (email,))
        if user is None:
            role = "school_admin" if body.school_admin else "teacher"
            await conn.execute("""insert into school_invites (email, role, invited_by) values (%s, %s, %s)
                                  on conflict (email) do update set role = excluded.role, invited_by = excluded.invited_by,
                                  created_at = now()""", (email, role, p.user_id))
            await audit(conn, "create", actor_id=p.user_id, meta=p.meta, table="school_invites", row_id=email,
                        after={"role": role})
            return {"invited": True}
        await school.grant_teacher(conn, user["id"], p.user_id, p.meta)
        if body.school_admin:
            await school.grant_school_admin(conn, user["id"], p.user_id, p.meta)
    return {"invited": False, "user_id": user["id"]}


@router.delete("/admin/teachers/{user_id}")
async def remove_teacher(user_id: int, p: Principal = Depends(require_school_admin)):
    """Removes the teacher and school administrator roles. Classes keep their teachers until changed in Klasser."""
    if user_id == p.user_id:
        raise HTTPException(409, "Du kan ikke fjerne dig selv")
    async with db.connection() as conn:
        target = await db.fetch_one(conn, "select role from users where id = %s", (user_id,))
        if target is None:
            raise HTTPException(404, "Brugeren findes ikke")
        if target["role"] == "admin":
            raise HTTPException(409, "En administrator af Gær er altid skoleadministrator. Det ændres i /admin.")
        if not await school.revoke_teacher(conn, user_id, p.user_id, p.meta):
            raise HTTPException(404, "Brugeren er ikke lærer")
    return {"ok": True}


@router.put("/admin/teachers/{user_id}/school-admin")
async def make_school_admin(user_id: int, p: Principal = Depends(require_school_admin)):
    async with db.connection() as conn:
        if await db.fetch_one(conn, "select 1 as ok from users where id = %s", (user_id,)) is None:
            raise HTTPException(404, "Brugeren findes ikke")
        await school.grant_teacher(conn, user_id, p.user_id, p.meta)
        await school.grant_school_admin(conn, user_id, p.user_id, p.meta)
    return {"ok": True}


@router.delete("/admin/teachers/{user_id}/school-admin")
async def unmake_school_admin(user_id: int, p: Principal = Depends(require_school_admin)):
    if user_id == p.user_id:
        raise HTTPException(409, "Du kan ikke fjerne din egen administratorrolle")
    async with db.connection() as conn:
        target = await db.fetch_one(conn, "select role from users where id = %s", (user_id,))
        if target and target["role"] == "admin":
            raise HTTPException(409, "En administrator af Gær er altid skoleadministrator. Det ændres i /admin.")
        await school.revoke_school_admin(conn, user_id, p.user_id, p.meta)
    return {"ok": True}


@router.delete("/admin/invites/{email}")
async def cancel_invite(email: str, p: Principal = Depends(require_school_admin)):
    async with db.connection() as conn:
        row = await db.fetch_one(conn, "delete from school_invites where email = %s returning role", (email.lower(),))
        if row is None:
            raise HTTPException(404, "Invitationen findes ikke")
        await audit(conn, "delete", actor_id=p.user_id, meta=p.meta, table="school_invites", row_id=email.lower(),
                    before=row)
    return {"ok": True}


# ── Students ───────────────────────────────────────────────────────────────

STATUS_FILTERS = {
    "no_github": "ga.user_id is null",
    "invited": "exists (select 1 from student_repos x where x.user_id = u.id and x.status = 'invited')",
    "failed": "exists (select 1 from student_repos x where x.user_id = u.id and x.status = 'failed')",
}


@router.get("/admin/students")
async def list_students(q: str | None = None, class_id: int | None = None, status: str | None = None,
                        limit: int = 200, offset: int = 0, p: Principal = Depends(require_school_admin)):
    where = ["exists (select 1 from class_members m join classes c on c.id = m.class_id "
             "where m.user_id = u.id and m.role = 'student' and c.archived_at is null)"]
    params: list = []
    if q:
        where.append("(u.display_name ilike %s or u.email ilike %s or ga.login ilike %s)")
        params += [f"%{q.strip()}%"] * 3
    if class_id:
        where.append("exists (select 1 from class_members m where m.user_id = u.id and m.class_id = %s and m.role = 'student')")
        params.append(class_id)
    if status in STATUS_FILTERS:
        where.append(STATUS_FILTERS[status])
    sql_where = " and ".join(where)
    limit, offset = max(1, min(limit, 500)), max(0, offset)
    async with db.connection() as conn:
        rows = await db.fetch_all(conn, f"""
            select u.id, u.display_name, u.email, ga.login as github_login,
                   (select json_agg(json_build_object('id', c.id, 'name', c.name, 'status', sr.status) order by c.name)
                      from class_members m join classes c on c.id = m.class_id
                      left join student_repos sr on sr.class_id = m.class_id and sr.user_id = m.user_id
                     where m.user_id = u.id and m.role = 'student' and c.archived_at is null) as classes
            from users u left join github_accounts ga on ga.user_id = u.id
            where {sql_where} order by coalesce(u.display_name, u.email) limit %s offset %s""",
            params + [limit, offset])
        total = await db.fetch_one(conn, f"""select count(*) as n from users u
                                             left join github_accounts ga on ga.user_id = u.id where {sql_where}""", params)
    for r in rows:
        r["name"] = _name(r)
        r["classes"] = r["classes"] or []
    return {"items": rows, "total": total["n"]}


@router.post("/admin/students/remind")
async def remind_students(p: Principal = Depends(require_school_admin)):
    """Sends an e-mail to every student who has not linked a GitHub account (one background job each)."""
    async with db.connection() as conn:
        rows = await db.fetch_all(conn, """
            select distinct u.id from users u join class_members m on m.user_id = u.id and m.role = 'student'
            join classes c on c.id = m.class_id left join github_accounts ga on ga.user_id = u.id
            where c.archived_at is null and ga.user_id is null and u.email is not null""")
        for r in rows:
            await jobs.enqueue(conn, "school.remind_github", {"user_id": r["id"]},
                               dedupe_key=f"school.remind_github:{r['id']}")
        await audit(conn, "school.reminder", actor_id=p.user_id, meta=p.meta, table="class_members",
                    reminder="github_account", count=len(rows))
    return {"queued": len(rows)}


@jobs.handler("school.remind_github")
async def _remind_github(payload: dict, job: dict) -> dict:
    async with db.connection() as conn:
        user = await db.fetch_one(conn, "select email, display_name from users where id = %s", (payload["user_id"],))
        linked = await db.fetch_one(conn, "select 1 as ok from github_accounts where user_id = %s", (payload["user_id"],))
        app_name = await settings_store.get(conn, "app_name")
    if user is None or not user["email"] or linked:
        return {"sent": False}
    from app import config
    link = config.settings.origin.rstrip("/") + "/school/student"
    hello = f"Hej {user['display_name']}" if user["display_name"] else "Hej"
    text = (f"{hello}\n\nDu er tilmeldt en klasse, men har ikke forbundet din GitHub-konto endnu. "
            f"Gør det her, så får du dit eget repo:\n{link}\n")
    html_body = (f"<p>{hello}</p><p>Du er tilmeldt en klasse, men har ikke forbundet din GitHub-konto endnu. "
                 f"Gør det her, så får du dit eget repo:</p><p><a href=\"{link}\">{link}</a></p>")
    await mail.send_mail(user["email"], "Forbind din GitHub-konto", text, html_body, app_name)
    return {"sent": True}


class MoveIn(BaseModel):
    from_class_id: int
    to_class_id: int


@router.post("/admin/students/{user_id}/move")
async def move_student(user_id: int, body: MoveIn, p: Principal = Depends(require_school_admin)):
    """Moves a student to another class. The repo in the old class stays on GitHub; a new one is made in the new class."""
    if body.from_class_id == body.to_class_id:
        raise HTTPException(400, "Vælg en anden klasse")
    async with db.connection() as conn:
        target = await db.fetch_one(conn, "select id, name from classes where id = %s and archived_at is null",
                                    (body.to_class_id,))
        if target is None:
            raise HTTPException(404, "Klassen findes ikke")
        moved = await db.fetch_one(conn, """delete from class_members where class_id = %s and user_id = %s
                                            and role = 'student' returning class_id""", (body.from_class_id, user_id))
        if moved is None:
            raise HTTPException(404, "Eleven er ikke i den klasse")
        await conn.execute("delete from student_repos where class_id = %s and user_id = %s", (body.from_class_id, user_id))
        account = await db.fetch_one(conn, "select 1 as ok from github_accounts where user_id = %s", (user_id,))
        await conn.execute("""insert into class_members (class_id, user_id, role) values (%s, %s, 'student')
                              on conflict do nothing""", (body.to_class_id, user_id))
        await conn.execute("""insert into student_repos (class_id, user_id, status) values (%s, %s, %s)
                              on conflict do nothing""", (body.to_class_id, user_id, "pending" if account else "waiting_account"))
        await audit(conn, "update", actor_id=p.user_id, meta=p.meta, table="class_members", row_id=user_id,
                    before={"class_id": body.from_class_id}, after={"class_id": body.to_class_id})
        if account:
            await classroom.enqueue_student(conn, body.to_class_id, user_id, p.user_id)
    return {"ok": True}


@router.delete("/admin/students/{user_id}")
async def remove_student(user_id: int, p: Principal = Depends(require_school_admin)):
    """Takes the student out of every class (frees the seat). Their account, repos and data are left as they are."""
    async with db.connection() as conn:
        rows = await db.fetch_all(conn, """delete from class_members where user_id = %s and role = 'student'
                                           returning class_id""", (user_id,))
        if not rows:
            raise HTTPException(404, "Brugeren er ikke elev i nogen klasse")
        await conn.execute("delete from student_repos where user_id = %s", (user_id,))
        await audit(conn, "delete", actor_id=p.user_id, meta=p.meta, table="class_members", row_id=user_id,
                    before={"classes": [r["class_id"] for r in rows]})
    return {"ok": True}


# ── Classes ────────────────────────────────────────────────────────────────

@router.get("/admin/classes")
async def list_classes(p: Principal = Depends(require_school_admin)):
    async with db.connection() as conn:
        rows = await db.fetch_all(conn, """
            select c.id, c.name, c.slug, c.template_repo, c.archived_at, c.created_at, o.login as org,
                   (select json_agg(json_build_object('id', u.id, 'name', coalesce(u.display_name, u.email)) order by u.display_name)
                      from class_members m join users u on u.id = m.user_id
                     where m.class_id = c.id and m.role = 'teacher') as teachers,
                   (select count(*) from class_members m where m.class_id = c.id and m.role = 'student') as students,
                   (select count(*) from student_repos sr where sr.class_id = c.id and sr.status in ('ready', 'transferred')) as ready,
                   (select count(*) from student_repos sr where sr.class_id = c.id and sr.status = 'failed') as failed
            from classes c join github_orgs o on o.id = c.org_id
            order by c.archived_at nulls first, c.name""")
    for r in rows:
        r["teachers"] = r["teachers"] or []
    return rows


class ClassTeacherIn(BaseModel):
    user_id: int


@router.post("/admin/classes/{class_id}/teachers")
async def add_class_teacher(class_id: int, body: ClassTeacherIn, p: Principal = Depends(require_school_admin)):
    async with db.connection() as conn:
        if await db.fetch_one(conn, "select 1 as ok from classes where id = %s", (class_id,)) is None:
            raise HTTPException(404, "Klassen findes ikke")
        user = await db.fetch_one(conn, "select role from users where id = %s", (body.user_id,))
        if user is None or not await school.is_teacher(conn, body.user_id, user["role"]):
            raise HTTPException(400, "Personen er ikke lærer. Tilføj vedkommende under Lærere først.")
        row = await db.fetch_one(conn, """insert into class_members (class_id, user_id, role) values (%s, %s, 'teacher')
                                          on conflict (class_id, user_id) do update set role = 'teacher'
                                          returning class_id""", (class_id, body.user_id))
        await conn.execute("delete from student_repos where class_id = %s and user_id = %s", (class_id, body.user_id))
        if row:
            await audit(conn, "create", actor_id=p.user_id, meta=p.meta, table="class_members",
                        row_id=f"{class_id}:{body.user_id}", after={"role": "teacher"})
    return {"ok": True}


@router.delete("/admin/classes/{class_id}/teachers/{user_id}")
async def remove_class_teacher(class_id: int, user_id: int, p: Principal = Depends(require_school_admin)):
    async with db.connection() as conn:
        n = await db.fetch_one(conn, "select count(*) as n from class_members where class_id = %s and role = 'teacher'",
                               (class_id,))
        if n["n"] <= 1:
            raise HTTPException(409, "En klasse skal have mindst én lærer. Tilføj den nye lærer først.")
        row = await db.fetch_one(conn, """delete from class_members where class_id = %s and user_id = %s
                                          and role = 'teacher' returning class_id""", (class_id, user_id))
        if row is None:
            raise HTTPException(404, "Personen er ikke lærer i klassen")
        await audit(conn, "delete", actor_id=p.user_id, meta=p.meta, table="class_members",
                    row_id=f"{class_id}:{user_id}", before={"role": "teacher"})
    return {"ok": True}


@router.post("/admin/classes/{class_id}/archive")
async def archive_class(class_id: int, p: Principal = Depends(require_school_admin)):
    async with db.connection() as conn:
        row = await db.fetch_one(conn, """update classes set archived_at = now() where id = %s and archived_at is null
                                          returning id""", (class_id,))
        if row:
            await audit(conn, "update", actor_id=p.user_id, meta=p.meta, table="classes", row_id=class_id,
                        before={"archived": False}, after={"archived": True})
    return {"ok": True}


# ── Usage and limits ───────────────────────────────────────────────────────

@router.get("/admin/usage")
async def usage(month: str | None = None, p: Principal = Depends(require_school_admin)):
    """AI tokens, storage and seats. month = YYYY-MM (default: this month). Students in several classes count in each."""
    try:
        start = date.fromisoformat(f"{month}-01") if month else date.today().replace(day=1)
    except ValueError:
        raise HTTPException(400, "Måned skal skrives ÅÅÅÅ-MM")
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    async with db.connection() as conn:
        ai_total = await db.fetch_one(conn, """select coalesce(sum(prompt_tokens + completion_tokens), 0) as n from ai_usage
                                               where ts >= %s and ts < %s""", (start, end))
        per_class = await db.fetch_all(conn, """
            select c.id, c.name,
                   (select string_agg(coalesce(u.display_name, u.email), ', ') from class_members t join users u on u.id = t.user_id
                     where t.class_id = c.id and t.role = 'teacher') as teachers,
                   (select count(*) from class_members m where m.class_id = c.id and m.role = 'student') as students,
                   (select coalesce(sum(a.prompt_tokens + a.completion_tokens), 0) from ai_usage a
                     where a.ts >= %s and a.ts < %s and a.user_id in
                       (select m.user_id from class_members m where m.class_id = c.id and m.role = 'student')) as ai_tokens,
                   (select coalesce(sum(f.size_bytes), 0) from files f where f.user_id in
                       (select m.user_id from class_members m where m.class_id = c.id and m.role = 'student')) as storage_bytes
            from classes c where c.archived_at is null order by c.name""", (start, end))
        values = await settings_store.get_all(conn)
        seats = await school.seats(conn)
        storage = await school.storage(conn)
    for r in per_class:
        r["ai_tokens"], r["storage_bytes"] = int(r["ai_tokens"]), int(r["storage_bytes"])
    return {"month": start.isoformat()[:7], "seats": seats, "storage": storage,
            "ai": {"used": int(ai_total["n"]), "school_monthly_tokens": int(values["school_ai_monthly_tokens"] or 0),
                   "per_user_monthly_tokens": int(values["ai_default_monthly_tokens"])},
            "per_class": per_class}


class LimitsIn(BaseModel):
    per_user_monthly_tokens: int = Field(ge=0, le=100_000_000)
    school_monthly_tokens: int = Field(ge=0, le=10_000_000_000)


@router.put("/admin/limits")
async def set_limits(body: LimitsIn, p: Principal = Depends(require_school_admin)):
    async with db.connection() as conn:
        before = {"per_user_monthly_tokens": await settings_store.get(conn, "ai_default_monthly_tokens"),
                  "school_monthly_tokens": await settings_store.get(conn, "school_ai_monthly_tokens")}
        await settings_store.set_many(conn, {"ai_default_monthly_tokens": body.per_user_monthly_tokens,
                                             "school_ai_monthly_tokens": body.school_monthly_tokens})
        b, a = changed(before, body.model_dump())
        if a:
            await audit(conn, "update", actor_id=p.user_id, meta=p.meta, table="settings", row_id="school_ai_limits",
                        before=b, after=a)
    return {"ok": True}


# ── Subscription, add-ons and invoicing ────────────────────────────────────

async def _subscription_view(conn, p: Principal) -> dict:
    sub = await school.subscription(conn)
    prices = await settings_store.get(conn, "school_addon_prices") or {}
    orders = await db.fetch_all(conn, """
        select a.id, a.addon, a.quantity, a.status, a.ordered_at, a.cancelled_at,
               coalesce(u.display_name, u.email) as ordered_by
        from school_addons a left join users u on u.id = a.ordered_by order by a.ordered_at desc""")
    active = {}
    for o in orders:
        if o["status"] == "active":
            active.setdefault(o["addon"], []).append(o)
    contact = None
    if sub and sub["contact_user_id"]:
        contact = await db.fetch_one(conn, "select id, display_name, email from users where id = %s", (sub["contact_user_id"],))
    catalog = [{"key": a.key, "title": a.title, "description": a.description, "repeatable": a.repeatable,
                "price": prices.get(a.key) or "Pris efter aftale",
                "active": [{"id": o["id"], "quantity": o["quantity"], "ordered_at": o["ordered_at"], "ordered_by": o["ordered_by"]}
                           for o in active.get(a.key, [])]} for a in school.ADDONS.values()]
    return {"subscription": sub and {k: sub[k] for k in ("plan", "seats", "storage_gb", "price_text", "renews_on", "ean",
                                                         "invoice_reference", "updated_at")},
            "contact": contact, "seats": await school.seats(conn), "storage": await school.storage(conn),
            "catalog": catalog, "orders": orders, "prices": prices, "can_edit_plan": p.role == "admin"}


@router.get("/admin/subscription")
async def get_subscription(p: Principal = Depends(require_school_admin)):
    async with db.connection() as conn:
        return await _subscription_view(conn, p)


class BillingIn(BaseModel):
    ean: str = Field(pattern=r"^[0-9]{13}$")
    invoice_reference: str | None = Field(default=None, max_length=100)
    contact_me: bool = True


@router.put("/admin/subscription/billing")
async def set_billing(body: BillingIn, p: Principal = Depends(require_school_admin)):
    """The school's own invoicing details: EAN number, reference on the invoice and the contact person."""
    async with db.connection() as conn:
        before = await school.subscription(conn) or {}
        contact = p.user_id if body.contact_me else before.get("contact_user_id")
        await conn.execute("""
            insert into school_subscription (id, ean, invoice_reference, contact_user_id, updated_by)
            values (1, %s, %s, %s, %s)
            on conflict (id) do update set ean = excluded.ean, invoice_reference = excluded.invoice_reference,
                contact_user_id = excluded.contact_user_id, updated_by = excluded.updated_by, updated_at = now()""",
            (body.ean, body.invoice_reference, contact, p.user_id))
        b, a = changed({k: before.get(k) for k in ("ean", "invoice_reference", "contact_user_id")},
                       {"ean": body.ean, "invoice_reference": body.invoice_reference, "contact_user_id": contact})
        if a:
            await audit(conn, "update", actor_id=p.user_id, meta=p.meta, table="school_subscription", row_id=1,
                        before=b, after=a)
    return {"ok": True}


class PlanIn(BaseModel):
    plan: str = Field(min_length=1, max_length=80)
    seats: int = Field(ge=0, le=100_000)
    storage_gb: int = Field(ge=0, le=1_000_000)
    price_text: str = Field(default="", max_length=120)
    renews_on: date | None = None
    prices: dict[str, str] = Field(default_factory=dict)


@router.put("/admin/subscription")
async def set_plan(body: PlanIn, p: Principal = Depends(require_school_admin)):
    """Plan, seats, storage and prices: what Happy Mates has agreed with the school. Only a Gær admin."""
    if p.role != "admin":
        raise HTTPException(403, "Abonnement og priser ændres af Happy Mates")
    unknown = set(body.prices) - set(school.ADDONS)
    if unknown:
        raise HTTPException(400, f"Ukendt tilkøb: {', '.join(sorted(unknown))}")
    prices = {k: v.strip()[:120] for k, v in body.prices.items() if v.strip()}
    async with db.connection() as conn:
        before = await school.subscription(conn) or {}
        before_prices = await settings_store.get(conn, "school_addon_prices") or {}
        await conn.execute("""
            insert into school_subscription (id, plan, seats, storage_gb, price_text, renews_on, updated_by)
            values (1, %s, %s, %s, %s, %s, %s)
            on conflict (id) do update set plan = excluded.plan, seats = excluded.seats, storage_gb = excluded.storage_gb,
                price_text = excluded.price_text, renews_on = excluded.renews_on, updated_by = excluded.updated_by,
                updated_at = now()""",
            (body.plan.strip(), body.seats, body.storage_gb, body.price_text.strip(), body.renews_on, p.user_id))
        await settings_store.set_many(conn, {"school_addon_prices": prices})
        keys = ("plan", "seats", "storage_gb", "price_text", "renews_on")
        b, a = changed({**{k: before.get(k) for k in keys}, "prices": before_prices},
                       {**{k: getattr(body, k) for k in keys}, "plan": body.plan.strip(), "price_text": body.price_text.strip(),
                        "prices": prices})
        if a:
            await audit(conn, "update", actor_id=p.user_id, meta=p.meta, table="school_subscription", row_id=1,
                        before=b, after=a)
    return {"ok": True}


class OrderIn(BaseModel):
    addon: str
    quantity: int = Field(default=1, ge=1, le=100)


@router.post("/admin/addons")
async def order_addon(body: OrderIn, p: Principal = Depends(require_school_admin)):
    addon = school.ADDONS.get(body.addon)
    if addon is None:
        raise HTTPException(404, "Ukendt tilkøb")
    async with db.connection() as conn:
        sub = await school.subscription(conn)
        if sub is None or not sub["ean"]:
            raise HTTPException(409, "Udfyld skolens EAN-nummer under Tilkøb, før I bestiller")
        if not addon.repeatable and await school.addon_units(conn, addon.key):
            raise HTTPException(409, "Tilkøbet er allerede aktivt")
        quantity = body.quantity if addon.repeatable else 1
        row = await db.fetch_one(conn, """insert into school_addons (addon, quantity, ordered_by) values (%s, %s, %s)
                                          returning id, addon, quantity, ordered_at""", (addon.key, quantity, p.user_id))
        await audit(conn, "school.addon.ordered", actor_id=p.user_id, meta=p.meta, table="school_addons",
                    row_id=row["id"], after={"addon": addon.key, "quantity": quantity})
        await webhooks.emit(conn, "school.addon.ordered", {"id": row["id"], "addon": addon.key, "quantity": quantity})
    return row


@router.post("/admin/addons/{order_id}/cancel")
async def cancel_addon(order_id: int, p: Principal = Depends(require_school_admin)):
    async with db.connection() as conn:
        row = await db.fetch_one(conn, """update school_addons set status = 'cancelled', cancelled_by = %s, cancelled_at = now()
                                          where id = %s and status = 'active' returning addon, quantity""",
                                 (p.user_id, order_id))
        if row is None:
            raise HTTPException(404, "Tilkøbet er ikke aktivt")
        if row["addon"] == "seats":
            s = await school.seats(conn)
            if s["total"] is not None and s["used"] > s["total"]:
                raise HTTPException(409, f"Der er {s['used']} elever i klasserne, men kun {s['total']} pladser uden dette "
                                         "tilkøb. Fjern elever eller arkivér klasser først.")
        await audit(conn, "school.addon.cancelled", actor_id=p.user_id, meta=p.meta, table="school_addons",
                    row_id=order_id, before={"status": "active"}, after={"status": "cancelled", **row})
        await webhooks.emit(conn, "school.addon.cancelled", {"id": order_id, "addon": row["addon"], "quantity": row["quantity"]})
    return {"ok": True}


@router.get("/admin/billing.csv")
async def billing_csv(p: Principal = Depends(require_school_admin)):
    """What to invoice: the subscription and every add-on order, for Happy Mates' EAN invoice."""
    async with db.connection() as conn:
        view = await _subscription_view(conn, p)
        await audit(conn, "school.billing.exported", actor_id=p.user_id, meta=p.meta, table="school_addons")
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    sub = view["subscription"] or {}
    w.writerow(["type", "navn", "antal", "status", "bestilt", "opsagt", "pris", "ean", "reference"])
    w.writerow(["abonnement", sub.get("plan", ""), sub.get("seats", ""), "aktiv", "", "", sub.get("price_text", ""),
                sub.get("ean", ""), sub.get("invoice_reference") or ""])
    for o in view["orders"]:
        a = school.ADDONS.get(o["addon"])
        w.writerow(["tilkøb", a.title if a else o["addon"], o["quantity"], o["status"], o["ordered_at"].date().isoformat(),
                    o["cancelled_at"].date().isoformat() if o["cancelled_at"] else "", view["prices"].get(o["addon"], ""),
                    sub.get("ean", ""), sub.get("invoice_reference") or ""])
    return Response(out.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="skole-fakturering-{date.today():%Y%m%d}.csv"'})


# ── School rules and the GitHub organisations ──────────────────────────────

class SettingsIn(BaseModel):
    student_email_domain: str = Field(default="", max_length=120, pattern=r"^@?([a-z0-9-]+\.)+[a-z]{2,}$|^$")
    teachers_create_classes: bool = True


@router.get("/admin/settings")
async def get_settings(p: Principal = Depends(require_school_admin)):
    async with db.connection() as conn:
        values = await settings_store.get_all(conn)
        orgs = await db.fetch_all(conn, """
            select o.id, o.login, o.created_at, o.disconnected_at, coalesce(u.display_name, u.email) as connected_by,
                   (select count(*) from classes c where c.org_id = o.id and c.archived_at is null) as classes
            from github_orgs o left join users u on u.id = o.connected_by order by o.login""")
    return {"student_email_domain": values["school_student_email_domain"],
            "teachers_create_classes": bool(values["school_teachers_create_classes"]), "orgs": orgs}


@router.put("/admin/settings")
async def put_settings(body: SettingsIn, p: Principal = Depends(require_school_admin)):
    domain = body.student_email_domain.strip().lower().lstrip("@")
    async with db.connection() as conn:
        before = {"student_email_domain": await settings_store.get(conn, "school_student_email_domain"),
                  "teachers_create_classes": await settings_store.get(conn, "school_teachers_create_classes")}
        after = {"student_email_domain": domain, "teachers_create_classes": body.teachers_create_classes}
        await settings_store.set_many(conn, {"school_student_email_domain": domain,
                                             "school_teachers_create_classes": body.teachers_create_classes})
        b, a = changed(before, after)
        if a:
            await audit(conn, "update", actor_id=p.user_id, meta=p.meta, table="settings", row_id="school", before=b, after=a)
    return {"ok": True}


@router.post("/admin/orgs/{org_id}/disconnect")
async def disconnect_org(org_id: int, p: Principal = Depends(require_school_admin)):
    """Stops using the organisation in Gær. The app stays installed on GitHub until someone removes it there."""
    async with db.connection() as conn:
        row = await db.fetch_one(conn, """update github_orgs set disconnected_at = now()
                                          where id = %s and disconnected_at is null returning login""", (org_id,))
        if row is None:
            raise HTTPException(404, "Organisationen er ikke forbundet")
        await audit(conn, "update", actor_id=p.user_id, meta=p.meta, table="github_orgs", row_id=org_id,
                    before={"connected": True}, after={"connected": False}, login=row["login"])
    return {"ok": True}


# ── A Gær admin can make anyone school administrator from /admin/users ─────

@admin_router.put("/users/{user_id}/school-admin")
async def admin_grant_school_admin(user_id: int, admin: Principal = Depends(require_admin_stepup)):
    async with db.connection() as conn:
        if await db.fetch_one(conn, "select 1 as ok from users where id = %s", (user_id,)) is None:
            raise HTTPException(404, "Brugeren findes ikke")
        await school.grant_teacher(conn, user_id, admin.user_id, admin.meta)
        await school.grant_school_admin(conn, user_id, admin.user_id, admin.meta)
    return {"ok": True}


@admin_router.delete("/users/{user_id}/school-admin")
async def admin_revoke_school_admin(user_id: int, admin: Principal = Depends(require_admin_stepup)):
    async with db.connection() as conn:
        await school.revoke_school_admin(conn, user_id, admin.user_id, admin.meta)
    return {"ok": True}
