// /school: shared data and pieces for the teacher, student and school administrator pages.
// The layout loads /api/class/me once and hands it to the sub pages through the router outlet.
import { useEffect, useState } from "react";
import { Navigate, NavLink, Outlet, useOutletContext, useSearchParams } from "react-router-dom";
import { api, errorText } from "../../api";
import { Alert, Loading, useLoad } from "../../ui";

export type RepoStatus = "waiting_account" | "pending" | "invited" | "created" | "ready" | "transferred" | "failed";

export type Org = { id: number; login: string; disconnected_at: string | null; created_at: string };

export type TeachingClass = {
  id: number; name: string; slug: string; join_code: string; template_repo: string; org: string;
  archived_at: string | null; created_at: string;
  students: number; ready: number; waiting: number; failed: number; no_account: number; invited: number;
};

export type LearningClass = {
  id: number; name: string; org: string; teachers: string | null; joined_at: string;
  repo_name: string | null; status: RepoStatus | null; last_error: string | null;
  repo_url: string | null; clone_url: string | null; invite_url: string;
};

export type ClassMe = {
  github: { configured: boolean; account: { login: string; verified_at: string } | null };
  is_teacher: boolean;
  is_school_admin: boolean;
  can_create_classes: boolean;
  orgs: Org[];
  teaching: TeachingClass[];
  learning: LearningClass[];
};

export type ClassCtx = { me: ClassMe; reload: () => Promise<void> };

export const useClassMe = () => useOutletContext<ClassCtx>();

export const STATUS: Record<RepoStatus, [string, "green" | "amber" | "blue" | "red" | "dark"]> = {
  waiting_account: ["Mangler GitHub-konto", "amber"],
  pending: ["Klar til repo", "blue"],
  invited: ["Invitation sendt", "amber"],
  created: ["Repo oprettet", "blue"],
  ready: ["Admin på repo", "green"],
  transferred: ["Overdraget til eleven", "dark"],
  failed: ["Fejl", "red"],
};

export function StatusPill({ status }: { status: RepoStatus | null }) {
  if (!status) return <span className="pill">—</span>;
  const [label, tone] = STATUS[status];
  return <span className={`pill ${tone}`}>{label}</span>;
}

const ERRORS: Record<string, string> = {
  state: "Linket fra GitHub var udløbet eller ugyldigt. Prøv igen.",
  github: "GitHub afviste forespørgslen. Prøv igen om lidt.",
  "konto-i-brug": "Den GitHub-konto er allerede forbundet til en anden bruger.",
  "ingen-adgang": "Din GitHub-konto har ikke adgang til installationen. Installer appen med en konto, der er ejer af organisationen.",
  "ikke-org": "Appen skal installeres på en organisation, ikke på en personlig konto.",
  "ikke-laerer": "Det kræver lærer-adgang at forbinde en organisation. En administrator kan give dig den.",
  installation: "GitHub sendte ingen installation tilbage. Prøv igen.",
};

/** Messages from the GitHub round trip (?fejl=…, ?forbundet=…, ?github=forbundet). */
export function GitHubReturn() {
  const [params, setParams] = useSearchParams();
  const fejl = params.get("fejl");
  const org = params.get("forbundet");
  const linked = params.get("github") === "forbundet";
  if (!fejl && !org && !linked) return null;
  const clear = () => {
    const next = new URLSearchParams(params);
    ["fejl", "forbundet", "github"].forEach((k) => next.delete(k));
    setParams(next, { replace: true });
  };
  return (
    <div className="spread">
      {fejl ? <Alert>{ERRORS[fejl] ?? "Noget gik galt hos GitHub."}</Alert>
        : <Alert kind="success">{org ? `Organisationen ${org} er forbundet.` : "Din GitHub-konto er forbundet."}</Alert>}
      <button className="btn btn-ghost btn-sm" onClick={clear}>Luk</button>
    </div>
  );
}

export async function goToGitHub(purpose: "link" | "install") {
  const { url } = await api.post<{ url: string }>("/api/class/github/start", { purpose });
  window.location.href = url;
}

export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, setError, run };
}

export function CopyField({ id, label, value }: { id: string; label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(t);
  }, [copied]);
  return (
    <div className="stack" style={{ "--gap": "6px" } as React.CSSProperties}>
      <label htmlFor={id} className="small muted" style={{ fontWeight: 600 }}>{label}</label>
      <div className="row" style={{ "--gap": "8px", flexWrap: "nowrap" } as React.CSSProperties}>
        <input id={id} readOnly value={value} className="mono" style={{ flex: 1, fontSize: 13, background: "var(--hm-muted)" }}
          onFocus={(e) => e.currentTarget.select()} />
        <button className="btn btn-outline btn-sm" onClick={() => navigator.clipboard.writeText(value).then(() => setCopied(true))}>
          {copied ? "Kopieret" : "Kopiér"}
        </button>
      </div>
    </div>
  );
}

/** /school sends you to your role: school administrator, then teacher, then student. */
export function SchoolHome() {
  const { me } = useClassMe();
  const [params] = useSearchParams();
  const target = me.is_school_admin ? "/school/admin" : me.is_teacher ? "/school/teacher" : "/school/student";
  const query = params.toString();
  return <Navigate to={query ? `${target}?${query}` : target} replace />;
}

export default function SchoolLayout() {
  const me = useLoad(() => api.get<ClassMe>("/api/class/me"));
  const staff = !!me.data && (me.data.is_teacher || me.data.is_school_admin);
  return (
    <div className="page">
      <div className="spread">
        <div className="stack" style={{ "--gap": "6px" } as React.CSSProperties}>
          <div className="eyebrow">GitHub-klasser</div>
          <h2>Skole</h2>
        </div>
        {staff && (
          <nav className="subnav" aria-label="Skole-visning">
            {me.data?.is_teacher && <NavLink to="/school/teacher">Lærer</NavLink>}
            <NavLink to="/school/student">Elev</NavLink>
            {me.data?.is_school_admin && <NavLink to="/school/admin">Skoleadmin</NavLink>}
          </nav>
        )}
      </div>
      <GitHubReturn />
      {me.error && <Alert>{me.error}</Alert>}
      {me.data?.github.configured === false && (
        <Alert kind="info">
          GitHub er ikke sat op på denne installation endnu. En administrator skal oprette en GitHub App og sætte
          variablerne <code>GITHUB_APP_ID</code>, <code>GITHUB_APP_SLUG</code>, <code>GITHUB_APP_PRIVATE_KEY</code>,{" "}
          <code>GITHUB_CLIENT_ID</code>, <code>GITHUB_CLIENT_SECRET</code> og <code>GITHUB_WEBHOOK_SECRET</code>.
        </Alert>
      )}
      {!me.data ? (me.loading ? <Loading /> : null) : <Outlet context={{ me: me.data, reload: me.reload } satisfies ClassCtx} />}
    </div>
  );
}
