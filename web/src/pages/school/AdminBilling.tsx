// /school/admin: the Forbrug (usage and limits) and Tilkøb (subscription, add-ons, EAN invoicing) tabs.
// Nothing is paid here: add-ons are registered and invoiced by Happy Mates on the next EAN invoice.
import { useState } from "react";
import { api, query } from "../../api";
import { Alert, download, fmtBytes, fmtDate, fmtNumber, Loading, useLoad } from "../../ui";
import { tokens } from "./AdminPeople";
import { useAction } from "./shared";
import type { Seats } from "./Admin";

type UsageData = {
  month: string;
  seats: Seats;
  storage: { used_bytes: number; total_gb: number | null };
  ai: { used: number; school_monthly_tokens: number; per_user_monthly_tokens: number };
  per_class: { id: number; name: string; teachers: string | null; students: number; ai_tokens: number; storage_bytes: number }[];
};

function lastMonths(n: number): { value: string; label: string }[] {
  const fmt = new Intl.DateTimeFormat("da-DK", { month: "long", year: "numeric" });
  const now = new Date();
  return Array.from({ length: n }, (_, i) => {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    const label = fmt.format(d);
    return { value: `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`, label: label[0].toUpperCase() + label.slice(1) };
  });
}

function Meter({ label, value, used, max, sub }: { label: string; value: string; used: number; max: number | null; sub: string }) {
  const pct = max ? Math.min(100, (100 * used) / max) : 0;
  return (
    <div className="card">
      <span className="muted small">{label}</span>
      <span className="stat" style={{ fontSize: 26 }}>{value}</span>
      {max ? (
        <div className="bar" role="img" aria-label={`${Math.round(pct)} % brugt`}>
          <div style={{ width: `${pct}%`, background: pct >= 95 ? "#dc2626" : pct >= 80 ? "#d97706" : undefined }} />
        </div>
      ) : null}
      <span className="muted tiny">{sub}</span>
    </div>
  );
}

export function Usage() {
  const months = lastMonths(12);
  const [month, setMonth] = useState(months[0].value);
  const data = useLoad(() => api.get<UsageData>(`/api/school/admin/usage${query({ month })}`), [month]);
  const act = useAction();
  const [perUser, setPerUser] = useState<string | null>(null);
  const [school, setSchool] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  if (!data.data) return data.loading ? <Loading /> : <Alert>{data.error}</Alert>;
  const u = data.data;
  const perUserValue = perUser ?? String(u.ai.per_user_monthly_tokens);
  const schoolValue = school ?? String(u.ai.school_monthly_tokens);
  const storageMax = u.storage.total_gb !== null ? u.storage.total_gb * 1024 ** 3 : null;

  const save = (e: React.FormEvent) => {
    e.preventDefault();
    setSaved(false);
    act.run(async () => {
      await api.put("/api/school/admin/limits", {
        per_user_monthly_tokens: Number(perUserValue.replace(/\D/g, "") || 0),
        school_monthly_tokens: Number(schoolValue.replace(/\D/g, "") || 0),
      });
      setPerUser(null);
      setSchool(null);
      await data.reload();
      setSaved(true);
    });
  };

  return (
    <div className="stack" style={{ "--gap": "20px" } as React.CSSProperties}>
      <div className="spread" style={{ alignItems: "flex-end" }}>
        <p className="muted" style={{ maxWidth: 640 }}>
          Hvad skolen bruger af elevpladser, AI og lagerplads. GitHub Actions og Codespaces ser du i organisationen på GitHub.
        </p>
        <label className="field">Periode
          <select value={month} onChange={(e) => setMonth(e.target.value)}>
            {months.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
          </select>
        </label>
      </div>
      <div className="grid" style={{ "--min": "200px" } as React.CSSProperties}>
        <Meter label="Elevpladser" used={u.seats.used} max={u.seats.total}
          value={u.seats.total !== null ? `${fmtNumber(u.seats.used)} af ${fmtNumber(u.seats.total)}` : fmtNumber(u.seats.used)}
          sub={u.seats.total !== null ? `${fmtNumber(Math.max(0, u.seats.total - u.seats.used))} ${u.seats.total - u.seats.used === 1 ? "ledig" : "ledige"} · nu` : "Ingen pladsgrænse endnu"} />
        <Meter label="AI-assistent" used={u.ai.used} max={u.ai.school_monthly_tokens || null} value={tokens(u.ai.used)}
          sub={u.ai.school_monthly_tokens ? `af ${tokens(u.ai.school_monthly_tokens)} for skolen` : "Intet loft for skolen"} />
        <Meter label="Lagerplads" used={u.storage.used_bytes} max={storageMax} value={fmtBytes(u.storage.used_bytes)}
          sub={u.storage.total_gb !== null ? `af ${fmtNumber(u.storage.total_gb)} GB · nu` : "Ingen grænse endnu"} />
      </div>

      <div className="class-columns">
        <section className="stack" style={{ flex: "999 1 560px", minWidth: 0 }} aria-labelledby="pr-klasse-h">
          <h3 id="pr-klasse-h">Forbrug pr. klasse</h3>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Klasse</th><th>Elever</th><th>AI</th><th>Lager</th></tr></thead>
              <tbody>
                {u.per_class.length === 0 && <tr><td colSpan={4} className="muted">Ingen aktive klasser.</td></tr>}
                {u.per_class.map((r) => (
                  <tr key={r.id}>
                    <td className="wrap"><strong>{r.name}</strong><div className="muted tiny">{r.teachers ?? ""}</div></td>
                    <td className="num">{r.students}</td>
                    <td className="num">{tokens(r.ai_tokens)}</td>
                    <td className="num">{fmtBytes(r.storage_bytes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="muted tiny">En elev i flere klasser tæller med i hver af dem.</p>
        </section>
        <form className="card" style={{ flex: "1 1 300px" }} onSubmit={save} aria-labelledby="graenser-h">
          <h3 id="graenser-h">Grænser for AI</h3>
          <label className="field">Pr. bruger pr. måned (tokens)
            <input inputMode="numeric" value={perUserValue} onChange={(e) => { setPerUser(e.target.value); setSaved(false); }} />
          </label>
          <label className="field">For hele skolen pr. måned (tokens)
            <input inputMode="numeric" value={schoolValue} onChange={(e) => { setSchool(e.target.value); setSaved(false); }} />
          </label>
          <p className="muted small">0 for hele skolen betyder intet loft. Når skolens loft er nået, stopper AI for alle resten af måneden.</p>
          {act.error && <Alert>{act.error}</Alert>}
          {saved && <Alert kind="success">Grænserne er gemt.</Alert>}
          <div className="row"><button className="btn btn-primary" disabled={act.busy}>Gem grænser</button></div>
        </form>
      </div>
    </div>
  );
}

type CatalogItem = {
  key: string; title: string; description: string; repeatable: boolean; price: string;
  active: { id: number; quantity: number; ordered_at: string; ordered_by: string | null }[];
};
type Order = { id: number; addon: string; quantity: number; status: "active" | "cancelled"; ordered_at: string;
               cancelled_at: string | null; ordered_by: string | null };
type SubscriptionView = {
  subscription: { plan: string; seats: number; storage_gb: number; price_text: string; renews_on: string | null;
                  ean: string | null; invoice_reference: string | null; updated_at: string } | null;
  contact: { id: number; display_name: string | null; email: string | null } | null;
  seats: Seats;
  storage: { used_bytes: number; total_gb: number | null };
  catalog: CatalogItem[];
  orders: Order[];
  prices: Record<string, string>;
  can_edit_plan: boolean;
};

function BillingForm({ view, onSaved }: { view: SubscriptionView; onSaved: () => Promise<void> }) {
  const act = useAction();
  const [ean, setEan] = useState(view.subscription?.ean ?? "");
  const [reference, setReference] = useState(view.subscription?.invoice_reference ?? "");
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await api.put("/api/school/admin/subscription/billing", { ean: ean.replace(/\s/g, ""), invoice_reference: reference.trim() || null, contact_me: true });
      await onSaved();
    });
  };
  return (
    <form className="stack" onSubmit={submit}>
      <div className="grid" style={{ "--min": "200px" } as React.CSSProperties}>
        <label className="field">EAN-nummer
          <input value={ean} onChange={(e) => setEan(e.target.value)} inputMode="numeric" placeholder="13 cifre" required />
        </label>
        <label className="field">Reference på fakturaen
          <input value={reference} onChange={(e) => setReference(e.target.value)} placeholder="fx IT-afdelingen" />
        </label>
      </div>
      <p className="muted small">Du bliver kontaktperson for fakturaen.</p>
      {act.error && <Alert>{act.error}</Alert>}
      <div className="row"><button className="btn btn-primary" disabled={act.busy}>Gem fakturaoplysninger</button></div>
    </form>
  );
}

function PlanForm({ view, onSaved }: { view: SubscriptionView; onSaved: () => Promise<void> }) {
  const s = view.subscription;
  const act = useAction();
  const [plan, setPlan] = useState(s?.plan ?? "Skole");
  const [seats, setSeats] = useState(String(s?.seats ?? 0));
  const [storage, setStorage] = useState(String(s?.storage_gb ?? 0));
  const [price, setPrice] = useState(s?.price_text ?? "");
  const [renews, setRenews] = useState(s?.renews_on ?? "");
  const [prices, setPrices] = useState<Record<string, string>>(view.prices);
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await api.put("/api/school/admin/subscription", { plan, seats: Number(seats), storage_gb: Number(storage), price_text: price,
                                                      renews_on: renews || null, prices });
      await onSaved();
    });
  };
  return (
    <form className="card" onSubmit={submit} aria-labelledby="plan-h">
      <h3 id="plan-h">Abonnement og priser</h3>
      <p className="muted small">Kun for Happy Mates (administratorer af Gær): det, der er aftalt med skolen.</p>
      <div className="grid" style={{ "--min": "180px" } as React.CSSProperties}>
        <label className="field">Plan<input value={plan} onChange={(e) => setPlan(e.target.value)} required /></label>
        <label className="field">Elevpladser<input inputMode="numeric" value={seats} onChange={(e) => setSeats(e.target.value)} /></label>
        <label className="field">Lagerplads (GB)<input inputMode="numeric" value={storage} onChange={(e) => setStorage(e.target.value)} /></label>
        <label className="field">Pris<input value={price} onChange={(e) => setPrice(e.target.value)} placeholder="fx 1.200 kr. pr. måned" /></label>
        <label className="field">Fornyes<input type="date" value={renews} onChange={(e) => setRenews(e.target.value)} /></label>
      </div>
      <div className="grid" style={{ "--min": "220px" } as React.CSSProperties}>
        {view.catalog.map((c) => (
          <label key={c.key} className="field">Pris: {c.title}
            <input value={prices[c.key] ?? ""} onChange={(e) => setPrices({ ...prices, [c.key]: e.target.value })} placeholder="Pris efter aftale" />
          </label>
        ))}
      </div>
      {act.error && <Alert>{act.error}</Alert>}
      <div className="row"><button className="btn btn-primary" disabled={act.busy}>Gem abonnement</button></div>
    </form>
  );
}

export function Addons() {
  const data = useLoad(() => api.get<SubscriptionView>("/api/school/admin/subscription"));
  const act = useAction();
  const [editBilling, setEditBilling] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  if (!data.data) return data.loading ? <Loading /> : <Alert>{data.error}</Alert>;
  const v = data.data;
  const s = v.subscription;
  const hasEan = !!s?.ean;

  const order = (c: CatalogItem) => {
    const text = `Bestil »${c.title}« (${c.price})? Det kommer med på skolens næste EAN-faktura.`;
    if (!confirm(text)) return;
    setDone(null);
    act.run(async () => { await api.post("/api/school/admin/addons", { addon: c.key, quantity: 1 }); await data.reload(); setDone(`${c.title} er bestilt.`); });
  };
  const cancel = (c: CatalogItem, id: number) => {
    if (!confirm(`Opsig »${c.title}«? Det gælder fra i dag.`)) return;
    setDone(null);
    act.run(async () => { await api.post(`/api/school/admin/addons/${id}/cancel`); await data.reload(); setDone(`${c.title} er opsagt.`); });
  };

  return (
    <div className="stack" style={{ "--gap": "20px" } as React.CSSProperties}>
      <section className="card" aria-labelledby="abon-h">
        <div className="spread" style={{ alignItems: "flex-start" }}>
          <div className="stack" style={{ "--gap": "4px" } as React.CSSProperties}>
            <span className="eyebrow">Abonnement</span>
            <h3 id="abon-h" style={{ fontSize: 20 }}>
              {s ? `${s.plan} · ${fmtNumber(v.seats.total ?? s.seats)} elevpladser` : "Intet abonnement registreret endnu"}
            </h3>
            {s && <span className="muted">{s.price_text || "Pris efter aftale"}{s.renews_on ? ` · fornyes ${fmtDate(s.renews_on).split(",")[0]}` : ""}</span>}
          </div>
          <dl className="facts">
            <dt>Betaling</dt><dd>{hasEan ? `EAN-faktura · ${s!.ean}` : "EAN-nummer mangler"}</dd>
            <dt>Reference</dt><dd>{s?.invoice_reference || "—"}</dd>
            <dt>Kontaktperson</dt><dd>{v.contact ? (v.contact.display_name ?? v.contact.email ?? `#${v.contact.id}`) : "—"}</dd>
          </dl>
          <div className="row">
            <button className="btn btn-outline" onClick={() => setEditBilling(!editBilling)}>{hasEan ? "Ret EAN og reference" : "Udfyld EAN-nummer"}</button>
            <button className="btn btn-outline" onClick={() => download("/api/school/admin/billing.csv")}>Hent fakturagrundlag (CSV)</button>
          </div>
        </div>
        {(editBilling || !hasEan) && <BillingForm view={v} onSaved={async () => { setEditBilling(false); await data.reload(); }} />}
      </section>

      {done && <Alert kind="success">{done}</Alert>}
      {act.error && <Alert>{act.error}</Alert>}

      <div className="grid" style={{ "--min": "260px" } as React.CSSProperties}>
        {v.catalog.map((c) => {
          const units = c.active.reduce((n, a) => n + a.quantity, 0);
          return (
            <article key={c.key} className="card">
              <div className="spread" style={{ alignItems: "flex-start" }}>
                <h3>{c.title}</h3>
                {units > 0 && <span className="pill green">{c.repeatable ? `Aktiv · ${units}×` : "Aktiv"}</span>}
              </div>
              <p className="muted small" style={{ flex: "1 1 auto" }}>{c.description}</p>
              <strong className="small">{c.price}</strong>
              <div className="row">
                {(c.repeatable || units === 0) && (
                  <button className="btn btn-primary btn-sm" disabled={act.busy || !hasEan} title={hasEan ? undefined : "Udfyld EAN-nummer først"}
                    onClick={() => order(c)}>{units > 0 ? "Bestil én mere" : "Bestil"}</button>
                )}
                {c.active.map((a) => (
                  <button key={a.id} className="btn btn-outline btn-sm" disabled={act.busy} onClick={() => cancel(c, a.id)}>
                    Opsig{c.repeatable && c.active.length > 1 ? ` (${fmtDate(a.ordered_at).split(",")[0]})` : ""}
                  </button>
                ))}
              </div>
            </article>
          );
        })}
        <article className="card">
          <div className="spread" style={{ alignItems: "flex-start" }}><h3>GitHub Codespaces</h3><span className="pill">Hos GitHub</span></div>
          <p className="muted small" style={{ flex: "1 1 auto" }}>Udviklingsmiljø i browseren til eleverne. Købes og betales i organisationen på GitHub.</p>
          <strong className="small">Betales hos GitHub</strong>
          <div className="row"><a className="btn btn-outline btn-sm" href="https://github.com/settings/organizations" target="_blank" rel="noreferrer">Åbn GitHub</a></div>
        </article>
      </div>
      <p className="muted small">Tilkøb bestilles af en skoleadministrator, gælder fra i dag og kommer med på næste EAN-faktura. Alle bestillinger og opsigelser står i ændringsloggen.</p>

      {v.orders.length > 0 && (
        <section className="stack" aria-labelledby="orders-h">
          <h3 id="orders-h">Bestillinger</h3>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Tilkøb</th><th>Antal</th><th>Bestilt</th><th>Status</th></tr></thead>
              <tbody>
                {v.orders.map((o) => (
                  <tr key={o.id}>
                    <td>{v.catalog.find((c) => c.key === o.addon)?.title ?? o.addon}</td>
                    <td className="num">{o.quantity}</td>
                    <td>{fmtDate(o.ordered_at)}{o.ordered_by ? ` · ${o.ordered_by}` : ""}</td>
                    <td>{o.status === "active" ? <span className="pill green">Aktiv</span> : <span className="pill">Opsagt {fmtDate(o.cancelled_at)}</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {v.can_edit_plan && <PlanForm view={v} onSaved={data.reload} />}
    </div>
  );
}
