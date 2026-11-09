-- Skole: the school administrator console on /school/admin (teachers, students, classes, usage, subscription and
-- add-ons). Spec: specs/classroom/README.md. Code: app/school.py, app/routes/school.py.

-- School administrators. Granted by a Gær admin in /admin/users or by another school administrator in /school/admin.
-- Users with the admin role count as school administrators without a row here.
create table school_admins (
    user_id     bigint primary key references users (id) on delete cascade,
    granted_by  bigint references users (id) on delete set null,
    created_at  timestamptz not null default now()
);

-- A teacher or school administrator added by e-mail before they have logged in. Applied (and deleted) the first time
-- that user opens /school.
create table school_invites (
    email       text primary key check (email = lower(email) and position('@' in email) > 1),
    role        text not null check (role in ('teacher', 'school_admin')),
    invited_by  bigint references users (id) on delete set null,
    created_at  timestamptz not null default now()
);

-- The school's subscription, invoiced by EAN. One row. Plan, seats, storage and prices are set by a Gær admin; the
-- school administrator keeps the EAN number, the invoice reference and the contact person up to date.
create table school_subscription (
    id                 smallint primary key default 1 check (id = 1),
    plan               text not null default 'Skole',
    seats              integer not null default 0 check (seats >= 0),
    storage_gb         integer not null default 0 check (storage_gb >= 0),
    price_text         text not null default '',
    renews_on          date,
    ean                text check (ean ~ '^[0-9]{13}$'),
    invoice_reference  text,
    contact_user_id    bigint references users (id) on delete set null,
    updated_by         bigint references users (id) on delete set null,
    updated_at         timestamptz not null default now()
);

-- Add-ons ordered by a school administrator. Nothing is paid here: an order is registered, announced to Happy Mates
-- (webhook school.addon.ordered) and invoiced on the next EAN invoice. The catalog is in app/school.py.
create table school_addons (
    id            bigserial primary key,
    addon         text not null,
    quantity      integer not null default 1 check (quantity between 1 and 100),
    status        text not null default 'active' check (status in ('active', 'cancelled')),
    ordered_by    bigint references users (id) on delete set null,
    ordered_at    timestamptz not null default now(),
    cancelled_by  bigint references users (id) on delete set null,
    cancelled_at  timestamptz
);
create index school_addons_active_idx on school_addons (addon) where status = 'active';
