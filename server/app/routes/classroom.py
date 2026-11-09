"""/api/class: GitHub classes. Spec: specs/classroom/README.md, logic: app/classroom.py, client: app/github.py.

Teachers (admin role, or granted in /admin/users) connect a GitHub organisation and create classes from a template
repo. Students join with a code, link their GitHub account and get their own private repo with admin rights.
"""

import json
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app import classroom, db, github, school, settings_store
from app.logs import audit
from app.sessions import (Principal, current_user, optional_principal, request_meta, require_admin,
                          require_admin_stepup)

router = APIRouter(prefix="/api/class", tags=["class"])
public_router = APIRouter(prefix="/api/public/github", tags=["class"])
legacy_router = APIRouter(include_in_schema=False)  # /class → /school (shared join links keep working)
admin_router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

STATUS_DONE = ("ready", "transferred")


def _not_configured() -> HTTPException:
    return HTTPException(503, "GitHub er ikke sat op på denne installation endnu")


async def _require_teacher(conn, p: Principal) -> None:
    if not await classroom.is_teacher(conn, p.user_id, p.role):
        raise HTTPException(403, "Kræver lærer-adgang. En administrator kan give dig den.")


async def _teacher_class(conn, class_id: int, p: Principal) -> dict:
    row = await db.fetch_one(conn, """
        select c.*, o.login as org, o.installation_id, o.disconnected_at
        from classes c join github_orgs o on o.id = c.org_id
        join class_members m on m.class_id = c.id and m.user_id = %s and m.role = 'teacher'
        where c.id = %s""", (p.user_id, class_id))
    if row is None:
        raise HTTPException(404, "Klassen findes ikke")
    return row


def _repo_links(org: str, repo: str | None, status: str | None, created: bool) -> dict:
    if not repo or not (created or status in STATUS_DONE):
        return {"repo_url": None, "clone_url": None}
    return {"repo_url": classroom.web_url(org, repo), "clone_url": classroom.web_url(org, repo) + ".git"}


# ── Overview for the logged-in user ────────────────────────────────────────

@router.get("/me")
async def my_classes(p: Principal = Depends(current_user)):
    async with db.connection() as conn:
        await school.apply_invites(conn, p.user_id, p.email, p.meta)
        teacher = await classroom.is_teacher(conn, p.user_id, p.role)
        school_admin = await school.is_school_admin(conn, p.user_id, p.role)
        can_create = school_admin or (teacher and bool(await settings_store.get(conn, "school_teachers_create_classes")))
        account = await db.fetch_one(conn, "select login, verified_at from github_accounts where user_id = %s",
                                     (p.user_id,))
        orgs = await db.fetch_all(conn, """
            select o.id, o.login, o.disconnected_at, o.created_at from github_orgs o
            join github_org_teachers t on t.org_id = o.id and t.user_id = %s order by o.login""",
            (p.user_id,)) if teacher else []
        teaching = await db.fetch_all(conn, """
            select c.id, c.name, c.slug, c.join_code, c.template_repo, c.archived_at, c.created_at, o.login as org,
                   count(sr.user_id) as students,
                   count(sr.user_id) filter (where sr.status in ('ready', 'transferred')) as ready,
                   count(sr.user_id) filter (where sr.status in ('waiting_account', 'pending', 'invited', 'created')) as waiting,
                   count(sr.user_id) filter (where sr.status = 'failed') as failed,
                   count(sr.user_id) filter (where sr.status = 'waiting_account') as no_account,
                   count(sr.user_id) filter (where sr.status = 'invited') as invited
            from classes c join github_orgs o on o.id = c.org_id
            join class_members m on m.class_id = c.id and m.user_id = %s and m.role = 'teacher'
            left join student_repos sr on sr.class_id = c.id
            group by c.id, o.login order by c.archived_at nulls first, c.created_at desc""", (p.user_id,))
        learning = await db.fetch_all(conn, """
            select c.id, c.name, o.login as org, sr.repo_name, sr.status, sr.last_error,
                   sr.repo_created_at is not null as repo_created, m.joined_at,
                   (select string_agg(coalesce(u.display_name, 'Lærer'), ', ' order by u.display_name)
                      from class_members t join users u on u.id = t.user_id
                     where t.class_id = c.id and t.role = 'teacher') as teachers
            from class_members m join classes c on c.id = m.class_id join github_orgs o on o.id = c.org_id
            left join student_repos sr on sr.class_id = c.id and sr.user_id = m.user_id
            where m.user_id = %s and m.role = 'student' and c.archived_at is null
            order by m.joined_at desc""", (p.user_id,))
    for c in learning:
        c.update(_repo_links(c["org"], c["repo_name"], c["status"], c.pop("repo_created")))
        c["invite_url"] = classroom.web_url("orgs", c["org"], "invitation")
    return {"github": {"configured": github.enabled(), "account": account},
            "is_teacher": teacher, "is_school_admin": school_admin, "can_create_classes": can_create, "orgs": orgs, "teaching": teaching, "learning": learning}


# ── Linking a GitHub account and connecting an organisation ────────────────

class StartIn(BaseModel):
    purpose: str = Field(pattern="^(link|install)$")


@router.post("/github/start")
async def github_start(body: StartIn, p: Principal = Depends(current_user)):
    """Returns the GitHub URL to send the browser to. install: install the app on an organisation (GitHub then
    sends the browser to /api/class/github/setup). link: prove which GitHub account is yours."""
    if not github.enabled():
        raise _not_configured()
    if body.purpose == "install":
        async with db.connection() as conn:
            await _require_teacher(conn, p)
        return {"url": github.install_url(classroom.sign_state(p.user_id, "install"))}
    return {"url": github.authorize_url(classroom.sign_state(p.user_id, "link"))}


def _back(path: str, **params: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?{urlencode(params)}" if params else path, status_code=303)


@router.get("/github/setup")
async def github_setup(request: Request, installation_id: int | None = None, setup_action: str | None = None):
    """GitHub's Setup URL after the app was installed. The installation id is only a hint: the teacher proves
    access to it through their own GitHub authorisation in the next step."""
    p = await optional_principal(request)
    if p is None or p.scope != "full":
        return RedirectResponse("/login", status_code=303)
    if not installation_id or not github.enabled():
        return _back("/school/teacher", fejl="installation")
    return RedirectResponse(github.authorize_url(classroom.sign_state(p.user_id, "install", installation_id)),
                            status_code=303)


@router.get("/github/callback")
async def github_callback(request: Request, code: str | None = None,
                          state: str | None = None):
    p = await optional_principal(request)
    if p is None or p.scope != "full":
        return RedirectResponse("/login", status_code=303)
    data = classroom.read_state(state)
    if data is None or data.get("u") != p.user_id or not code or not github.enabled():
        return _back("/school", fejl="state")
    install = data.get("p") == "install"
    home = "/school/teacher" if install else "/school/student"
    try:
        token = await github.exchange_code(code)
        gh_user = await github.get_user(token)
        installations = await github.user_installation_ids(token) if install else set()
        installation = await github.get_installation(int(data["i"])) if install and data.get("i") else None
    except github.GitHubError:
        return _back(home, fejl="github")

    async with db.connection() as conn:
        other = await db.fetch_one(conn, "select user_id from github_accounts where github_id = %s and user_id <> %s",
                                   (gh_user["id"], p.user_id))
        if other:
            return _back(home, fejl="konto-i-brug")
        before = await db.fetch_one(conn, "select github_id, login from github_accounts where user_id = %s for update",
                                    (p.user_id,))
        await conn.execute("""
            insert into github_accounts (user_id, github_id, login) values (%s, %s, %s)
            on conflict (user_id) do update set github_id = excluded.github_id, login = excluded.login, verified_at = now()""",
            (p.user_id, gh_user["id"], gh_user["login"]))
        after = {"github_id": gh_user["id"], "login": gh_user["login"]}
        if before != after:
            await audit(conn, "update" if before else "create", actor_id=p.user_id, meta=p.meta,
                        table="github_accounts", row_id=p.user_id, before=before, after=after)
        # Students who were waiting for an account can now be provisioned.
        for r in await db.fetch_all(conn, """update student_repos set status = 'pending', updated_at = now()
                                             where user_id = %s and status = 'waiting_account' returning class_id""",
                                    (p.user_id,)):
            await audit(conn, "update", actor_id=p.user_id, meta=p.meta, table="student_repos",
                        row_id=f"{r['class_id']}:{p.user_id}", before={"status": "waiting_account"},
                        after={"status": "pending"})

        if install:
            if not await classroom.is_teacher(conn, p.user_id, p.role):
                return _back(home, fejl="ikke-laerer")
            if installation is None or int(data["i"]) not in installations:
                return _back(home, fejl="ingen-adgang")
            account = installation.get("account") or {}
            if account.get("type") != "Organization":
                return _back(home, fejl="ikke-org")
            org = await db.fetch_one(conn, """
                insert into github_orgs (installation_id, login, connected_by) values (%s, %s, %s)
                on conflict (installation_id) do update set login = excluded.login, disconnected_at = null
                returning id, (xmax = 0) as inserted""", (installation["id"], account["login"], p.user_id))
            if org["inserted"]:
                await audit(conn, "create", actor_id=p.user_id, meta=p.meta, table="github_orgs", row_id=org["id"],
                            after={"login": account["login"], "installation_id": installation["id"]})
            added = await db.fetch_one(conn, """insert into github_org_teachers (org_id, user_id) values (%s, %s)
                                                on conflict do nothing returning org_id""", (org["id"], p.user_id))
            if added:
                await audit(conn, "create", actor_id=p.user_id, meta=p.meta, table="github_org_teachers",
                            row_id=f"{org['id']}:{p.user_id}", after={"org": account["login"]})
            return _back(home, forbundet=account["login"])

        await classroom.enqueue_user(conn, p.user_id, p.user_id)
    return _back(home, github="forbundet")


@router.delete("/github/account")
async def unlink_account(p: Principal = Depends(current_user)):
    """Unlinks the GitHub account in Gær. Repos and rights on GitHub are left as they are."""
    async with db.connection() as conn:
        row = await db.fetch_one(conn, "delete from github_accounts where user_id = %s returning github_id, login",
                                 (p.user_id,))
        if row is None:
            raise HTTPException(404, "Ingen GitHub-konto er forbundet")
        await audit(conn, "delete", actor_id=p.user_id, meta=p.meta, table="github_accounts", row_id=p.user_id,
                    before=row)
    return {"ok": True}


# ── Classes (teacher) ──────────────────────────────────────────────────────

class ClassIn(BaseModel):
    org_id: int
    name: str = Field(min_length=1, max_length=80)
    template_repo: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")


@router.post("/classes")
async def create_class(body: ClassIn, p: Principal = Depends(current_user)):
    if not github.enabled():
        raise _not_configured()
    async with db.connection() as conn:
        await _require_teacher(conn, p)
        if not await settings_store.get(conn, "school_teachers_create_classes") \
                and not await school.is_school_admin(conn, p.user_id, p.role):
            raise HTTPException(403, "På denne skole opretter skolens administrator klasserne")
        org = await db.fetch_one(conn, """
            select o.id, o.login, o.installation_id from github_orgs o
            join github_org_teachers t on t.org_id = o.id and t.user_id = %s
            where o.id = %s and o.disconnected_at is null""", (p.user_id, body.org_id))
        if org is None:
            raise HTTPException(404, "Organisationen er ikke forbundet")
        account = await db.fetch_one(conn, "select login from github_accounts where user_id = %s", (p.user_id,))
        base = classroom.slugify(body.name)
        taken = {r["slug"] for r in await db.fetch_all(
            conn, "select slug from classes where org_id = %s and slug like %s", (org["id"], base + "%"))}
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base[:35]}-{n}", n + 1

    inst, login = org["installation_id"], org["login"]
    try:
        status, tpl = await github.call(inst, "GET", f"/repos/{body.template_repo}")
        if status != 200 or not tpl.get("is_template"):
            raise HTTPException(400, "Skabelonen findes ikke, eller den er ikke markeret som skabelon (Template repository) på GitHub")
        teams = {}
        for key, name, desc in (("student", slug, f"Elever i {body.name}"),
                                ("teacher", f"{slug}-laerere", f"Lærere i {body.name}")):
            status, data = await github.call(inst, "POST", f"/orgs/{login}/teams",
                                             {"name": name, "description": desc, "privacy": "closed"})
            if status == 201:
                teams[key] = data["slug"]
            elif status == 422:  # a team with that name exists already: use it
                teams[key] = name
            else:
                raise github.fail(status, data, "Holdet kunne ikke oprettes")
        if account:
            await github.call(inst, "PUT", f"/orgs/{login}/teams/{teams['teacher']}/memberships/{account['login']}",
                              {"role": "maintainer"})
    except github.GitHubError as e:
        raise HTTPException(502, str(e))

    async with db.connection() as conn:
        row = await db.fetch_one(conn, """
            insert into classes (org_id, name, slug, join_code, template_repo, student_team, teacher_team, created_by)
            values (%s, %s, %s, %s, %s, %s, %s, %s) returning id, name, slug, join_code, template_repo""",
            (org["id"], body.name.strip(), slug, classroom.new_join_code(), body.template_repo,
             teams["student"], teams["teacher"], p.user_id))
        await conn.execute("insert into class_members (class_id, user_id, role) values (%s, %s, 'teacher')",
                           (row["id"], p.user_id))
        await audit(conn, "create", actor_id=p.user_id, meta=p.meta, table="classes", row_id=row["id"],
                    after={"name": row["name"], "org": login, "template_repo": body.template_repo, "teams": teams})
    return row


@router.get("/classes/{class_id}")
async def class_detail(class_id: int, p: Principal = Depends(current_user)):
    async with db.connection() as conn:
        c = await _teacher_class(conn, class_id, p)
        students = await db.fetch_all(conn, """
            select m.user_id, coalesce(u.display_name, ga.login, 'Elev #' || u.id) as name, ga.login as github_login,
                   sr.repo_name, sr.status, sr.last_error, sr.attempts, sr.updated_at,
                   sr.repo_created_at is not null as repo_created, m.joined_at
            from class_members m join users u on u.id = m.user_id
            left join github_accounts ga on ga.user_id = m.user_id
            left join student_repos sr on sr.class_id = m.class_id and sr.user_id = m.user_id
            where m.class_id = %s and m.role = 'student' order by name""", (class_id,))
        teachers = await db.fetch_all(conn, """
            select u.id, coalesce(u.display_name, 'Lærer') as name, ga.login as github_login
            from class_members m join users u on u.id = m.user_id left join github_accounts ga on ga.user_id = u.id
            where m.class_id = %s and m.role = 'teacher' order by name""", (class_id,))
    for s in students:
        s.update(_repo_links(c["org"], s["repo_name"], s["status"], s.pop("repo_created")))
    return {"id": c["id"], "name": c["name"], "slug": c["slug"], "org": c["org"], "join_code": c["join_code"],
            "template_repo": c["template_repo"], "student_team": c["student_team"], "teacher_team": c["teacher_team"],
            "archived_at": c["archived_at"], "disconnected": c["disconnected_at"] is not None,
            "template_url": classroom.web_url(c["template_repo"]),
            "team_url": classroom.web_url("orgs", c["org"], "teams", c["student_team"] or ""),
            "students": students, "teachers": teachers}


@router.post("/classes/{class_id}/provision")
async def provision(class_id: int, p: Principal = Depends(current_user)):
    """Creates the missing repos and retries failed ones as a background job; the page polls the class."""
    if not github.enabled():
        raise _not_configured()
    async with db.connection() as conn:
        c = await _teacher_class(conn, class_id, p)
        if c["archived_at"] or c["disconnected_at"]:
            raise HTTPException(409, "Klassen er arkiveret, eller organisationen er ikke længere forbundet")
        n = await db.fetch_one(conn, """select count(*) as n from student_repos where class_id = %s
                                        and status not in ('ready', 'transferred')""", (class_id,))
        await classroom.enqueue_class(conn, class_id, p.user_id)
    return {"queued": n["n"]}


@router.post("/classes/{class_id}/students/{user_id}/transfer")
async def transfer(class_id: int, user_id: int, p: Principal = Depends(current_user)):
    """End of course: moves the repo to the student's own GitHub account. GitHub asks the student to accept."""
    async with db.connection() as conn:
        c = await _teacher_class(conn, class_id, p)
        row = await db.fetch_one(conn, """
            select sr.class_id, sr.user_id, sr.repo_name, sr.status, ga.login from student_repos sr
            join github_accounts ga on ga.user_id = sr.user_id
            where sr.class_id = %s and sr.user_id = %s for update of sr""", (class_id, user_id))
        if row is None or row["status"] != "ready":
            raise HTTPException(409, "Kun et færdigt repo kan overdrages")
        try:
            status, data = await github.call(c["installation_id"], "POST", f"/repos/{c['org']}/{row['repo_name']}/transfer",
                                             {"new_owner": row["login"]})
        except github.GitHubError as e:
            raise HTTPException(502, str(e))
        if status != 202:
            raise HTTPException(502, str(github.fail(status, data, "Repoet kunne ikke overdrages")))
        await conn.execute("""update student_repos set status = 'transferred', updated_at = now()
                              where class_id = %s and user_id = %s""", (class_id, user_id))
        await audit(conn, "update", actor_id=p.user_id, meta=p.meta, table="student_repos",
                    row_id=f"{class_id}:{user_id}", before={"status": "ready"},
                    after={"status": "transferred", "new_owner": row["login"]})
    return {"ok": True}


@router.delete("/classes/{class_id}/students/{user_id}")
async def remove_student(class_id: int, user_id: int, p: Principal = Depends(current_user)):
    """Takes the student out of the class in Gær. The repo on GitHub is left as it is."""
    async with db.connection() as conn:
        await _teacher_class(conn, class_id, p)
        repo = await db.fetch_one(conn, """delete from student_repos where class_id = %s and user_id = %s
                                           returning repo_name, status""", (class_id, user_id))
        member = await db.fetch_one(conn, """delete from class_members where class_id = %s and user_id = %s
                                             and role = 'student' returning joined_at""", (class_id, user_id))
        if member is None:
            raise HTTPException(404, "Eleven er ikke i klassen")
        await audit(conn, "delete", actor_id=p.user_id, meta=p.meta, table="class_members",
                    row_id=f"{class_id}:{user_id}", before={"role": "student", **(repo or {})})
    return {"ok": True}


@router.post("/classes/{class_id}/archive")
async def archive(class_id: int, p: Principal = Depends(current_user)):
    async with db.connection() as conn:
        c = await _teacher_class(conn, class_id, p)
        if c["archived_at"]:
            return {"ok": True}
        await conn.execute("update classes set archived_at = now() where id = %s", (class_id,))
        await audit(conn, "update", actor_id=p.user_id, meta=p.meta, table="classes", row_id=class_id,
                    before={"archived": False}, after={"archived": True})
    return {"ok": True}


# ── Joining (student) ──────────────────────────────────────────────────────

class JoinIn(BaseModel):
    code: str = Field(min_length=4, max_length=20)


@router.post("/join")
async def join(body: JoinIn, p: Principal = Depends(current_user)):
    code = body.code.strip().upper().replace(" ", "")
    if len(code) == 8 and "-" not in code:
        code = f"{code[:4]}-{code[4:]}"
    async with db.connection() as conn:
        c = await db.fetch_one(conn, "select id, name from classes where join_code = %s and archived_at is null", (code,))
        if c is None:
            raise HTTPException(404, "Koden passer ikke til en klasse")
        member = await db.fetch_one(conn, "select role from class_members where class_id = %s and user_id = %s",
                                    (c["id"], p.user_id))
        if member:
            if member["role"] == "teacher":
                raise HTTPException(409, "Du er lærer i klassen")
            return {"id": c["id"], "name": c["name"], "joined": False}
        if not await school.email_allowed(conn, p.email):
            raise HTTPException(403, "Brug din skole-e-mail. Skift den under Konto, og prøv igen.")
        if not await school.has_free_seat(conn, p.user_id):
            raise HTTPException(409, "Skolen har ikke flere elevpladser. Kontakt skolens administrator.")
        account = await db.fetch_one(conn, "select login from github_accounts where user_id = %s", (p.user_id,))
        status = "pending" if account else "waiting_account"
        await conn.execute("insert into class_members (class_id, user_id, role) values (%s, %s, 'student')",
                           (c["id"], p.user_id))
        await conn.execute("insert into student_repos (class_id, user_id, status) values (%s, %s, %s)",
                           (c["id"], p.user_id, status))
        await audit(conn, "create", actor_id=p.user_id, meta=p.meta, table="class_members",
                    row_id=f"{c['id']}:{p.user_id}", after={"class": c["name"], "role": "student", "status": status})
        if account and github.enabled():
            await classroom.enqueue_student(conn, c["id"], p.user_id, p.user_id)
    return {"id": c["id"], "name": c["name"], "joined": True}


# ── Webhook from the GitHub App ────────────────────────────────────────────

@public_router.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    if not github.valid_signature(body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(401, "Ugyldig signatur")
    event = request.headers.get("x-github-event", "")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(400, "Ugyldig JSON")
    action = payload.get("action")
    installation_id = (payload.get("installation") or {}).get("id")
    meta = request_meta(request)

    if event == "organization" and action == "member_added" and installation_id:
        login = ((payload.get("membership") or {}).get("user") or {}).get("login")
        async with db.connection() as conn:
            users = await db.fetch_all(conn, """
                select distinct sr.user_id from student_repos sr
                join classes c on c.id = sr.class_id join github_orgs o on o.id = c.org_id
                join github_accounts ga on ga.user_id = sr.user_id
                where o.installation_id = %s and lower(ga.login) = lower(%s) and sr.status = 'invited'""",
                (installation_id, login or ""))
            for u in users:
                await classroom.enqueue_user(conn, u["user_id"], None)
        return {"ok": True, "students": len(users)}

    if event == "installation" and action == "deleted" and installation_id:
        async with db.connection() as conn:
            org = await db.fetch_one(conn, """update github_orgs set disconnected_at = now()
                                              where installation_id = %s and disconnected_at is null
                                              returning id, login""", (installation_id,))
            if org:
                await audit(conn, "update", actor_id=None, meta=meta, table="github_orgs", row_id=org["id"],
                            before={"connected": True}, after={"connected": False}, reason="app uninstalled on GitHub")
    return {"ok": True}


# ── Teachers are granted by an admin (a role change: needs step-up like /users/{id}/role) ──

@admin_router.put("/users/{user_id}/teacher")
async def grant_teacher(user_id: int, admin: Principal = Depends(require_admin_stepup)):
    async with db.connection() as conn:
        if await db.fetch_one(conn, "select 1 as ok from users where id = %s", (user_id,)) is None:
            raise HTTPException(404, "Brugeren findes ikke")
        row = await db.fetch_one(conn, """insert into teachers (user_id, granted_by) values (%s, %s)
                                          on conflict do nothing returning user_id""", (user_id, admin.user_id))
        if row:
            await audit(conn, "role.changed", actor_id=admin.user_id, meta=admin.meta, table="teachers",
                        row_id=user_id, before={"teacher": False}, after={"teacher": True})
    return {"ok": True}


@admin_router.delete("/users/{user_id}/teacher")
async def revoke_teacher(user_id: int, admin: Principal = Depends(require_admin_stepup)):
    async with db.connection() as conn:
        row = await db.fetch_one(conn, "delete from teachers where user_id = %s returning user_id", (user_id,))
        if row:
            await audit(conn, "role.changed", actor_id=admin.user_id, meta=admin.meta, table="teachers",
                        row_id=user_id, before={"teacher": True}, after={"teacher": False})
    return {"ok": True}


# ── /class moved to /school ────────────────────────────────────────────────

@legacy_router.get("/class")
@legacy_router.get("/class/{rest:path}")
async def class_moved(request: Request, rest: str = ""):
    target = "/school" + (f"/{rest}" if rest else "")
    query = request.url.query
    return RedirectResponse(f"{target}?{query}" if query else target, status_code=301)
