import { useEffect, useState } from 'react';
import { api } from '../api';
import { useToast } from '../ui.jsx';

const GROUPS = ['Your home', 'Cooling & heating', 'Appliances', 'Your tariff (optional)'];

export default function Home() {
  const [data, setData] = useState(null);
  const [f, setF] = useState({});
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  useEffect(() => { api.profile().then((d) => { setData(d); setF(d.profile); }).catch((e) => toast(e.message, 'bad')); }, []);
  if (!data) return <p className="muted">Loading…</p>;

  const set = (id, v) => setF({ ...f, [id]: v });
  const save = async (e) => {
    e.preventDefault(); setBusy(true);
    const body = {};
    for (const q of data.questions) {
      const v = f[q.id];
      body[q.id] = v === '' || v === undefined ? null : v;
    }
    if (f.climate_zone) body.climate_zone = f.climate_zone;
    try { const d = await api.saveProfile(body); setData(d); setF(d.profile); toast('Saved. Your forecasts now use these answers.'); }
    catch (x) { toast(x.message, 'bad'); } finally { setBusy(false); }
  };

  const field = (q) => {
    const v = f[q.id] ?? '';
    if (q.depends_on && f[q.depends_on] === 0) return null;
    return (
      <label key={q.id} className={q.type === 'text' ? 'wide' : ''}>{q.label}
        {q.type === 'bool'
          ? <select value={v === '' ? '' : v ? 'yes' : 'no'} onChange={(e) => set(q.id, e.target.value === '' ? null : e.target.value === 'yes')}>
              <option value="">Not sure / skip</option><option value="yes">Yes</option><option value="no">No</option></select>
          : <input type={q.type === 'text' ? 'text' : 'number'} value={v} min={q.min} max={q.max} step={q.id.includes('hours') || q.id === 'solar_kw' || q.id === 'tariff_per_unit' ? '0.5' : '1'}
              maxLength={q.type === 'text' ? 60 : undefined} onChange={(e) => set(q.id, q.type === 'text' ? e.target.value : e.target.value === '' ? '' : Number(e.target.value))} />}
        <small>{q.why}</small>
      </label>
    );
  };

  const pct = data.completeness;
  return (
    <>
      <header className="page-head"><h1>My home</h1>
        <p>Answer what you can. Each answer sharpens your forecast, and you can skip anything you're unsure about.</p></header>
      <div className="meter big" aria-label={`Profile ${pct}% complete`}><i style={{ width: `${pct}%` }} />
        <span>{pct}% of the useful details filled in{pct < 60 ? ' — the biggest gains are at the top of each section' : pct < 100 ? ' — nearly there' : ' — complete'}</span></div>

      <form onSubmit={save}>
        {GROUPS.map((g) => {
          const qs = data.questions.filter((q) => q.group === g);
          return (
            <section className="card" key={g}><h2>{g}</h2>
              <div className="form-grid">{qs.map(field)}</div>
              {g === 'Your home' && data.zone_guessed && (
                <label className="wide">We don't know the climate of "{f.city}". Which is closest?
                  <select value={f.climate_zone || ''} onChange={(e) => set('climate_zone', e.target.value || null)}>
                    <option value="">Let us assume mixed (composite)</option><option value="hot_humid">Hot &amp; humid (coastal)</option><option value="hot_dry">Hot &amp; dry</option>
                    <option value="composite">Hot summers, cool winters</option><option value="temperate">Mild all year</option><option value="cold">Cold / hills</option></select></label>)}
            </section>);
        })}
        <div className="actions"><button className="btn primary" disabled={busy}>{busy ? 'Saving…' : 'Save my home details'}</button></div>
      </form>
      <p className="muted small" style={{ marginTop: '1.2rem' }}>How we use this: Smart Bill AI runs on this server and estimates your typical electricity use from these answers (trained on {data.model.households.toLocaleString('en-IN')} simulated households, {data.model.mape_holdout_pct}% average error on its own test set), then adjusts itself to your real bills. Nothing leaves your computer.</p>
    </>
  );
}
