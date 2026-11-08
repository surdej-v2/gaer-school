// /class/teacher: create classes, share the join code, create the students' repos and follow their status.
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../../api";
import { Alert, fmtDate, Loading, useLoad } from "../../ui";
import { CopyField, goToGitHub, StatusPill, useAction, useClassMe, type RepoStatus } from "./shared";

type Student = {
  user_id: number; name: string; github_login: string | null; repo_name: string | null; status: RepoStatus | null;
  last_error: string | null; attempts: number; updated_at: string | null; joined_at: string; repo_url: string | null;
};

type ClassDetail = {
  id: number; name: string; slug: string; org: string; join_code: string; template_repo: string; template_url: string;
  student_team: string | null; teacher_team: string | null; team_url: string; archived_at: string | null;
  disconnected: boolean; students: Student[]; teachers: { id: number; name: string; github_login: string | null }[];
};

const BUSY: (RepoStatus | null)[] = ["pending", "created"];

function NewClass({ onCreated, onCancel }: { onCreated: (id: number) => void; onCancel: () => void }) {
  const { me, reload } = useClassMe();
  const orgs = me.orgs.filter((o) => !o.disconnected_at);
  const [orgId, setOrgId] = useState(orgs[0]?.id ?? 0);
  const [name, setName] = useState("");
  const [template, setTemplate] = useState(orgs[0] ? `${orgs[0].login}/` : "");
  const create = useAction();
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    create.run(async () => {
      const r = await api.post<{ id: number }>("/api/class/classes", { org_id: orgId, name: name.trim(), template_repo: template.trim() });
      await reload();
      onCreated(r.id);
    });
  };
  if (orgs.length === 0) {
    return (
      <div className="card">
        <h3>Forbind en organisation først</h3>
        <p className="muted small">Klasser hører til en GitHub-organisation, hvor Gær GitHub App er installeret.</p>
        <div className="row">
          <button className="btn btn-primary" disabled={!me.github.configured || create.busy}
            onClick={() => create.run(() => goToGitHub("install"))}>Forbind organisation</button>
        </div>
        {create.error && <Alert>{create.error}</Alert>}
      </div>
    );
  }
  return (
    <form className="card" onSubmit={submit}>
      <h3>Ny klasse</h3>
      <div className="grid" style={{ "--min": "220px" } as React.CSSProperties}>
        <label className="field">Organisation
          <select value={orgId} onChange={(e) => setOrgId(Number(e.target.value))}>
            {orgs.map((o) => <option key={o.id} value={o.id}>{o.login}</option>)}
          </select>
        </label>
        <label className="field">Navn
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="fx 9.B Webudvikling" maxLength={80} required />
        </label>
        <label className="field">Skabelon-repo
          <input value={template} onChange={(e) => setTemplate(e.target.value)} placeholder="organisation/skabelon" className="mono" required />
        </label>
      </div>
      <p className="muted small">
        Skabelonen skal være markeret som <em>Template repository</em> på GitHub. Hver elev får en privat kopi,
        hvor eleven er admin, og klassens lærerhold kan vedligeholde den.
      </p>
      {create.error && <Alert>{create.error}</Alert>}
      <div className="row">
        <button className="btn btn-primary" disabled={create.busy || !name.trim() || !template.includes("/")}>
          {create.busy ? "Opretter hold på GitHub …" : "Opret klasse"}
        </button>
        <button type="button" className="btn btn-ghost" onClick={onCancel}>Annullér</button>
      </div>
    </form>
  );
}

function ClassView({ id }: { id: number }) {
  const { reload: reloadMe } = useClassMe();
  const detail = useLoad(() => api.get<ClassDetail>(`/api/class/classes/${id}`), [id]);
  const act = useAction();
  const [polls, setPolls] = useState(0);
  const c = detail.data;
  const busy = !!c?.students.some((s) => BUSY.includes(s.status));

  // Repos are created in the background: follow along for a while.
  useEffect(() => {
    if (!busy || polls > 40) return;
    const t = window.setTimeout(() => { setPolls((n) => n + 1); detail.reload(); }, 2500);
    return () => window.clearTimeout(t);
  }, [busy, polls, c]);

  if (!c) return detail.loading ? <Loading /> : <Alert>{detail.error}</Alert>;
  const missing = c.students.filter((s) => s.status !== "ready" && s.status !== "transferred" && s.status !== "waiting_account").length;
  const after = async () => { setPolls(0); await detail.reload(); await reloadMe(); };

  const provision = () => act.run(async () => { await api.post(`/api/class/classes/${id}/provision`); await after(); });
  const transfer = (s: Student) => {
    if (!confirm(`Overdrag ${s.repo_name} til ${s.github_login}s egen GitHub-konto? Du mister adgangen, medmindre eleven tilføjer dig.`)) return;
    act.run(async () => { await api.post(`/api/class/classes/${id}/students/${s.user_id}/transfer`); await after(); });
  };
  const remove = (s: Student) => {
    if (!confirm(`Fjern ${s.name} fra klassen? Repoet på GitHub bliver ikke slettet.`)) return;
    act.run(async () => { await api.del(`/api/class/classes/${id}/students/${s.user_id}`); await after(); });
  };
  const archive = () => {
    if (!confirm(`Arkivér ${c.name}? Elever kan ikke længere tilmelde sig, og der oprettes ikke flere repos.`)) return;
    act.run(async () => { await api.post(`/api/class/classes/${id}/archive`); await after(); });
  };

  return (
    <div className="stack" style={{ "--gap": "20px", minWidth: 0 } as React.CSSProperties}>
      <section className="card" aria-labelledby="klasse-navn">
        <div className="spread" style={{ alignItems: "flex-start" }}>
          <div className="stack" style={{ "--gap": "4px" } as React.CSSProperties}>
            <h3 id="klasse-navn" style={{ fontSize: 22 }}>{c.name}</h3>
            <span className="muted small">
              Hold <a href={c.team_url} target="_blank" rel="noreferrer" className="mono">{c.org}/{c.student_team}</a> ·
              skabelon <a href={c.template_url} target="_blank" rel="noreferrer" className="mono">{c.template_repo}</a> ·{" "}
              {c.students.length} {c.students.length === 1 ? "elev" : "elever"}
            </span>
          </div>
          {!c.archived_at && (
            <div className="row">
              <button className="btn btn-primary" onClick={provision} disabled={act.busy || c.disconnected || missing === 0}>
                {missing ? `Opret manglende repos (${missing})` : "Alle repos er oprettet"}
              </button>
              <button className="btn btn-outline" onClick={archive} disabled={act.busy}>Arkivér</button>
            </div>
          )}
        </div>
        {c.archived_at && <Alert kind="info">Klassen er arkiveret {fmtDate(c.archived_at)}.</Alert>}
        {c.disconnected && <Alert kind="warn">Gær GitHub App er fjernet fra {c.org}. Forbind organisationen igen for at oprette repos.</Alert>}
        {!c.archived_at && (
          <div className="grid" style={{ "--min": "260px" } as React.CSSProperties}>
            <CopyField id="join-code" label="Tilmeldingskode til eleverne" value={c.join_code} />
            <CopyField id="join-link" label="Eller send linket" value={`${window.location.origin}/class/student?kode=${c.join_code}`} />
          </div>
        )}
        <p className="muted small">
          Hver elev får <span className="mono">{c.slug}-&lt;github-bruger&gt;</span> som privat kopi af skabelonen og er admin på det.
          Holdet <span className="mono">{c.teacher_team}</span> kan vedligeholde alle repos. Ved kursets slut kan du overdrage et repo
          til elevens egen konto.
        </p>
        {act.error && <Alert>{act.error}</Alert>}
      </section>

      <section className="stack" aria-labelledby="elever">
        <div className="spread">
          <h3 id="elever">Elever og repos</h3>
          <button className="btn btn-ghost btn-sm" onClick={() => detail.reload()}>Opdatér</button>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Elev</th><th>GitHub</th><th>Repo</th><th>Status</th><th><span className="sr-only">Handlinger</span></th></tr></thead>
            <tbody>
              {c.students.length === 0 && (
                <tr><td colSpan={5} className="muted">Ingen elever endnu. Del tilmeldingskoden ovenfor.</td></tr>
              )}
              {c.students.map((s) => (
                <tr key={s.user_id}>
                  <td><strong>{s.name}</strong><div className="muted tiny">Tilmeldt {fmtDate(s.joined_at)}</div></td>
                  <td className="mono small">{s.github_login ?? "—"}</td>
                  <td className="mono small">
                    {s.repo_url ? <a href={s.repo_url} target="_blank" rel="noreferrer">{s.repo_name}</a> : (s.repo_name ?? "—")}
                  </td>
                  <td className="wrap">
                    <StatusPill status={s.status} />
                    {s.status === "failed" && s.last_error && <div className="tiny" style={{ color: "#991b1b", marginTop: 4 }}>{s.last_error}</div>}
                  </td>
                  <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                    {s.status === "ready" && <button className="btn btn-outline btn-sm" onClick={() => transfer(s)} disabled={act.busy}>Overdrag</button>}{" "}
                    <button className="btn btn-danger btn-sm" onClick={() => remove(s)} disabled={act.busy}>Fjern</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="muted tiny">Lærere: {c.teachers.map((t) => t.github_login ? `${t.name} (${t.github_login})` : t.name).join(", ")}</p>
      </section>
    </div>
  );
}

export default function Teacher() {
  const { me } = useClassMe();
  const [params, setParams] = useSearchParams();
  const creating = params.get("ny") === "1";
  const selected = Number(params.get("klasse")) || me.teaching.find((c) => !c.archived_at)?.id || me.teaching[0]?.id;
  const select = (next: Record<string, string>) => setParams(next, { replace: false });

  if (!me.is_teacher) {
    return (
      <Alert kind="info">
        Lærer-siden kræver lærer-adgang. En administrator kan give dig den under /admin → Brugere.{" "}
        Er du elev, så gå til <Link to="/class/student">Elev</Link>.
      </Alert>
    );
  }

  return (
    <div className="class-columns">
      <nav className="card class-list" aria-label="Klasser" style={{ flex: "1 1 220px", maxWidth: 300 }}>
        <span className="eyebrow">Klasser</span>
        {me.teaching.map((c) => (
          <button key={c.id} type="button" className={!creating && c.id === selected ? "active" : ""}
            aria-current={!creating && c.id === selected ? "true" : undefined}
            onClick={() => select({ klasse: String(c.id) })}>
            {c.name}{c.archived_at && <span className="muted tiny"> · arkiveret</span>}
          </button>
        ))}
        <button type="button" className={`new ${creating ? "active" : ""}`} onClick={() => select({ ny: "1" })}>+ Ny klasse</button>
      </nav>
      <div style={{ flex: "999 1 560px", minWidth: 0 }}>
        {creating || !selected
          ? <NewClass onCreated={(id) => select({ klasse: String(id) })} onCancel={() => select({})} />
          : <ClassView key={selected} id={selected} />}
      </div>
    </div>
  );
}
