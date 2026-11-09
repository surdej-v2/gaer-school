# Klasse — GitHub classes (`/class`)

A teacher connects a GitHub organisation, creates classes from a template repo, and every student gets their own
private repo in the organisation with **admin** rights. Design canvas:
https://claude.ai/artifact/9kcLpSHD1VfAQAeXhPf1F1

| Route | Who | What |
| --- | --- | --- |
| `/class` | logged in | Dashboard: organisation, key figures, classes, "Kræver handling". Students see their classes. |
| `/class/teacher` | teachers | Create classes, share the join code, create repos, follow each student's status, transfer at course end. |
| `/class/student` | logged in | Connect GitHub → accept the invitation → own repo. Join with a code (`?kode=` prefills it). |

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
- **Teachers** = users with the admin role, or users an admin marks as teacher in `/admin/users` (table `teachers`).
  A teacher only sees organisations they proved access to (`github_org_teachers`) and classes they teach (`class_members`).

## Flow

1. Teacher → *Forbind organisation* → installs the app on GitHub → GitHub sends the browser to
   `/api/class/github/setup?installation_id=…` → Gær asks for the teacher's GitHub authorisation (signed `state`
   bound to the Gær user and installation) → `/api/class/github/callback` checks `GET /user/installations`
   contains the installation and that it is an **Organization** → `github_orgs` + `github_org_teachers`.
2. Teacher creates a class: the template must have `is_template`; Gær creates the teams `<slug>` (students) and
   `<slug>-laerere` (teachers, the teacher added as maintainer) and a join code `XXXX-XXXX`.
3. Student joins with the code (`class_members`, `student_repos` = `waiting_account` or `pending`) and links GitHub.
4. Provisioning (`app/classroom.py`, one student at a time, row locked with `for update nowait`):
   invite to the student team → `invited` until accepted → generate repo (`created`, `repo_created_at`) →
   student `admin` + teachers' team `maintain` → `ready`. Errors → `failed` + `last_error`; *Opret manglende repos* retries,
   and a repo that was already generated is never generated again.
5. The webhook `POST /api/public/github/webhook` (HMAC `X-Hub-Signature-256`, CSRF-exempt) moves invited students on
   when `organization.member_added` arrives, and marks the organisation disconnected on `installation.deleted`.

Every change writes `audit()` in the same transaction. Webhook-driven changes have no actor.

## Personal data

`github_accounts` (GitHub id + login), `class_members`, `student_repos`, `teachers`, `github_org_teachers` are in the
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
