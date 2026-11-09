-- Klasse: a teacher connects a GitHub organisation (through the Gær GitHub App), creates classes from a
-- template repo, and every student gets their own private repo in the organisation with admin rights.
-- Spec: specs/classroom/README.md. Code: app/github.py, app/classroom.py, app/routes/classroom.py.

-- Who may connect organisations and create classes. Granted by an admin in /admin/users.
-- Admins count as teachers without a row here.
create table teachers (
    user_id     bigint primary key references users (id) on delete cascade,
    granted_by  bigint references users (id) on delete set null,
    created_at  timestamptz not null default now()
);

-- A user's verified GitHub account (proved through GitHub's user authorisation, never used to log in).
create table github_accounts (
    user_id      bigint primary key references users (id) on delete cascade,
    github_id    bigint not null unique,
    login        text not null,
    verified_at  timestamptz not null default now()
);
create index github_accounts_login_idx on github_accounts (lower(login));

-- A GitHub organisation where the Gær GitHub App is installed.
create table github_orgs (
    id               bigserial primary key,
    installation_id  bigint not null unique,
    login            text not null,
    connected_by     bigint references users (id) on delete set null,
    created_at       timestamptz not null default now(),
    disconnected_at  timestamptz                          -- the app was uninstalled on GitHub
);

-- Teachers who have proved access to the installation (each through their own GitHub authorisation).
create table github_org_teachers (
    org_id      bigint not null references github_orgs (id) on delete cascade,
    user_id     bigint not null references users (id) on delete cascade,
    created_at  timestamptz not null default now(),
    primary key (org_id, user_id)
);

create table classes (
    id             bigserial primary key,
    org_id         bigint not null references github_orgs (id) on delete cascade,
    name           text not null,
    slug           text not null check (slug ~ '^[a-z0-9][a-z0-9-]{0,38}$'),
    join_code      text not null unique,
    template_repo  text not null,                        -- owner/name, must be a template repository
    student_team   text,                                 -- GitHub team slugs, set once created
    teacher_team   text,
    created_by     bigint references users (id) on delete set null,
    created_at     timestamptz not null default now(),
    archived_at    timestamptz,
    unique (org_id, slug)
);

create table class_members (
    class_id   bigint not null references classes (id) on delete cascade,
    user_id    bigint not null references users (id) on delete cascade,
    role       text not null check (role in ('teacher', 'student')),
    joined_at  timestamptz not null default now(),
    primary key (class_id, user_id)
);
create index class_members_user_idx on class_members (user_id);

-- One repo per student per class, and where provisioning has got to:
--   waiting_account  the student has not connected a GitHub account yet
--   pending          ready to invite
--   invited          invited to the organisation, waiting for the student to accept
--   created          repo generated from the template, rights not set yet
--   ready            the student is admin on the repo, the teachers' team maintains it
--   transferred      moved to the student's own GitHub account (end of course)
--   failed           last_error says why; provisioning again retries
create table student_repos (
    class_id    bigint not null references classes (id) on delete cascade,
    user_id     bigint not null references users (id) on delete cascade,
    repo_name   text,
    status      text not null default 'waiting_account'
                check (status in ('waiting_account', 'pending', 'invited', 'created', 'ready', 'transferred', 'failed')),
    last_error  text,
    attempts    integer not null default 0,
    repo_created_at  timestamptz,                        -- so a retry after a later failure does not generate twice
    updated_at  timestamptz not null default now(),
    primary key (class_id, user_id),
    unique (class_id, repo_name)
);

insert into features (key, title, description, area, ring, builtin) values
    ('klasse', 'Klasse', 'GitHub-klasser: lærer forbinder en organisation, hver elev får sit eget repo', 'bruger', 1, true)
on conflict (key) do nothing;
