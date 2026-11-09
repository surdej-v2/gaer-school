// /class/student: connect GitHub, accept the organisation's invitation, use your own repo.
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../../api";
import { useAuth } from "../../auth";
import { CopyField, goToGitHub, StatusPill, useAction, useClassMe, type LearningClass } from "./shared";

export function JoinCard({ onJoined }: { onJoined?: () => Promise<void> | void }) {
  const [params] = useSearchParams();
  const [code, setCode] = useState(params.get("kode") ?? "");
  const [done, setDone] = useState<string | null>(null);
  const join = useAction();
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    join.run(async () => {
      const r = await api.post<{ name: string; joined: boolean }>("/api/class/join", { code });
      setDone(r.joined ? `Du er tilmeldt ${r.name}.` : `Du er allerede i ${r.name}.`);
      setCode("");
      await onJoined?.();
    });
  };
  return (
    <form className="card" onSubmit={submit}>
      <h3>Tilmeld en klasse</h3>
      <label className="field" htmlFor="join-code">Kode fra din lærer</label>
      <div className="row" style={{ "--gap": "8px", flexWrap: "nowrap" } as React.CSSProperties}>
        <input id="join-code" value={code} onChange={(e) => setCode(e.target.value)} placeholder="ABCD-2345"
          autoComplete="off" className="mono" style={{ flex: 1, textTransform: "uppercase" }} />
        <button className="btn btn-primary" disabled={join.busy || code.trim().length < 4}>Tilmeld</button>
      </div>
      {join.error && <p className="small" style={{ color: "#991b1b" }}>{join.error}</p>}
      {done && <p className="small" style={{ color: "#166534" }}>{done}</p>}
    </form>
  );
}

function Step({ n, title, state, children }: { n: number; title: string; state: "done" | "now" | "later"; children: React.ReactNode }) {
  return (
    <li className={`class-step ${state}`}>
      <div className="row" style={{ "--gap": "10px" } as React.CSSProperties}>
        <span className="step-num" aria-hidden="true">{state === "done" ? "✓" : n}</span>
        <strong>{title}</strong>
        {state === "done" && <span className="sr-only">(færdig)</span>}
      </div>
      {children}
    </li>
  );
}

function ClassCard({ c }: { c: LearningClass }) {
  return (
    <article className="card">
      <div className="spread">
        <div className="stack" style={{ "--gap": "2px" } as React.CSSProperties}>
          <h3>{c.name}</h3>
          <span className="muted small">{c.teachers ? `Lærer: ${c.teachers}` : c.org}</span>
        </div>
        <StatusPill status={c.status} />
      </div>
      {c.clone_url && c.status !== "transferred" && (
        <>
          <CopyField id={`clone-${c.id}`} label="Klon dit repo" value={`git clone ${c.clone_url}`} />
          <div className="row"><a className="btn btn-dark btn-sm" href={c.repo_url!} target="_blank" rel="noreferrer" style={{ color: "#fff" }}>Åbn på GitHub</a></div>
        </>
      )}
      {c.status === "transferred" && (
        <p className="small">Repoet er overdraget til din egen GitHub-konto. Accepter overdragelsen i mailen fra GitHub.</p>
      )}
      {c.status === "waiting_account" && <p className="muted small">Forbind din GitHub-konto ovenfor, så oprettes dit repo.</p>}
      {c.status === "invited" && (
        <p className="small">
          Accepter invitationen til <span className="mono">{c.org}</span>:{" "}
          <a href={c.invite_url} target="_blank" rel="noreferrer">åbn invitationen på GitHub</a>. Dit repo{" "}
          <span className="mono">{c.repo_name}</span> oprettes bagefter.
        </p>
      )}
      {(c.status === "pending" || c.status === "created") && <p className="muted small">Dit repo er ved at blive oprettet.</p>}
      {c.status === "failed" && <p className="small" style={{ color: "#991b1b" }}>Dit repo kunne ikke oprettes endnu. Din lærer kan se fejlen og prøve igen.</p>}
    </article>
  );
}

export default function Student() {
  const { me, reload } = useClassMe();
  const { user } = useAuth();
  const github = useAction();
  const account = me.github.account;
  const invited = me.learning.find((c) => c.status === "invited");
  const anyReady = me.learning.some((c) => c.status === "ready" || c.status === "transferred");

  const unlink = () => {
    if (!confirm("Fjern forbindelsen til din GitHub-konto? Dine repos på GitHub bliver ikke rørt.")) return;
    github.run(async () => {
      await api.del("/api/class/github/account");
      await reload();
    });
  };

  return (
    <div className="stack" style={{ "--gap": "24px" } as React.CSSProperties}>
      <h3>Hej{user?.display_name ? ` ${user.display_name}` : ""}</h3>
      <section className="card" aria-labelledby="kom-i-gang">
        <h3 id="kom-i-gang">Kom i gang</h3>
        <ol className="class-steps">
          <Step n={1} title="Forbind GitHub-konto" state={account ? "done" : "now"}>
            {account ? (
              <div className="row small">
                <span>Forbundet som <span className="mono">{account.login}</span></span>
                <button className="btn btn-ghost btn-sm" onClick={unlink} disabled={github.busy}>Fjern</button>
              </div>
            ) : (
              <>
                <p className="small muted">Vi gemmer kun dit brugernavn. Du logger stadig ind i Gær, som du plejer.</p>
                <button className="btn btn-primary btn-sm" disabled={!me.github.configured || github.busy}
                  onClick={() => github.run(() => goToGitHub("link"))}>Forbind GitHub</button>
              </>
            )}
          </Step>
          <Step n={2} title="Accepter invitationen" state={invited ? "now" : anyReady ? "done" : "later"}>
            {invited ? (
              <a className="btn btn-primary btn-sm" href={invited.invite_url} target="_blank" rel="noreferrer" style={{ color: "#fff", alignSelf: "flex-start" }}>
                Åbn invitation på GitHub
              </a>
            ) : <p className="small muted">Når du er i en klasse, inviteres du til skolens organisation på GitHub.</p>}
            {invited && (
              <button className="btn btn-ghost btn-sm" style={{ alignSelf: "flex-start" }} onClick={() => reload()}>Jeg har accepteret</button>
            )}
          </Step>
          <Step n={3} title="Dit repo er klar" state={anyReady ? "done" : "later"}>
            <p className="small muted">Du bliver admin på dit eget repo. Din lærer kan også se og hjælpe i det.</p>
          </Step>
        </ol>
        {github.error && <p className="small" style={{ color: "#991b1b" }}>{github.error}</p>}
      </section>

      <div className="class-columns">
        <section className="stack" style={{ flex: "999 1 560px", minWidth: 0 }}>
          <h3>Mine klasser</h3>
          {me.learning.length === 0 && <p className="muted small">Du er ikke i nogen klasse endnu. Brug koden fra din lærer.</p>}
          {me.learning.map((c) => <ClassCard key={c.id} c={c} />)}
        </section>
        <aside className="stack" style={{ flex: "1 1 280px" }}>
          <JoinCard onJoined={reload} />
          <div className="card">
            <h3>Dine data</h3>
            <p className="small muted">Vi gemmer dit GitHub-brugernavn og hvilke klasser du er i. Du kan hente eller slette dine data under Privatliv.</p>
            <Link to="/privatliv" className="small" style={{ fontWeight: 600 }}>Gå til Privatliv</Link>
          </div>
        </aside>
      </div>
    </div>
  );
}
