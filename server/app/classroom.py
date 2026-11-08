"""Klasse: provisioning of student repos in a teacher's GitHub organisation.

Each student in a class has one student_repos row. provision_student() moves it forward as far as it can:

    waiting_account → pending → invited → (member) → created → ready      (failed: last_error, retry later)

  1. invite the student to the class's GitHub team (which invites them to the organisation)
  2. once they are a member: generate <slug>-<login> from the class's template repo, private
  3. give the student admin on the repo and the class's teachers' team maintain

Every status change is audited in the same transaction as the change. The row is locked (for update nowait)
while a student is provisioned, so two runs never create the same repo twice.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import time

from psycopg import errors

from app import config, db, github
from app.logs import RequestMeta, audit

STATE_TTL_SECONDS = 600
JOIN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I


# ── Who is a teacher ────────────────────────────────────────────────────────

async def is_teacher(conn, user_id: int, role: str) -> bool:
    if role == "admin":
        return True
    return await db.fetch_one(conn, "select 1 as ok from teachers where user_id = %s", (user_id,)) is not None


# ── Signed state for GitHub redirects (bound to the Gær user) ──────────────

def sign_state(user_id: int, purpose: str, installation_id: int | None = None) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(
        {"u": user_id, "p": purpose, "i": installation_id, "e": int(time.time()) + STATE_TTL_SECONDS,
         "n": secrets.token_hex(8)}, separators=(",", ":")).encode()).decode().rstrip("=")
    mac = hmac.new(config.settings.secret_key.encode(), b"class-state:" + payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{mac}"


def read_state(state: str | None) -> dict | None:
    if not state or "." not in state:
        return None
    payload, mac = state.rsplit(".", 1)
    expected = hmac.new(config.settings.secret_key.encode(), b"class-state:" + payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return None
    if data.get("e", 0) < time.time():
        return None
    return data


# ── Names ──────────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    s = name.lower()
    for a, b in (("æ", "ae"), ("ø", "oe"), ("å", "aa")):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s or "klasse")[:39].strip("-") or "klasse"


def repo_name_for(slug: str, login: str) -> str:
    return f"{slug}-{login.lower()}"[:100]


def new_join_code() -> str:
    raw = "".join(secrets.choice(JOIN_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def web_url(*parts: str) -> str:
    return "/".join([config.settings.github_web_url.rstrip("/"), *parts])


# ── Provisioning ───────────────────────────────────────────────────────────

async def _set_status(conn, row: dict, status: str, *, actor_id: int | None, meta: RequestMeta,
                      repo_name: str | None = None, error: str | None = None, created: bool = False) -> None:
    repo_name = repo_name or row["repo_name"]
    await conn.execute("""
        update student_repos set status = %s, repo_name = %s, last_error = %s, updated_at = now(),
               attempts = attempts + case when %s = 'failed' then 1 else 0 end,
               repo_created_at = case when %s then coalesce(repo_created_at, now()) else repo_created_at end
        where class_id = %s and user_id = %s""",
        (status, repo_name, error, status, created, row["class_id"], row["user_id"]))
    if status != row["status"] or repo_name != row["repo_name"] or error:
        await audit(conn, "update", actor_id=actor_id, meta=meta, table="student_repos",
                    row_id=f"{row['class_id']}:{row['user_id']}",
                    before={"status": row["status"], "repo_name": row["repo_name"]},
                    after={"status": status, "repo_name": repo_name}, error=error)


async def provision_student(class_id: int, user_id: int, *, actor_id: int | None, meta: RequestMeta) -> str | None:
    """Moves one student's repo as far forward as GitHub allows. Returns the new status, or None when the
    row is missing, already final or being provisioned by another run."""
    async with db.connection() as conn:
        try:
            row = await db.fetch_one(conn, """
                select sr.class_id, sr.user_id, sr.repo_name, sr.status, sr.repo_created_at,
                       c.slug, c.template_repo, c.student_team, c.teacher_team, c.archived_at,
                       o.installation_id, o.login as org, o.disconnected_at, ga.login
                from student_repos sr
                join classes c on c.id = sr.class_id
                join github_orgs o on o.id = c.org_id
                left join github_accounts ga on ga.user_id = sr.user_id
                where sr.class_id = %s and sr.user_id = %s
                for update of sr nowait""", (class_id, user_id))
        except errors.LockNotAvailable:
            return None
        if row is None or row["status"] in ("ready", "transferred") or row["archived_at"] or row["disconnected_at"]:
            return None
        if not row["login"]:
            await _set_status(conn, row, "waiting_account", actor_id=actor_id, meta=meta)
            return "waiting_account"

        login, org, inst = row["login"], row["org"], row["installation_id"]
        repo = row["repo_name"] or repo_name_for(row["slug"], login)
        try:
            # 1. Member of the organisation (through the class's team)?
            if not row["repo_created_at"]:
                status, data = await github.call(inst, "GET", f"/orgs/{org}/teams/{row['student_team']}/memberships/{login}")
                state = data.get("state") if status == 200 and isinstance(data, dict) else None
                if state is None:
                    status, data = await github.call(inst, "PUT", f"/orgs/{org}/teams/{row['student_team']}/memberships/{login}",
                                                     {"role": "member"})
                    if status != 200:
                        raise github.fail(status, data, "Invitationen kunne ikke sendes")
                    state = data.get("state")
                if state != "active":
                    await _set_status(conn, row, "invited", actor_id=actor_id, meta=meta, repo_name=repo)
                    return "invited"

                # 2. The repo, generated from the template.
                status, data = await github.call(inst, "POST", f"/repos/{row['template_repo']}/generate",
                                                 {"owner": org, "name": repo, "private": True,
                                                  "description": f"{login} · {row['slug']}"})
                if status == 422:
                    raise github.GitHubError(422, f"Repoet {repo} findes allerede i organisationen")
                if status != 201:
                    raise github.fail(status, data, "Repoet kunne ikke oprettes")
                await _set_status(conn, row, "created", actor_id=actor_id, meta=meta, repo_name=repo, created=True)
                row = {**row, "status": "created", "repo_name": repo}

            # 3. The student is admin on their own repo, the teachers' team maintains it.
            status, data = await github.call(inst, "PUT", f"/repos/{org}/{repo}/collaborators/{login}",
                                             {"permission": "admin"})
            if status not in (201, 204):
                raise github.fail(status, data, "Eleven kunne ikke gøres til admin")
            if row["teacher_team"]:
                status, data = await github.call(inst, "PUT", f"/orgs/{org}/teams/{row['teacher_team']}/repos/{org}/{repo}",
                                                 {"permission": "maintain"})
                if status != 204:
                    raise github.fail(status, data, "Lærerne kunne ikke få adgang")
            await _set_status(conn, row, "ready", actor_id=actor_id, meta=meta, repo_name=repo)
            return "ready"
        except github.GitHubError as e:
            await _set_status(conn, row, "failed", actor_id=actor_id, meta=meta, repo_name=repo, error=str(e)[:500])
            return "failed"


async def provision_class(class_id: int, *, actor_id: int | None, meta: RequestMeta) -> None:
    async with db.connection() as conn:
        rows = await db.fetch_all(conn, """select user_id from student_repos where class_id = %s
                                           and status not in ('ready', 'transferred') order by user_id""", (class_id,))
    for r in rows:
        await provision_student(class_id, r["user_id"], actor_id=actor_id, meta=meta)


async def provision_user(user_id: int, *, actor_id: int | None, meta: RequestMeta) -> None:
    """After a student links a GitHub account or an organisation reports a new member."""
    async with db.connection() as conn:
        rows = await db.fetch_all(conn, """select class_id from student_repos where user_id = %s
                                           and status not in ('ready', 'transferred') order by class_id""", (user_id,))
    for r in rows:
        await provision_student(r["class_id"], user_id, actor_id=actor_id, meta=meta)
