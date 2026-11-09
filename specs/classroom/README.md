# Skole — school administration and GitHub classes (`/school`)

A school administrator runs the school on `/school/admin`; a teacher connects a GitHub organisation, creates classes from
a template repo, and every student gets their own private repo in the organisation with **admin** rights. Design canvas:
https://claude.ai/artifact/9kcLpSHD1VfAQAeXhPf1F1

| Route | Who | What |
| --- | --- | --- |
| `/school` | logged in | Sends you to your role: school administrator → `/school/admin`, teacher → `/school/teacher`, else `/school/student`. |
| `/school/admin` | school administrators | Tabs (`?fane=`): Oversigt, Lærere, Elever, Klasser, Forbrug, Tilkøb, Organisation. |
| `/school/teacher` | teachers | Create classes, share the join code, create repos, follow each student's status, transfer at course end. |
| `/school/student` | logged in | Connect GitHub → accept the invitation → own repo. Join with a code (`?kode=` prefills it). |
| `/class/…` | — | 301 to `/school/…`, so join links shared before the move keep working. |

Behind feature `klasse` (ring 1). Raise the ring in `/admin/features` when it is ready for everyone.

## Decisions (defaults)

- **Template, not fork.** GitHub cannot fork a repo into the organisation it already lives in. Each student repo is
  generated from a *template repository* (`POST /repos/{template}/generate`), private, named `<class-slug>-<github-login>`.
- **Ownership = admin in the school's organisation** while the course runs. The class's teachers' team
  (`<slug>-laerere`) gets *maintain* on every repo. At course end a teacher can **transfer** a repo to the student's
  own account (GitHub asks the student to accept).
- **GitHub App, not personal tokens.** Gær acts with installation tokens. The private key stays in an environment variable.
- **GitHub never logs anyone in.** Login to Gær is unchanged. A GitHub account is only *linked* to a Gær user,
  proved through the app's user authorisation. One GitHub account per Gær user.
- **School administrators** = users with the admin role, or a row in `school_admins` (granted in `/admin/users` with
  step-up, or by another school administrator in `/school/admin`). They manage teachers, students, classes, usage,
  limits, the subscription and add-ons, and the school rules.
- **Teachers** = school administrators, or a row in `teachers`. A school administrator adds teachers by e-mail; an e-mail
  that has not logged in yet becomes a `school_invites` row and the role is granted the first time that user opens `/school`.
  A teacher only sees organisations they proved access to (`github_org_teachers`) and classes they teach (`class_members`).

## Flow

1. Teacher → *Forbind organisation* → installs the app on GitHub → GitHub sends the browser to
   `/api/class/github/setup?installation_id=…` → Gær asks for the teacher's GitHub authorisation (signed `state`
   bound to the Gær user and installation) → `/api/class/github/callback` checks `GET /user/installations`
   contains the installation and that it is an **Organization** → `github_orgs` + `github_org_teachers`.
2. Teacher creates a class: the template must have `is_template`; Gær creates the teams `<slug>` (students) and
   `<slug>-laerere` (teachers, the teacher added as maintainer) and a join code `XXXX-XXXX`.
3. Student joins with the code (`class_members`, `student_repos` = `waiting_account` or `pending`) and links GitHub.
4. Provisioning (`app/classroom.py`, background jobs `class.provision_*` in `app.jobs`, one student at a time, row locked with `for update nowait`):
   invite to the student team → `invited` until accepted → generate repo (`created`, `repo_created_at`) →
   student `admin` + teachers' team `maintain` → `ready`. Errors → `failed` + `last_error`; *Opret manglende repos* retries,
   and a repo that was already generated is never generated again.
5. The webhook `POST /api/public/github/webhook` (HMAC `X-Hub-Signature-256`, CSRF-exempt) moves invited students on
   when `organization.member_added` arrives, and marks the organisation disconnected on `installation.deleted`.

Every change writes `audit()` in the same transaction. Webhook-driven changes have no actor.

## School administration (`/school/admin`)

- **Seats**: `school_subscription.seats` + 25 per active `seats` add-on. A student uses a seat while they are in a class
  that is not archived. Joining a class needs a free seat (409 otherwise). Without a subscription row there is no limit.
- **Storage**: `storage_gb` + 100 GB per `storage` add-on, shown against all files (not enforced yet).
- **AI**: "pr. bruger" is the instance default `ai_default_monthly_tokens`; "for hele skolen" (`school_ai_monthly_tokens`,
  0 = none) stops AI for everyone for the rest of the month when reached (`routes/ai.py`).
- **School rules** (`settings`): `school_student_email_domain` (students must log in with that domain to join a class) and
  `school_teachers_create_classes` (otherwise only school administrators create classes).
- **Påmind**: one background job per student without a GitHub account sends a mail (`school.remind_github`).

## Subscription, add-ons and EAN invoicing

Nothing is paid in Gær. Happy Mates invoices the school by **EAN**:

1. A Gær admin (Happy Mates) sets plan, seats, storage, price and add-on prices in Tilkøb → "Abonnement og priser".
2. The school administrator enters the EAN number and invoice reference, and becomes the contact person.
3. Add-ons (catalog in `app/school.py`: `seats`, `storage`, `ai`, `courses`, `support`) are ordered and cancelled by a
   school administrator. Each order is audited and emits the webhook `school.addon.ordered` / `school.addon.cancelled`
   (ids only), so Happy Mates can be notified through `/admin/webhooks`.
4. "Hent fakturagrundlag (CSV)" (`/api/school/admin/billing.csv`) lists the subscription and every order for the invoice.

Only `seats` and `storage` change limits in Gær; the other add-ons are registered for the invoice. GitHub Codespaces and
Actions are bought and paid in the organisation on GitHub.

## Personal data

`github_accounts` (GitHub id + login), `class_members`, `student_repos`, `teachers`, `github_org_teachers`,
`school_admins`, `school_invites` (by e-mail), add-on orders and the billing contact are in the
GDPR export (`build_user_export`) and cascade on erasure. **Repos on GitHub are not deleted** by an erasure — they live
in the school's organisation (or the student's own account after a transfer). Removing a student from a class or
archiving a class does not touch GitHub either.

GitHub requires users to be at least 13. Younger students are out of scope for v1.

## Setting up the GitHub App (once per installation of Gær)

GitHub → Settings → Developer settings → GitHub Apps → *New GitHub App* (or under the school's organisation):

| Setting | Value |
| --- | --- |
| Homepage URL | `ORIGIN` |
| Callback URL | `ORIGIN/api/class/github/callback` |
| Request user authorization (OAuth) during installation | **off** (Gær asks itself after the setup step) |
| Setup URL | `ORIGIN/api/class/github/setup`, *Redirect on update* on |
| Webhook URL | `ORIGIN/api/public/github/webhook`, with a random secret |
| Repository permissions | Administration: **Read & write** · Contents: **Read** · Metadata: Read |
| Organization permissions | Members: **Read & write** |
| Subscribe to events | Organization |
| Where can this app be installed | Any account (other schools) or only your own |

Then generate a private key and set in `.env` (see `deploy/.env.example`):

```
GITHUB_APP_ID=…            # App ID
GITHUB_APP_SLUG=…          # the app's URL name: github.com/apps/<slug>
GITHUB_APP_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\nMIIE…\n-----END RSA PRIVATE KEY-----
GITHUB_CLIENT_ID=…
GITHUB_CLIENT_SECRET=…
GITHUB_WEBHOOK_SECRET=…
```

In the school's organisation: *Settings → Member privileges → Base permissions* = **No permission**, so students only
see their own repo. Mark the class's starting repo as *Template repository*.

## Not in v1

- Sending teacher updates of the template to every student repo as a pull request.
- Co-teachers on a class (beyond the teacher who created it), assignments, deadlines, grading.
- Students under 13 without their own GitHub account.
