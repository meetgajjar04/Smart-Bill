import { useState } from 'react';
import { api } from '../api';
import { inr, useToast } from '../ui.jsx';

/** Slide a setting, see the yearly rupee impact calculated by Smart Bill AI. */
export default function WhatIf({ base = {} }) {
  const [v, setV] = useState({ ac_hours_peak: base.ac_hours_peak ?? 6, ac_count: base.ac_count ?? 1, solar_kw: base.solar_kw ?? 0, electric_geyser: !!base.electric_geyser });
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const run = async () => {
    setBusy(true);
    try { setRes(await api.whatIf(v)); } catch (e) { toast(e.message, 'bad'); } finally { setBusy(false); }
  };
  const num = (k) => (e) => setV({ ...v, [k]: Number(e.target.value) });
  return (
    <section className="card">
      <h2>What if…? <small className="muted">estimates from your home model</small></h2>
      <div className="form-grid">
        <label>AC hours per day in peak summer: <b>{v.ac_hours_peak}</b>
          <input type="range" min="0" max="16" step="0.5" value={v.ac_hours_peak} onChange={num('ac_hours_peak')} /></label>
        <label>Number of ACs: <b>{v.ac_count}</b>
          <input type="range" min="0" max="6" step="1" value={v.ac_count} onChange={num('ac_count')} /></label>
        <label>Rooftop solar (kW): <b>{v.solar_kw}</b>
          <input type="range" min="0" max="10" step="0.5" value={v.solar_kw} onChange={num('solar_kw')} /></label>
        <label className="check"><input type="checkbox" checked={v.electric_geyser} onChange={(e) => setV({ ...v, electric_geyser: e.target.checked })} /> Electric geyser</label>
      </div>
      <button className="btn primary" onClick={run} disabled={busy}>{busy ? 'Calculating…' : 'Show the impact'}</button>
      {res && (
        <p className="forecast">
          {res.annual_saving >= 0 ? 'You would save ' : 'This would cost you '}<b className={`num ${res.annual_saving >= 0 ? 'ok' : 'bad'}`}>{inr(Math.abs(res.annual_saving))}</b> a year
          <span className="muted"> · about {inr(Math.abs(res.monthly_saving))}/month · {inr(res.baseline_annual)} → {inr(res.new_annual)} per year</span></p>)}
    </section>
  );
}
