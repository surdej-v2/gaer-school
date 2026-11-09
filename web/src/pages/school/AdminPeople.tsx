// /school/admin: the Lærere, Elever and Klasser tabs.
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, query } from "../../api";
import { Alert, fmtDate, fmtNumber, Loading, useLoad } from "../../ui";
import { StatusPill, useAction, type RepoStatus } from "./shared";

type Teacher = {
  id: number; name: string; email: string | null; github_login: string | null; classes: string | null; students: number;
  student_ai_tokens: number; school_admin: boolean; gaer_admin: boolean; teacher: boolean;
};
type Invite = { email: string; role: "teacher" | "school_admin"; created_at: string; invited_by: string | null };

export function tokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toLocaleString("da-DK", { maximumFractionDigits: 2 })} mio. tokens`;
  return `${fmtNumber(n)} tokens`;
}

export function Teachers() {
  const data = useLoad(() => api.get<{ teachers: Teacher[]; invites: Invite[] }>("/api/school/admin/teachers"));
  const act = useAction();
  const [email, setEmail] = useState("");
  const [asAdmin, setAsAdmin] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const add = (e: React.FormEvent) => {
    e.preventDefault();
    setNote(null);
    act.run(async () => {
      const r = await api.post<{ invited: boolean }>("/api/school/admin/teachers", { email: email.trim(), school_admin: asAdmin });
      setNote(r.invited ? `${email.trim()} har ikke logget ind endnu og får rollen ved første login.` : `${email.trim()} er nu lærer.`);
      setEmail("");
      setAsAdmin(false);
      await data.reload();
    });
  };
  const call = (fn: () => Promise<unknown>, confirmText?: string) => {
    if (confirmText && !confirm(confirmText)) return;
    act.run(async () => { await fn(); await data.reload(); });
  };

  return (
    <div className="stack" style={{ "--gap": "16px" } as React.CSSProperties}>
      <form className="card" onSubmit={add} aria-labelledby="add-teacher-h">
        <h3 id="add-teacher-h">Tilføj lærer</h3>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <label className="field" style={{ flex: "1 1 300px" }}>E-mail
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="navn@skole.dk" required />
          </label>
          <label className="row small" style={{ "--gap": "8px", fontWeight: 500, paddingBottom: 10 } as React.CSSProperties}>
            <input type="checkbox" checked={asAdmin} onChange={(e) => setAsAdmin(e.target.checked)} /> Også skoleadministrator
          </label>
          <button className="btn btn-primary" disabled={act.busy || !email.includes("@")}>Gør til lærer</button>
        </div>
        <p className="muted small">Har personen ikke logget ind endnu, bliver vedkommende lærer ved første besøg på /school.</p>
        {note && <Alert kind="success">{note}</Alert>}
        {act.error && <Alert>{act.error}</Alert>}
      </form>

      {!data.data ? (data.loading ? <Loading /> : <Alert>{data.error}</Alert>) : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Lærer</th><th>GitHub</th><th>Klasser</th><th>Forbrug denne md.</th><th>Rolle</th><th><span className="sr-only">Handlinger</span></th></tr></thead>
            <tbody>
              {data.data.teachers.map((t) => (
                <tr key={t.id}>
                  <td className="wrap"><strong>{t.name}</strong><div className="muted tiny">{t.email ?? ""}</div></td>
                  <td className="mono small">{t.github_login ?? <span className="pill amber">Mangler</span>}</td>
                  <td className="wrap">{t.classes ?? "—"}</td>
                  <td className="small">{t.students ? `${t.students} elever · ${tokens(t.student_ai_tokens)}` : "—"}</td>
                  <td>{t.school_admin ? <span className="pill dark">{t.gaer_admin ? "Admin" : "Skoleadmin"}</span> : <span className="pill blue">Lærer</span>}</td>
                  <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                    {!t.gaer_admin && (t.school_admin
                      ? <button className="btn btn-outline btn-sm" disabled={act.busy}
                          onClick={() => call(() => api.del(`/api/school/admin/teachers/${t.id}/school-admin`))}>Fjern skoleadmin</button>
                      : <button className="btn btn-outline btn-sm" disabled={act.busy}
                          onClick={() => call(() => api.put(`/api/school/admin/teachers/${t.id}/school-admin`, {}))}>Gør til skoleadmin</button>)}{" "}
                    {!t.gaer_admin && (
                      <button className="btn btn-danger btn-sm" disabled={act.busy}
                        onClick={() => call(() => api.del(`/api/school/admin/teachers/${t.id}`),
                          `Fjern ${t.name} som lærer? Klasserne beholder deres lærere, til du ændrer dem under Klasser.`)}>Fjern</button>
                    )}
                  </td>
                </tr>
              ))}
              {data.data.invites.map((i) => (
                <tr key={i.email}>
                  <td className="wrap"><strong>{i.email}</strong><div className="muted tiny">Inviteret {fmtDate(i.created_at)}{i.invited_by ? ` af ${i.invited_by}` : ""}</div></td>
                  <td>—</td><td>—</td><td>—</td>
                  <td><span className="pill amber">{i.role === "school_admin" ? "Inviteret · skoleadmin" : "Inviteret"}</span></td>
                  <td style={{ textAlign: "right" }}>
                    <button className="btn btn-danger btn-sm" disabled={act.busy}
                      onClick={() => call(() => api.del(`/api/school/admin/invites/${encodeURIComponent(i.email)}`))}>Annullér</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

type Student = {
  id: number; name: string; email: string | null; github_login: string | null;
  classes: { id: number; name: string; status: RepoStatus | null }[];
};
type ClassRow = {
  id: number; name: string; slug: string; template_repo: string; org: string; archived_at: string | null;
  teachers: { id: number; name: string | null }[]; students: number; ready: number; failed: number;
};

export function Students() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const classId = params.get("klasse") ?? "";
  const status = params.get("status") ?? "";
  const set = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v); else next.delete(k);
    setParams(next, { replace: true });
  };
  const data = useLoad(() => api.get<{ items: Student[]; total: number }>(
    `/api/school/admin/students${query({ q, class_id: classId, status })}`), [q, classId, status]);
  const classes = useLoad(() => api.get<ClassRow[]>("/api/school/admin/classes"));
  const act = useAction();
  const [done, setDone] = useState<string | null>(null);
  const [moving, setMoving] = useState<{ student: Student; from: number } | null>(null);
  const [target, setTarget] = useState("");
  const active = (classes.data ?? []).filter((c) => !c.archived_at);

  const remind = () => act.run(async () => {
    const r = await api.post<{ queued: number }>("/api/school/admin/students/remind");
    setDone(r.queued ? `Påmindelsen sendes til ${r.queued} ${r.queued === 1 ? "elev" : "elever"}.` : "Alle elever har forbundet GitHub.");
  });
  const remove = (s: Student) => {
    if (!confirm(`Fjern ${s.name} fra alle klasser? Pladsen bliver ledig. Kontoen og repoet på GitHub bliver ikke slettet.`)) return;
    act.run(async () => { await api.del(`/api/school/admin/students/${s.id}`); await data.reload(); });
  };
  const move = (e: React.FormEvent) => {
    e.preventDefault();
    if (!moving || !target) return;
    act.run(async () => {
      await api.post(`/api/school/admin/students/${moving.student.id}/move`, { from_class_id: moving.from, to_class_id: Number(target) });
      setMoving(null);
      setTarget("");
      await data.reload();
    });
  };

  return (
    <div className="stack" style={{ "--gap": "16px" } as React.CSSProperties}>
      <div className="filters">
        <label className="field">Søg
          <input type="search" defaultValue={q} placeholder="Navn, e-mail eller GitHub-bruger"
            onKeyDown={(e) => { if (e.key === "Enter") set("q", (e.target as HTMLInputElement).value.trim()); }}
            onBlur={(e) => set("q", e.target.value.trim())} />
        </label>
        <label className="field">Klasse
          <select value={classId} onChange={(e) => set("klasse", e.target.value)}>
            <option value="">Alle klasser</option>
            {active.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </label>
        <label className="field">Status
          <select value={status} onChange={(e) => set("status", e.target.value)}>
            <option value="">Alle</option>
            <option value="no_github">Mangler GitHub-konto</option>
            <option value="invited">Afventer invitation</option>
            <option value="failed">Fejl</option>
          </select>
        </label>
        <button className="btn btn-outline" disabled={act.busy} onClick={remind}>Påmind dem uden GitHub-konto</button>
      </div>
      {done && <Alert kind="success">{done}</Alert>}
      {act.error && <Alert>{act.error}</Alert>}

      {moving && (
        <form className="card" onSubmit={move} aria-labelledby="move-h">
          <h3 id="move-h">Flyt {moving.student.name}</h3>
          <label className="field">Til klasse
            <select value={target} onChange={(e) => setTarget(e.target.value)} required>
              <option value="">Vælg klasse</option>
              {active.filter((c) => c.id !== moving.from).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </label>
          <p className="muted small">Repoet i den gamle klasse bliver på GitHub. Eleven får et nyt repo fra den nye klasses skabelon.</p>
          <div className="row">
            <button className="btn btn-primary" disabled={act.busy || !target}>Flyt</button>
            <button type="button" className="btn btn-ghost" onClick={() => setMoving(null)}>Annullér</button>
          </div>
        </form>
      )}

      {!data.data ? (data.loading ? <Loading /> : <Alert>{data.error}</Alert>) : (
        <>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Elev</th><th>GitHub</th><th>Klasser og repos</th><th><span className="sr-only">Handlinger</span></th></tr></thead>
              <tbody>
                {data.data.items.length === 0 && <tr><td colSpan={4} className="muted">Ingen elever matcher.</td></tr>}
                {data.data.items.map((s) => (
                  <tr key={s.id}>
                    <td className="wrap"><strong>{s.name}</strong><div className="muted tiny">{s.email ?? ""}</div></td>
                    <td className="mono small">{s.github_login ?? "—"}</td>
                    <td className="wrap">
                      <div className="stack" style={{ "--gap": "6px" } as React.CSSProperties}>
                        {s.classes.map((c) => (
                          <div key={c.id} className="row" style={{ "--gap": "8px" } as React.CSSProperties}>
                            <span>{c.name}</span><StatusPill status={c.status} />
                            <button type="button" className="link-button small" onClick={() => { setMoving({ student: s, from: c.id }); setTarget(""); }}>Flyt</button>
                          </div>
                        ))}
                      </div>
                    </td>
                    <td style={{ textAlign: "right" }}>
                      <button className="btn btn-danger btn-sm" disabled={act.busy} onClick={() => remove(s)}>Fjern fra skolen</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="muted small">{fmtNumber(data.data.total)} {data.data.total === 1 ? "elev" : "elever"}</p>
        </>
      )}
    </div>
  );
}

export function Classes() {
  const data = useLoad(() => api.get<ClassRow[]>("/api/school/admin/classes"));
  const teachers = useLoad(() => api.get<{ teachers: Teacher[] }>("/api/school/admin/teachers"));
  const act = useAction();
  const [editing, setEditing] = useState<number | null>(null);
  const [pick, setPick] = useState("");

  const call = (fn: () => Promise<unknown>, confirmText?: string) => {
    if (confirmText && !confirm(confirmText)) return;
    act.run(async () => { await fn(); await data.reload(); });
  };

  if (!data.data) return data.loading ? <Loading /> : <Alert>{data.error}</Alert>;
  return (
    <div className="stack" style={{ "--gap": "16px" } as React.CSSProperties}>
      <p className="muted">Alle klasser på skolen. Lærerne opretter dem; du kan skifte lærere og arkivere.</p>
      {act.error && <Alert>{act.error}</Alert>}
      <div className="table-wrap">
        <table>
          <thead><tr><th>Klasse</th><th>Lærere</th><th>Elever</th><th>Repos klar</th><th><span className="sr-only">Handlinger</span></th></tr></thead>
          <tbody>
            {data.data.length === 0 && <tr><td colSpan={5} className="muted">Ingen klasser endnu.</td></tr>}
            {data.data.map((c) => (
              <tr key={c.id}>
                <td className="wrap"><strong>{c.name}</strong><div className="muted tiny mono">{c.template_repo}</div>
                  {c.archived_at && <span className="pill">Arkiveret {fmtDate(c.archived_at)}</span>}</td>
                <td className="wrap">
                  <div className="stack" style={{ "--gap": "4px" } as React.CSSProperties}>
                    {c.teachers.map((t) => (
                      <div key={t.id} className="row" style={{ "--gap": "6px" } as React.CSSProperties}>
                        <span>{t.name ?? `Lærer #${t.id}`}</span>
                        {!c.archived_at && c.teachers.length > 1 && (
                          <button type="button" className="link-button small" aria-label={`Fjern ${t.name ?? "lærer"} fra ${c.name}`}
                            onClick={() => call(() => api.del(`/api/school/admin/classes/${c.id}/teachers/${t.id}`))}>Fjern</button>
                        )}
                      </div>
                    ))}
                    {editing === c.id && (
                      <form className="row" style={{ "--gap": "6px" } as React.CSSProperties} onSubmit={(e) => {
                        e.preventDefault();
                        if (pick) call(async () => { await api.post(`/api/school/admin/classes/${c.id}/teachers`, { user_id: Number(pick) }); setEditing(null); });
                      }}>
                        <label className="sr-only" htmlFor={`teacher-${c.id}`}>Lærer</label>
                        <select id={`teacher-${c.id}`} value={pick} onChange={(e) => setPick(e.target.value)}>
                          <option value="">Vælg lærer</option>
                          {(teachers.data?.teachers ?? []).filter((t) => !c.teachers.some((x) => x.id === t.id))
                            .map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
                        </select>
                        <button className="btn btn-primary btn-sm" disabled={!pick || act.busy}>Tilføj</button>
                      </form>
                    )}
                  </div>
                </td>
                <td className="num">{c.students}</td>
                <td>
                  <div className="row" style={{ "--gap": "8px", flexWrap: "nowrap" } as React.CSSProperties}>
                    <div className="bar" style={{ width: 80 }} role="img" aria-label={`${c.ready} af ${c.students} klar`}>
                      <div style={{ width: `${c.students ? (100 * c.ready) / c.students : 0}%` }} />
                    </div>
                    <span className="num small">{c.ready}/{c.students}</span>
                    {c.failed > 0 && <span className="pill red">{c.failed} fejl</span>}
                  </div>
                </td>
                <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                  {!c.archived_at && (
                    <>
                      <button className="btn btn-outline btn-sm" onClick={() => { setEditing(editing === c.id ? null : c.id); setPick(""); }}>Tilføj lærer</button>{" "}
                      <button className="btn btn-outline btn-sm" disabled={act.busy}
                        onClick={() => call(() => api.post(`/api/school/admin/classes/${c.id}/archive`),
                          `Arkivér ${c.name}? Eleverne frigiver deres pladser, og der oprettes ikke flere repos. Repoerne på GitHub bliver.`)}>Arkivér</button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
