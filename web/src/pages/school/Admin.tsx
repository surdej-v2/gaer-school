// /school/admin: the school administrator console. Tabs live in ?fane= so they can be linked and reloaded.
// Oversigt and Organisation are here; Lærere, Elever and Klasser in AdminPeople.tsx; Forbrug and Tilkøb in AdminBilling.tsx.
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../../api";
import { Alert, fmtDate, fmtNumber, Loading, useLoad } from "../../ui";
import { Classes, Students, Teachers } from "./AdminPeople";
import { Addons, Usage } from "./AdminBilling";
import { goToGitHub, useAction, useClassMe } from "./shared";

const TABS = [
  ["oversigt", "Oversigt"], ["laerere", "Lærere"], ["elever", "Elever"], ["klasser", "Klasser"],
  ["forbrug", "Forbrug"], ["tilkob", "Tilkøb"], ["organisation", "Organisation"],
] as const;
type Tab = (typeof TABS)[number][0];

export type Seats = { used: number; total: number | null };

type OverviewData = {
  counts: {
    classes: number; archived: number; repos_ready: number; repos_total: number; repos_failed: number;
    students_without_github: number; invites: number; teachers: number; students: number; teachers_without_github: number;
  };
  seats: Seats;
  orgs: { id: number; login: string; disconnected_at: string | null }[];
  recent: { ts: string; action: string; table_name: string; actor: string; before: Record<string, unknown> | null;
            after: Record<string, unknown> | null }[];
};

const TABLE_LABEL: Record<string, string> = {
  classes: "klasse", class_members: "klassemedlem", student_repos: "elev-repo", teachers: "lærer",
  school_admins: "skoleadmin", school_invites: "invitation", school_addons: "tilkøb", school_subscription: "abonnement",
  github_orgs: "organisation", github_org_teachers: "organisation", settings: "skoleregler",
};

function describe(r: OverviewData["recent"][number]): string {
  const what = TABLE_LABEL[r.table_name] ?? r.table_name;
  if (r.action === "school.addon.ordered") return `bestilte et tilkøb (${String(r.after?.addon ?? "")})`;
  if (r.action === "school.addon.cancelled") return "opsagde et tilkøb";
  if (r.action === "school.reminder") return "sendte påmindelser om GitHub-konto";
  if (r.action === "role.changed") return Object.values(r.after ?? {})[0] ? `gav rollen ${what}` : `fjernede rollen ${what}`;
  if (r.action === "create" && r.table_name === "class_members") return r.after?.role === "student" ? "meldte sig ind i en klasse" : "blev lærer i en klasse";
  if (r.action === "create") return `oprettede ${what}`;
  if (r.action === "delete") return `fjernede ${what}`;
  if (r.table_name === "student_repos" && r.after?.status) return `satte et elev-repo til ${String(r.after.status)}`;
  return `ændrede ${what}`;
}

function Overview({ go }: { go: (t: Tab, extra?: Record<string, string>) => void }) {
  const data = useLoad(() => api.get<OverviewData>("/api/school/admin/overview"));
  if (!data.data) return data.loading ? <Loading /> : <Alert>{data.error}</Alert>;
  const { counts: c, seats, orgs, recent } = data.data;
  const seatsLow = seats.total !== null && seats.total - seats.used <= Math.max(3, Math.round(seats.total * 0.05));
  const todo: { tone: "warn" | "error" | "info"; title: string; body: string; action?: [string, () => void] }[] = [];
  if (!orgs.some((o) => !o.disconnected_at))
    todo.push({ tone: "info", title: "Ingen GitHub-organisation er forbundet", body: "Forbind skolens organisation, før lærerne opretter klasser.",
                action: ["Gå til Organisation", () => go("organisation")] });
  if (seatsLow && seats.total !== null)
    todo.push({ tone: "warn", title: `${fmtNumber(seats.used)} af ${fmtNumber(seats.total)} elevpladser er brugt`,
                body: (() => { const left = Math.max(0, seats.total - seats.used); return left === 0 ? "Der er ikke plads til flere elever." : `Der er plads til ${fmtNumber(left)} ${left === 1 ? "elev" : "elever"} mere.`; })(), action: ["Tilkøb pladser", () => go("tilkob")] });
  if (c.students_without_github)
    todo.push({ tone: "warn", title: `${fmtNumber(c.students_without_github)} ${c.students_without_github === 1 ? "elev mangler" : "elever mangler"} en GitHub-konto`,
                body: "Uden kontoen kan deres repo ikke oprettes.", action: ["Se eleverne", () => go("elever", { status: "no_github" })] });
  if (c.teachers_without_github)
    todo.push({ tone: "warn", title: `${fmtNumber(c.teachers_without_github)} ${c.teachers_without_github === 1 ? "lærer har" : "lærere har"} ikke forbundet GitHub`,
                body: "De kan ikke få adgang til elevernes repos endnu.", action: ["Se lærerne", () => go("laerere")] });
  if (c.repos_failed)
    todo.push({ tone: "error", title: `${fmtNumber(c.repos_failed)} ${c.repos_failed === 1 ? "repo kunne" : "repos kunne"} ikke oprettes`,
                body: "Læreren kan se fejlen og prøve igen i klassen.", action: ["Se eleverne", () => go("elever", { status: "failed" })] });

  const stats: [string, string, string][] = [
    ["Lærere", fmtNumber(c.teachers), c.invites ? `${c.invites} inviteret` : `${c.teachers_without_github} uden GitHub-konto`],
    ["Elever", fmtNumber(c.students), seats.total !== null ? `af ${fmtNumber(seats.total)} pladser` : "ingen pladsgrænse"],
    ["Klasser", fmtNumber(c.classes), `${fmtNumber(c.archived)} arkiveret`],
    ["Repos klar", fmtNumber(c.repos_ready), `af ${fmtNumber(c.repos_total)}`],
  ];
  return (
    <div className="stack" style={{ "--gap": "24px" } as React.CSSProperties}>
      <div className="grid" style={{ "--min": "180px" } as React.CSSProperties}>
        {stats.map(([label, value, sub]) => (
          <div key={label} className="card"><span className="muted small">{label}</span><span className="stat">{value}</span>
            <span className="muted tiny">{sub}</span></div>
        ))}
      </div>
      <div className="class-columns">
        <section className="card" style={{ flex: "1 1 380px" }} aria-labelledby="todo-h">
          <h3 id="todo-h">Kræver handling</h3>
          {todo.length === 0 && <p className="muted small">Intet lige nu.</p>}
          {todo.map((t) => (
            <div key={t.title} className={`alert ${t.tone}`}>
              <strong>{t.title}</strong>
              <div>{t.body}{t.action && <> <button type="button" className="link-button" onClick={t.action[1]}>{t.action[0]}</button></>}</div>
            </div>
          ))}
        </section>
        <section className="card" style={{ flex: "1 1 380px" }} aria-labelledby="recent-h">
          <h3 id="recent-h">Seneste ændringer</h3>
          {recent.length === 0 && <p className="muted small">Ingen ændringer endnu.</p>}
          <ul className="plain-list">
            {recent.map((r, i) => (
              <li key={i} className="spread small">
                <span><strong>{r.actor}</strong> {describe(r)}</span>
                <span className="muted">{fmtDate(r.ts)}</span>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  );
}

type SettingsData = {
  student_email_domain: string;
  teachers_create_classes: boolean;
  orgs: { id: number; login: string; created_at: string; disconnected_at: string | null; connected_by: string | null; classes: number }[];
};

function Organisation() {
  const { me } = useClassMe();
  const data = useLoad(() => api.get<SettingsData>("/api/school/admin/settings"));
  const act = useAction();
  const [domain, setDomain] = useState<string | null>(null);
  const [teachersCreate, setTeachersCreate] = useState<boolean | null>(null);
  const [saved, setSaved] = useState(false);
  if (!data.data) return data.loading ? <Loading /> : <Alert>{data.error}</Alert>;
  const d = data.data;
  const domainValue = domain ?? d.student_email_domain;
  const createValue = teachersCreate ?? d.teachers_create_classes;

  const save = (e: React.FormEvent) => {
    e.preventDefault();
    setSaved(false);
    act.run(async () => {
      await api.put("/api/school/admin/settings", { student_email_domain: domainValue.trim(), teachers_create_classes: createValue });
      await data.reload();
      setSaved(true);
    });
  };
  const disconnect = (id: number, login: string) => {
    if (!confirm(`Afbryd forbindelsen til ${login}? Der kan ikke oprettes flere repos der, før den forbindes igen. Appen fjernes ikke fra GitHub.`)) return;
    act.run(async () => { await api.post(`/api/school/admin/orgs/${id}/disconnect`); await data.reload(); });
  };

  return (
    <div className="class-columns">
      <section className="card" style={{ flex: "1 1 380px" }} aria-labelledby="org-h">
        <h3 id="org-h">GitHub-organisation</h3>
        {d.orgs.length === 0 && <p className="muted small">Ingen organisation er forbundet endnu.</p>}
        {d.orgs.map((o) => (
          <div key={o.id} className="stack" style={{ "--gap": "6px", paddingBottom: 8 } as React.CSSProperties}>
            <div className="row">
              <span className="mono" style={{ fontWeight: 600 }}>github.com/{o.login}</span>
              {o.disconnected_at ? <span className="pill red">Afbrudt</span> : <span className="pill green">Forbundet</span>}
            </div>
            <span className="muted small">
              Forbundet {fmtDate(o.created_at)}{o.connected_by ? ` af ${o.connected_by}` : ""} · {o.classes} {o.classes === 1 ? "klasse" : "klasser"}
            </span>
            <div className="row">
              <a className="btn btn-outline btn-sm" href={`https://github.com/${o.login}`} target="_blank" rel="noreferrer">Åbn på GitHub</a>
              {!o.disconnected_at && <button className="btn btn-danger btn-sm" disabled={act.busy} onClick={() => disconnect(o.id, o.login)}>Afbryd forbindelsen</button>}
            </div>
          </div>
        ))}
        <p className="muted small">
          Sæt organisationens basisrettighed til <strong>No permission</strong> på GitHub (Settings → Member privileges),
          så eleverne kun ser deres egne repos.
        </p>
        <div className="row">
          <button className="btn btn-primary" disabled={!me.github.configured || act.busy}
            onClick={() => act.run(() => goToGitHub("install"))}>Forbind organisation</button>
        </div>
      </section>
      <form className="card" style={{ flex: "1 1 380px" }} onSubmit={save} aria-labelledby="regler-h">
        <h3 id="regler-h">Regler for skolen</h3>
        <label className="field">Elever skal bruge en e-mail fra
          <input value={domainValue} onChange={(e) => { setDomain(e.target.value); setSaved(false); }} placeholder="elev.skole.dk (tom: alle)" />
        </label>
        <label className="row small" style={{ "--gap": "8px", fontWeight: 500 } as React.CSSProperties}>
          <input type="checkbox" checked={createValue} onChange={(e) => { setTeachersCreate(e.target.checked); setSaved(false); }} />
          Lærere må selv oprette klasser
        </label>
        {act.error && <Alert>{act.error}</Alert>}
        {saved && <Alert kind="success">Reglerne er gemt.</Alert>}
        <div className="row"><button className="btn btn-primary" disabled={act.busy}>Gem</button></div>
      </form>
    </div>
  );
}

export default function Admin() {
  const { me } = useClassMe();
  const [params, setParams] = useSearchParams();
  const tab = (TABS.find(([id]) => id === params.get("fane"))?.[0] ?? "oversigt") as Tab;
  const go = (t: Tab, extra: Record<string, string> = {}) => setParams({ fane: t, ...extra });

  if (!me.is_school_admin) {
    return (
      <Alert kind="info">
        Skoleadministrationen kræver rollen skoleadministrator. Gå til <Link to="/school/teacher">Lærer</Link> eller{" "}
        <Link to="/school/student">Elev</Link>.
      </Alert>
    );
  }
  return (
    <div className="stack" style={{ "--gap": "20px" } as React.CSSProperties}>
      <div role="tablist" aria-label="Skoleadministration" className="tabs">
        {TABS.map(([id, label]) => (
          <button key={id} type="button" role="tab" aria-selected={tab === id} className={tab === id ? "active" : ""}
            onClick={() => go(id)}>{label}</button>
        ))}
      </div>
      {tab === "oversigt" && <Overview go={go} />}
      {tab === "laerere" && <Teachers />}
      {tab === "elever" && <Students />}
      {tab === "klasser" && <Classes />}
      {tab === "forbrug" && <Usage />}
      {tab === "tilkob" && <Addons />}
      {tab === "organisation" && <Organisation />}
    </div>
  );
}
