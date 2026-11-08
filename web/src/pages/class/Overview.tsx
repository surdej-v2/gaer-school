// /class: the dashboard. Teachers see their organisation, classes and what needs attention;
// students see their classes; everyone else gets the join form.
import { Link } from "react-router-dom";
import { fmtNumber } from "../../ui";
import { goToGitHub, StatusPill, useAction, useClassMe } from "./shared";
import { JoinCard } from "./Student";

export default function Overview() {
  const { me } = useClassMe();
  const connect = useAction();
  const active = me.teaching.filter((c) => !c.archived_at);
  const sum = (k: "students" | "ready" | "waiting" | "failed") => active.reduce((n, c) => n + c[k], 0);
  const todo = active.flatMap((c) => [
    c.no_account ? { key: `${c.id}-a`, tone: "warn", text: `${c.no_account} ${c.no_account === 1 ? "elev mangler" : "elever mangler"} at forbinde GitHub`, cls: c } : null,
    c.invited ? { key: `${c.id}-i`, tone: "warn", text: `${c.invited} ${c.invited === 1 ? "invitation er" : "invitationer er"} ikke accepteret`, cls: c } : null,
    c.failed ? { key: `${c.id}-f`, tone: "error", text: `${c.failed} ${c.failed === 1 ? "repo kunne" : "repos kunne"} ikke oprettes`, cls: c } : null,
  ]).filter((x) => x !== null);

  return (
    <div className="stack" style={{ "--gap": "24px" } as React.CSSProperties}>
      {me.is_teacher && (
        <>
          {me.orgs.length === 0 ? (
            <div className="card">
              <h3>Forbind en GitHub-organisation</h3>
              <p className="muted small">
                Installer Gær GitHub App på skolens organisation. Så kan Gær oprette hold og repos og invitere elever.
                Sæt organisationens basisrettighed til <strong>No permission</strong>, så eleverne kun ser deres egne repos.
              </p>
              <div className="row">
                <button className="btn btn-primary" disabled={!me.github.configured || connect.busy}
                  onClick={() => connect.run(() => goToGitHub("install"))}>Forbind organisation</button>
              </div>
              {connect.error && <p className="small" style={{ color: "#991b1b" }}>{connect.error}</p>}
            </div>
          ) : (
            <div className="stack">
              {me.orgs.map((o) => (
                <div key={o.id} className="card" style={{ flexDirection: "row", alignItems: "center", flexWrap: "wrap", gap: 16 }}>
                  <div className="stack" style={{ "--gap": "4px", flex: "1 1 280px" } as React.CSSProperties}>
                    <div className="row">
                      <h3 className="mono">github.com/{o.login}</h3>
                      {o.disconnected_at ? <span className="pill red">Afinstalleret</span> : <span className="pill green">Forbundet</span>}
                    </div>
                    <p className="muted small">
                      {o.disconnected_at ? "Appen er fjernet fra organisationen. Forbind den igen for at oprette repos."
                        : "Gær GitHub App må oprette hold og repos og invitere medlemmer."}
                    </p>
                  </div>
                  <div className="row">
                    <a className="btn btn-outline" href={`https://github.com/${o.login}`} target="_blank" rel="noreferrer">Åbn på GitHub</a>
                    <Link className="btn btn-primary" to="/class/teacher?ny=1" style={{ color: "#fff", textDecoration: "none" }}>Ny klasse</Link>
                  </div>
                </div>
              ))}
            </div>
          )}

          <div className="grid" style={{ "--min": "180px" } as React.CSSProperties}>
            {([["Klasser", fmtNumber(active.length)], ["Elever", fmtNumber(sum("students"))],
              ["Repos klar", fmtNumber(sum("ready"))], ["Afventer eller fejl", fmtNumber(sum("waiting") + sum("failed"))]] as const)
              .map(([label, value]) => (
                <div key={label} className="card"><span className="muted small">{label}</span><span className="stat">{value}</span></div>
              ))}
          </div>

          <div className="class-columns">
            <section className="stack" style={{ flex: "999 1 560px", minWidth: 0 }}>
              <h3>Mine klasser</h3>
              <div className="table-wrap">
                <table>
                  <thead><tr><th>Klasse</th><th>Skabelon</th><th>Elever</th><th>Repos klar</th><th>Status</th></tr></thead>
                  <tbody>
                    {me.teaching.length === 0 && <tr><td colSpan={5} className="muted">Ingen klasser endnu.</td></tr>}
                    {me.teaching.map((c) => (
                      <tr key={c.id}>
                        <td><Link to={`/class/teacher?klasse=${c.id}`}><strong>{c.name}</strong></Link>
                          <div className="muted tiny mono">{c.org}/{c.slug}</div></td>
                        <td className="mono tiny">{c.template_repo}</td>
                        <td className="num">{c.students}</td>
                        <td>
                          <div className="row" style={{ "--gap": "8px", flexWrap: "nowrap" } as React.CSSProperties}>
                            <div className="bar" style={{ width: 80 }} role="img" aria-label={`${c.ready} af ${c.students} klar`}>
                              <div style={{ width: `${c.students ? (100 * c.ready) / c.students : 0}%` }} />
                            </div>
                            <span className="num small">{c.ready}/{c.students}</span>
                          </div>
                        </td>
                        <td>{c.archived_at ? <span className="pill">Arkiveret</span>
                          : c.failed ? <span className="pill red">{c.failed} fejl</span>
                          : c.waiting ? <span className="pill amber">{c.waiting} afventer</span>
                          : <span className="pill green">Alle klar</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
            <aside className="card" style={{ flex: "1 1 280px" }}>
              <h3>Kræver handling</h3>
              {todo.length === 0 ? <p className="muted small">Intet lige nu.</p> : todo.map((t) => (
                <div key={t.key} className={`alert ${t.tone}`}>
                  {t.text} i <Link to={`/class/teacher?klasse=${t.cls.id}`}>{t.cls.name}</Link>.
                </div>
              ))}
            </aside>
          </div>
        </>
      )}

      {me.learning.length > 0 && (
        <section className="stack">
          <h3>Klasser, jeg går i</h3>
          <div className="grid">
            {me.learning.map((c) => (
              <Link key={c.id} to="/class/student" className="card">
                <div className="spread"><h3>{c.name}</h3><StatusPill status={c.status} /></div>
                <p className="muted small">{c.teachers ? `Lærer: ${c.teachers}` : c.org}</p>
              </Link>
            ))}
          </div>
        </section>
      )}

      {!me.is_teacher && me.learning.length === 0 && (
        <div className="grid">
          <JoinCard />
          <div className="card">
            <h3>Sådan virker det</h3>
            <p className="muted small">
              Din lærer giver dig en kode. Når du har tilmeldt dig og forbundet din GitHub-konto, får du dit eget repo
              i skolens organisation, hvor du er admin.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
