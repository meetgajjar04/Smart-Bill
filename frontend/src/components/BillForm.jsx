import { useState } from 'react';
import { CATS, cap } from '../ui.jsx';

const blank = { category: 'electricity', provider: '', account_number: '', bill_date: '', due_date: '', amount: '', units: '', notes: '' };

export default function BillForm({ initial, submitLabel, onSubmit, onCancel, flagged = [] }) {
  const [f, setF] = useState({ ...blank, ...Object.fromEntries(Object.entries(initial || {}).filter(([k]) => k in blank).map(([k, v]) => [k, v ?? ''])) });
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const warn = (k) => (flagged.includes(k) ? 'needs-check' : '');

  const submit = async (e) => {
    e.preventDefault(); setBusy(true);
    const body = { category: f.category, provider: f.provider || null, account_number: f.account_number || null,
      bill_date: f.bill_date || null, due_date: f.due_date, amount: Number(f.amount),
      units: f.units === '' ? null : Number(f.units), notes: f.notes || null };
    try { await onSubmit(body); } finally { setBusy(false); }
  };

  return (
    <form className="form-grid" onSubmit={submit}>
      <label>Type
        <select value={f.category} onChange={set('category')}>{CATS.map((c) => <option key={c} value={c}>{cap(c)}</option>)}</select>
      </label>
      <label className={warn('provider')}>Provider<input maxLength={80} value={f.provider} onChange={set('provider')} placeholder="e.g. Adani Electricity" /></label>
      <label className={warn('amount')}>Amount (₹)<input type="number" required min="0.01" max="10000000" step="0.01" value={f.amount} onChange={set('amount')} /></label>
      <label className={warn('due_date')}>Due date<input type="date" required value={f.due_date} onChange={set('due_date')} /></label>
      <label className={warn('bill_date')}>Bill date<input type="date" value={f.bill_date} onChange={set('bill_date')} /></label>
      <label>Account / consumer no.<input maxLength={40} value={f.account_number} onChange={set('account_number')} /></label>
      {f.category === 'electricity' && <label>Units used (kWh)<input type="number" min="0" step="0.1" value={f.units} onChange={set('units')} /></label>}
      <label className="wide">Notes<input maxLength={300} value={f.notes} onChange={set('notes')} /></label>
      <div className="actions wide">
        <button className="btn primary" disabled={busy}>{busy ? 'Saving…' : submitLabel}</button>
        {onCancel && <button type="button" className="btn" onClick={onCancel}>Cancel</button>}
      </div>
    </form>
  );
}
