"""Skole: roles, seats and add-ons for /school. Routes: app/routes/school.py; classes and repos: app/classroom.py.

Roles (all checked here, never in the frontend):
  school administrator  admin role, or a row in school_admins
  teacher               a school administrator, or a row in teachers
  student               a student member of a class (class_members)

Seats: the subscription's seats plus 25 per active "seats" add-on. A student uses a seat while they are in at least one
class that is not archived. Without a subscription row there is no limit (the school is not set up for invoicing yet).
"""

from dataclasses import dataclass

from app import db, settings_store
from app.logs import audit

SEATS_PER_ADDON = 25
STORAGE_GB_PER_ADDON = 100


@dataclass(frozen=True)
class Addon:
    key: str
    title: str
    description: str
    repeatable: bool = False   # may be ordered several times (each order adds a unit)


ADDONS: dict[str, Addon] = {a.key: a for a in (
    Addon("seats", "Flere elevpladser", f"{SEATS_PER_ADDON} ekstra pladser til elever. Kan bestilles flere gange.", True),
    Addon("storage", "Ekstra lagerplads", f"{STORAGE_GB_PER_ADDON} GB ekstra til filer og afleveringer.", True),
    Addon("ai", "AI-assistent til elever", "AI-chat i appen og i browserudvidelsen, med lofter pr. bruger og for skolen."),
    Addon("courses", "Kursuspakker", "Færdige skabelon-repos med opgaver til 7.–9. klasse og valgfag."),
    Addon("support", "Prioriteret support", "Svar inden for én skoledag og hjælp til opstart af klasser."),
)}


# ── Roles ──────────────────────────────────────────────────────────────────

async def is_school_admin(conn, user_id: int, role: str) -> bool:
    if role == "admin":
        return True
    return await db.fetch_one(conn, "select 1 as ok from school_admins where user_id = %s", (user_id,)) is not None


async def is_teacher(conn, user_id: int, role: str) -> bool:
    if await is_school_admin(conn, user_id, role):
        return True
    return await db.fetch_one(conn, "select 1 as ok from teachers where user_id = %s", (user_id,)) is not None


async def apply_invites(conn, user_id: int, email: str | None, meta) -> list[str]:
    """Turns an invitation for this user's e-mail into roles. Returns the roles granted."""
    if not email:
        return []
    invite = await db.fetch_one(conn, "delete from school_invites where email = %s returning role, invited_by",
                                (email.lower(),))
    if invite is None:
        return []
    granted = ["teacher"]
    await grant_teacher(conn, user_id, invite["invited_by"], meta)
    if invite["role"] == "school_admin":
        await grant_school_admin(conn, user_id, invite["invited_by"], meta)
        granted.append("school_admin")
    return granted


async def grant_teacher(conn, user_id: int, actor_id: int | None, meta) -> bool:
    row = await db.fetch_one(conn, """insert into teachers (user_id, granted_by) values (%s, %s)
                                      on conflict do nothing returning user_id""", (user_id, actor_id))
    if row:
        await audit(conn, "role.changed", actor_id=actor_id, meta=meta, table="teachers", row_id=user_id,
                    before={"teacher": False}, after={"teacher": True})
    return row is not None


async def grant_school_admin(conn, user_id: int, actor_id: int | None, meta) -> bool:
    row = await db.fetch_one(conn, """insert into school_admins (user_id, granted_by) values (%s, %s)
                                      on conflict do nothing returning user_id""", (user_id, actor_id))
    if row:
        await audit(conn, "role.changed", actor_id=actor_id, meta=meta, table="school_admins", row_id=user_id,
                    before={"school_admin": False}, after={"school_admin": True})
    return row is not None


async def revoke_school_admin(conn, user_id: int, actor_id: int | None, meta) -> bool:
    row = await db.fetch_one(conn, "delete from school_admins where user_id = %s returning user_id", (user_id,))
    if row:
        await audit(conn, "role.changed", actor_id=actor_id, meta=meta, table="school_admins", row_id=user_id,
                    before={"school_admin": True}, after={"school_admin": False})
    return row is not None


async def revoke_teacher(conn, user_id: int, actor_id: int | None, meta) -> bool:
    await revoke_school_admin(conn, user_id, actor_id, meta)
    row = await db.fetch_one(conn, "delete from teachers where user_id = %s returning user_id", (user_id,))
    if row:
        await audit(conn, "role.changed", actor_id=actor_id, meta=meta, table="teachers", row_id=user_id,
                    before={"teacher": True}, after={"teacher": False})
    return row is not None


# ── Seats, storage and the subscription ─────────────────────────────────────

async def subscription(conn) -> dict | None:
    return await db.fetch_one(conn, "select * from school_subscription where id = 1")


async def addon_units(conn, key: str) -> int:
    row = await db.fetch_one(conn, """select coalesce(sum(quantity), 0) as n from school_addons
                                      where addon = %s and status = 'active'""", (key,))
    return int(row["n"])


async def seats(conn) -> dict:
    sub = await subscription(conn)
    used = await db.fetch_one(conn, """select count(distinct m.user_id) as n from class_members m
                                       join classes c on c.id = m.class_id
                                       where m.role = 'student' and c.archived_at is null""")
    total = None if sub is None else sub["seats"] + SEATS_PER_ADDON * await addon_units(conn, "seats")
    return {"used": int(used["n"]), "total": total}


async def storage(conn) -> dict:
    sub = await subscription(conn)
    used = await db.fetch_one(conn, "select coalesce(sum(size_bytes), 0) as n from files")
    total = None if sub is None else sub["storage_gb"] + STORAGE_GB_PER_ADDON * await addon_units(conn, "storage")
    return {"used_bytes": int(used["n"]), "total_gb": total}


async def has_free_seat(conn, user_id: int) -> bool:
    """A student who already uses a seat may join more classes; otherwise a free seat is needed."""
    s = await seats(conn)
    if s["total"] is None:
        return True
    already = await db.fetch_one(conn, """select 1 as ok from class_members m join classes c on c.id = m.class_id
                                          where m.user_id = %s and m.role = 'student' and c.archived_at is null""",
                                 (user_id,))
    return already is not None or s["used"] < s["total"]


async def email_allowed(conn, email: str | None) -> bool:
    domain = (await settings_store.get(conn, "school_student_email_domain") or "").strip().lower().lstrip("@")
    if not domain:
        return True
    return bool(email) and email.lower().endswith("@" + domain)


# ── AI for the whole school ────────────────────────────────────────────────

async def school_ai_left(conn) -> int | None:
    """Tokens left this month under the school's cap, or None when there is no cap."""
    cap = int(await settings_store.get(conn, "school_ai_monthly_tokens") or 0)
    if cap <= 0:
        return None
    used = await db.fetch_one(conn, """select coalesce(sum(prompt_tokens + completion_tokens), 0) as n from ai_usage
                                       where ts >= date_trunc('month', now())""")
    return max(0, cap - int(used["n"]))
