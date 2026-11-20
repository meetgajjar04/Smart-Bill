import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Bar, Doughnut } from 'react-chartjs-2';
import { api } from '../api';
import WhyHigh from '../components/WhyHigh.jsx';
import { CATS, CAT_COLOR, CAT_ICON, Empty, cap, dueText, inr, monthLabel, useToast } from '../ui.jsx';

export default function Dashboard({ user }) {
  const [s, setS] = useState(null);
  const [alerts, setAlerts] = useState([]);
  const [fc, setFc] = useState(null);
  const [prof, setProf] = useState(null);
  const [summaryError, setSummaryError] = useState(null);
  const toast = useToast();
  const load = () => {
    setSummaryError(null);
    api.summary().then(setS).catch((e) => { setSummaryError(e.message); toast(e.message, 'bad'); });
    api.alerts().then(setAlerts).catch(() => {});
  };
  useEffect(() => { load(); api.profile().then(setProf).catch(() => {}); api.predict('electricity').then((p) => setFc(p.forecast)).catch(() => {}); }, []);

  if (!s) {
    return summaryError
      ? <p className="note warn">Could not load your dashboard: {summaryError} <button className="link" onClick={load}>Retry</button></p>
      : <p className="muted">Loading your bills…</p>;
  }
  const empty = s.trend.every((r) => CATS.every((c) => !r[c]));
  const pay = async (b) => { try { await api.setPaid(b.id, true); toast('Marked as paid.'); load(); } catch (e) { toast(e.message, 'bad'); } };
  const dismiss = async (a) => { await api.readAlert(a.id).catch(() => {}); setAlerts(alerts.filter((x) => x.id !== a.id)); };

  return (
    <>
      <header className="page-head"><h1>Hello, {user.name.split(' ')[0]}</h1>
        <p>{s.overdue_count ? `${s.overdue_count} bill${s.overdue_count > 1 ? 's are' : ' is'} overdue.` : s.due_soon_count ? `${s.due_soon_count} due this week.` : 'Nothing due this week.'}</p></header>

      {s.draft_count > 0 && <p className="note warn">You have {s.draft_count} uploaded bill(s) waiting for your check. <Link to="/bills?status=draft">Review them</Link></p>}
      {prof && prof.completeness < 70 && <p className="note warn">Your forecast is {prof.completeness}% informed by your home details. <Link to="/home">Answer a few quick questions</Link> to make it more accurate.</p>}
      {alerts.map((a) => (
        <div key={a.id} className={`alert ${a.severity}`} role="alert">
          <span>{a.message} {a.kind === 'anomaly' && a.bill_id && <WhyHigh billId={a.bill_id} onDone={load} />}</span><button className="link" onClick={() => dismiss(a)}>Dismiss</button>
        </div>
      ))}

      <section className="kpis">
        <div className="kpi"><small>This month</small><b className="num">{inr(s.this_month)}</b>
          <span className={s.change_pct > 0 ? 'bad' : 'ok'}>{s.change_pct == null ? 'no earlier month to compare' : `${s.change_pct > 0 ? '▲' : '▼'} ${Math.abs(s.change_pct)}% vs last month`}</span></div>
        <div className="kpi"><small>Still to pay</small><b className="num">{inr(s.unpaid_amount)}</b><span>{s.unpaid_count} bill{s.unpaid_count === 1 ? '' : 's'}</span></div>
        <div className="kpi"><small>Overdue</small><b className={`num ${s.overdue_count ? 'bad' : ''}`}>{s.overdue_count}</b><span>{s.overdue_count ? 'pay these first' : 'all caught up'}</span></div>
        <div className="kpi"><small>Electricity next month</small><b className="num">{fc ? inr(fc.amount) : '—'}</b>
          <span>{fc ? `likely ${inr(fc.low)}–${inr(fc.high)}` : 'needs a few months of bills'}</span></div>
      </section>

      {empty ? <Empty title="No bills yet">Add your first bill and the charts, forecast and tips will appear here. <Link to="/upload">Add a bill</Link></Empty> : (
        <div className="grid2">
          <section className="card slips">
            <h2>Coming due</h2>
            {s.upcoming.length === 0 && <p className="muted">Everything is paid. 🎉</p>}
            {s.upcoming.map((b) => { const d = dueText(b.due_date); return (
              <div key={b.id} className={`slip ${d.tone}`}>
                <div><strong>{CAT_ICON[b.category]} {b.provider || cap(b.category)}</strong><small>{d.text}</small></div>
                <b className="num">{inr(b.amount)}</b>
                <button className="btn sm" onClick={() => pay(b)}>Mark paid</button>
              </div>); })}
          </section>
          <section className="card">
            <h2>Where the money goes <small className="muted">last 12 months</small></h2>
            <div className="chart sm"><Doughnut data={{ labels: CATS.map(cap), datasets: [{ data: CATS.map((c) => s.share[c]), backgroundColor: CATS.map((c) => CAT_COLOR[c]), borderWidth: 0 }] }}
              options={{ maintainAspectRatio: false, cutout: '62%', plugins: { legend: { position: 'right' } } }} /></div>
          </section>
          <section className="card wide">
            <h2>Monthly spending</h2>
            <div className="chart"><Bar data={{ labels: s.trend.map((r) => monthLabel(r.month)), datasets: CATS.map((c) => ({ label: cap(c), data: s.trend.map((r) => r[c]), backgroundColor: CAT_COLOR[c], stack: 's' })) }}
              options={{ maintainAspectRatio: false, scales: { x: { stacked: true, grid: { display: false } }, y: { stacked: true, ticks: { callback: (v) => inr(v) } } },
                plugins: { legend: { position: 'bottom' }, tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${inr(c.raw)}` } } } }} /></div>
          </section>
        </div>
      )}
    </>
  );
}
