import { useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api } from '../api';
import BillForm from '../components/BillForm.jsx';
import BillAI from '../components/BillAI.jsx';
import WhyHigh from '../components/WhyHigh.jsx';
import { Badge, CATS, CAT_ICON, Empty, STATE_TONE, cap, inr, useToast } from '../ui.jsx';

const PAGE_SIZE = 50;

export default function Bills() {
  const [sp, setSp] = useSearchParams();
  const f = { category: sp.get('category') || '', month: sp.get('month') || '', status: sp.get('status') || '' };
  const [data, setData] = useState(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [edit, setEdit] = useState(null);
  const requestRef = useRef(0);
  const toast = useToast();

  const load = (offset = 0, append = false) => {
    // Rapid filter changes can overlap; only the newest request may render.
    const id = ++requestRef.current;
    if (append) setLoadingMore(true);
    else setData(null);
    return api.bills({ ...f, limit: PAGE_SIZE, offset })
      .then((page) => {
        if (id !== requestRef.current) return;
        setData((prev) => (append && prev ? { ...page, items: [...prev.items, ...page.items] } : page));
      })
      .catch((e) => { if (id === requestRef.current) toast(e.message, 'bad'); })
      .finally(() => { if (id === requestRef.current) setLoadingMore(false); });
  };

  useEffect(() => { load(); }, [f.category, f.month, f.status]);
  const set = (k) => (e) => { const n = new URLSearchParams(sp); e.target.value ? n.set(k, e.target.value) : n.delete(k); setSp(n); };

  const act = async (fn, msg) => { try { await fn(); toast(msg); load(); } catch (e) { toast(e.message, 'bad'); } };
  const remove = (b) => window.confirm(`Delete this ${b.category} bill of ${inr(b.amount)}?`) && act(() => api.deleteBill(b.id), 'Bill deleted.');

  return (
    <>
      <header className="page-head"><h1>All bills</h1></header>
      <div className="filters">
        <label>Type<select value={f.category} onChange={set('category')}><option value="">All</option>{CATS.map((c) => <option key={c} value={c}>{cap(c)}</option>)}</select></label>
        <label>Month<input type="month" value={f.month} onChange={set('month')} /></label>
        <label>Status<select value={f.status} onChange={set('status')}>
          <option value="">All</option><option value="unpaid">Unpaid</option><option value="overdue">Overdue</option><option value="paid">Paid</option><option value="draft">Waiting for review</option></select></label>
      </div>

      {edit && <section className="card"><h2>Edit bill</h2>
        <BillForm key={edit.id} initial={edit} submitLabel={edit.reviewed ? 'Save changes' : 'Confirm & save'} onCancel={() => setEdit(null)}
          onSubmit={async (b) => { try { await (edit.reviewed ? api.updateBill(edit.id, b) : api.confirmBill(edit.id, b)); toast('Saved.'); setEdit(null); load(); } catch (e) { toast(e.message, 'bad'); } }} /></section>}

      {!data ? <p className="muted">Loading…</p> : data.items.length === 0 ? <Empty title="No bills match">Change the filters or add a bill.</Empty> : (
        <>
          <div className="table-wrap"><table>
            <thead><tr><th>Bill</th><th>Due</th><th className="r">Amount</th><th>Status</th><th>Insight</th><th /></tr></thead>
            <tbody>{data.items.map((b) => (
              <tr key={b.id}>
                <td><strong>{CAT_ICON[b.category]} {b.provider || cap(b.category)}</strong><small>{cap(b.category)}{b.units ? ` · ${b.units} kWh` : ''}</small></td>
                <td>{b.due_date || '—'}</td>
                <td className="r num">{inr(b.amount)}</td>
                <td><Badge tone={STATE_TONE[b.state]}>{cap(b.state)}</Badge></td>
                <td className="insight">{b.analysis ? <span className={b.analysis.is_anomaly ? 'bad' : ''}>{b.analysis.is_anomaly ? '⚠ ' : ''}{b.analysis.summary}</span> : <span className="muted">—</span>}
                  {b.analysis?.is_anomaly && !b.feedback && <div><WhyHigh billId={b.id} onDone={load} /></div>}
                  {b.feedback && <small>Your note: {b.feedback.reason.replace('_', ' ')}</small>}
                  {b.reviewed && <BillAI billId={b.id} />}</td>
                <td className="row-actions">
                  {b.reviewed && <button className="btn sm" onClick={() => act(() => api.setPaid(b.id, !b.paid), b.paid ? 'Marked unpaid.' : 'Marked paid.')}>{b.paid ? 'Undo paid' : 'Mark paid'}</button>}
                  {b.has_file && <a className="btn sm" href={`/api/bills/${b.id}/file`} target="_blank" rel="noreferrer">File</a>}
                  <button className="btn sm" onClick={() => { setEdit(b); window.scrollTo({ top: 0, behavior: 'smooth' }); }}>{b.reviewed ? 'Edit' : 'Review'}</button>
                  <button className="btn sm bad" onClick={() => remove(b)}>Delete</button>
                </td>
              </tr>))}</tbody>
          </table></div>
          {data.items.length < data.total && (
            <p className="center">
              <button className="btn" onClick={() => load(data.items.length, true)} disabled={loadingMore}>
                {loadingMore ? 'Loading…' : `Load more (${data.total - data.items.length} left)`}
              </button>
            </p>
          )}
        </>
      )}
    </>
  );
}
