import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { Line } from 'react-chartjs-2';
import { api } from '../api';
import WhatIf from '../components/WhatIf.jsx';
import { Badge, CATS, CAT_COLOR, Empty, cap, inr, monthLabel, useToast } from '../ui.jsx';

const TONE = { high: 'bad', medium: 'warn', low: 'muted' };

export default function Insights() {
  const [cat, setCat] = useState('electricity');
  const [p, setP] = useState(null);
  const [anoms, setAnoms] = useState([]);
  const [anomsBusy, setAnomsBusy] = useState(true);
  const [recs, setRecs] = useState([]);
  const [recsBusy, setRecsBusy] = useState(true);
  const [prof, setProf] = useState(null);
  const [ai, setAi] = useState(null);
  const [aiStatus, setAiStatus] = useState(null);
  const [aiBusy, setAiBusy] = useState(false);
  const [predictError, setPredictError] = useState(null);
  const [anomsError, setAnomsError] = useState(false);
  const [recsError, setRecsError] = useState(false);
  const predictRef = useRef(0);
  const tabRefs = useRef({});
  const toast = useToast();

  const loadAnomalies = () => {
    setAnomsBusy(true);
    setAnomsError(false);
    api.anomalies().then(setAnoms).catch(() => setAnomsError(true)).finally(() => setAnomsBusy(false));
  };
  const loadPredict = () => {
    const id = ++predictRef.current;
    setP(null);
    setPredictError(null);
    api.predict(cat).then((result) => {
      if (id === predictRef.current) setP(result);
    }).catch((e) => {
      if (id === predictRef.current) setPredictError(e.message);
    });
  };

  useEffect(() => {
    loadAnomalies();
    api.recommendations().then(setRecs).catch((e) => { setRecsError(true); toast(e.message, 'bad'); }).finally(() => setRecsBusy(false));
    api.profile().then(setProf).catch(() => {});
  }, []);
  useEffect(() => {
    api.portfolioAi().then(setAi).catch(() => {});
    api.aiStatus().then(setAiStatus).catch(() => {});
  }, []);
  useEffect(loadPredict, [cat]);

  const onTabKey = (e) => {
    const current = CATS.indexOf(cat);
    let next;
    if (e.key === 'ArrowRight') next = (current + 1) % CATS.length;
    else if (e.key === 'ArrowLeft') next = (current - 1 + CATS.length) % CATS.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = CATS.length - 1;
    if (next === undefined) return;
    e.preventDefault();
    const name = CATS[next];
    setCat(name);
    tabRefs.current[name]?.focus();
  };

  const runAi = async () => {
    setAiBusy(true);
    try { setAi(await api.analyzePortfolio()); toast('Deep analysis ready.'); }
    catch (e) { toast(e.message, 'bad'); }
    finally { setAiBusy(false); }
  };

  const hist = p?.history || [];
  const f = p?.forecast;
  const labels = [...hist.map((h) => monthLabel(h.month)), ...(f ? [monthLabel(f.month)] : [])];
  const color = CAT_COLOR[cat];
  const money = { y: { ticks: { callback: (v) => inr(v) } }, x: { grid: { display: false } } };
  const tip = { tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${inr(c.raw)}` } }, legend: { position: 'bottom' } };
  const nothing = p && hist.length === 0 && !f;
  const excluded = hist.filter((h) => h.excluded).length;
  const lastIdx = hist.length - 1;

  return (
    <>
      <header className="page-head"><h1>Insights</h1><p>Forecasts, unusual bills and ways to spend less — calculated on this computer from your bills and your home details.</p></header>

      <section className="card ai-card">
        <div className="ai-head">
          <div>
            <h2>Deep analysis of all your bills <small className="muted">by Smart Bill AI</small></h2>
            <p className="muted small">Reads every bill together and explains what changed, what is at risk and what to do. Runs entirely on this computer.</p>
          </div>
          <button className="btn" onClick={runAi} disabled={aiBusy || aiStatus?.enabled === false}>
            {aiBusy ? 'Analysing…' : ai?.analysis ? 'Re-run analysis' : 'Analyze all bills'}
          </button>
        </div>

        {aiStatus && !aiStatus.enabled && <p className="note warn">Smart Bill AI is disabled. Set <code>OLLAMA_ENABLED=true</code> and restart the backend.</p>}
        {aiStatus?.enabled && !aiStatus.available && (
          <p className="note warn">Smart Bill AI is not running at <code>{aiStatus.url}</code>. Start it with <code>ollama serve</code>, then reload this page. {aiStatus.error}</p>
        )}
        {aiStatus?.enabled && aiStatus.available && !aiStatus.model_ready && (
          <p className="note warn">{aiStatus.error}</p>
        )}

        {!ai?.analysis ? (
          <p className="muted">No analysis yet. {aiStatus?.model_ready ? 'Click “Analyze all bills” to have Smart Bill AI read your whole history.' : 'Install and start Ollama to unlock this.'}</p>
        ) : (
          <div className="ai-result">
            {ai.analysis.source === 'local-fallback' && (
              <p className="note warn">Smart Bill AI could not finish the deep read in time, so this is a quick built-in summary. Click “Re-run analysis” to try again.</p>
            )}
            {ai.analysis.source === 'unverified-fallback' && (
              <p className="note warn">Smart Bill AI answered, but its wording could not be checked against your bills, so this summary is the built-in version calculated from your data. Click “Re-run analysis” to try again.</p>
            )}
            <h3>{ai.analysis.headline || 'Your bills at a glance'}</h3>
            <p>{ai.analysis.summary}</p>
            {ai.analysis.trends?.length > 0 && (
              <>
                <strong className="ai-label">Trends</strong>
                <ul className="ai-list">{ai.analysis.trends.map((t) => <li key={t}>{t}</li>)}</ul>
              </>
            )}
            {ai.analysis.risks?.length > 0 && (
              <>
                <strong className="ai-label">Risks</strong>
                <ul className="ai-list">{ai.analysis.risks.map((t) => <li key={t}>{t}</li>)}</ul>
              </>
            )}
            {ai.analysis.actions?.length > 0 && (
              <>
                <strong className="ai-label">What to do next</strong>
                <div className="ai-actions">
                  {ai.analysis.actions.map((a, i) => (
                    <div key={i} className="ai-action">
                      <strong>{a.title}</strong>
                      {a.detail && <p>{a.detail}</p>}
                      {a.est_monthly_saving > 0 && <small className="ok">Could save about {inr(a.est_monthly_saving)}/month (estimate)</small>}
                    </div>
                  ))}
                </div>
              </>
            )}
            <small className="muted">Generated by Smart Bill AI{ai.created_at ? ` on ${new Date(ai.created_at).toLocaleString('en-IN')}` : ''}. Estimates only — check important figures against your bill.</small>
          </div>
        )}
      </section>

      <div className="tabs" role="tablist" onKeyDown={onTabKey}>
        {CATS.map((c) => (
          <button key={c} id={`tab-${c}`} ref={(el) => { tabRefs.current[c] = el; }} role="tab"
                  aria-selected={c === cat} aria-controls="forecast-panel" tabIndex={c === cat ? 0 : -1}
                  className={c === cat ? 'on' : ''} onClick={() => setCat(c)}>{cap(c)}</button>
        ))}
      </div>

      {cat === 'electricity' && prof && prof.completeness < 70 && <p className="note warn">Forecast quality depends on knowing your home. <Link to="/home">Answer a few questions ({prof.completeness}% done)</Link>.</p>}

      <section id="forecast-panel" role="tabpanel" aria-labelledby={`tab-${cat}`} className="card">
        <h2>{cap(cat)}: history and forecast</h2>
        {predictError ? <p className="note warn">Could not calculate this forecast: {predictError} <button className="link" onClick={loadPredict}>Retry</button></p>
          : !p ? <p className="muted">Calculating…</p>
            : nothing ? <Empty title={`No ${cat} bills yet`}>{cat === 'electricity' ? <>Add bills, or <Link to="/home">describe your home</Link> to get a forecast before you have any history.</> : 'Add a few months of bills to see a forecast.'}</Empty> : (
          <>
            <div className="chart"><Line data={{ labels, datasets: [
              { label: 'Actual', data: [...hist.map((h) => h.amount), null], borderColor: color, backgroundColor: color + '33', fill: true, tension: 0.25,
                pointBackgroundColor: hist.map((h) => (h.excluded ? '#8A97A3' : color)).concat([color]), pointRadius: hist.map((h) => (h.excluded ? 6 : 3)).concat([0]) },
              ...(f ? [
                { label: 'Forecast', data: [...hist.slice(0, -1).map(() => null), ...(lastIdx >= 0 ? [hist[lastIdx].amount] : []), f.amount], borderColor: color, borderDash: [6, 5], pointRadius: 5, pointStyle: 'rectRot' },
                { label: 'Likely range (90%)', data: [...hist.map(() => null), f.high], borderColor: 'transparent', backgroundColor: color + '55', pointRadius: 4, pointStyle: 'triangle' },
                { label: ' ', data: [...hist.map(() => null), f.low], borderColor: 'transparent', backgroundColor: color + '55', pointRadius: 4, pointStyle: 'triangle', rotation: 180 },
              ] : []),
            ] }} options={{ maintainAspectRatio: false, scales: money, plugins: { ...tip, legend: { position: 'bottom', labels: { filter: (i) => i.text.trim() } } } }} /></div>
            {f && <p className="forecast"><b className="num">{inr(f.amount)}</b> expected in {monthLabel(f.month)} <span className="muted">· likely {inr(f.low)}–{inr(f.high)}{f.mape != null ? ` · past forecasts missed by ${f.mape}% on average` : ''}</span></p>}
            {f && f.n_months === 0 && <p className="note warn">This forecast comes only from your home details. Add your bills and it will adjust to how you really use power.</p>}
            {f && f.n_months > 0 && f.n_months < 4 && <p className="note warn">Based on {f.n_months} month{f.n_months === 1 ? '' : 's'} of bills plus your home model. It sharpens with each bill you add.</p>}
            {excluded > 0 && <p className="muted small">Grey dots are months you told us were one-offs or before a change; they're left out of the forecast.</p>}
          </>
        )}
      </section>

      {f?.models?.length > 0 && (
        <div className="grid2">
          <section className="card">
            <h2>How this forecast was made</h2>
            <p className="muted small">Several models each make a prediction. The ones that have been most accurate for you get the most say.</p>
            {f.models.map((m) => (
              <div key={m.key} className="model-row">
                <div><strong>{m.name}</strong><small>predicts {inr(m.forecast)}{m.backtest_mape != null ? ` · usually off by ${m.backtest_mape}%` : ' · not tested yet'}</small></div>
                <div className="bar" aria-label={`${Math.round(m.weight * 100)}% influence`}><i style={{ width: `${m.weight * 100}%` }} /><span>{Math.round(m.weight * 100)}%</span></div>
              </div>))}
          </section>
          {p.drivers?.length > 0 ? (
            <section className="card">
              <h2>What drives your electricity in {monthLabel(f.month)}</h2>
              {p.drivers.map((d) => (
                <div key={d.name} className="model-row">
                  <div><strong>{d.name}</strong><small>{d.kwh} units a month</small></div>
                  <div className="bar"><i className={d.share < 0 ? 'neg' : ''} style={{ width: `${Math.abs(d.share)}%` }} /><span>{d.share}%</span></div>
                </div>))}
              <p className="muted small">Typical-home estimates from your answers; the real split depends on your habits.</p>
            </section>) : <section className="card"><h2>What drives your bill</h2><p className="muted">Describe your home to see which appliances cost you the most. <Link to="/home">My home</Link></p></section>}
        </div>
      )}

      {cat === 'electricity' && prof && prof.completeness > 0 && <WhatIf base={prof.profile} />}

      {p?.backtest?.length > 0 && (
        <section className="card">
          <h2>Predicted vs actual <small className="muted">{p.mape != null && `average miss ${p.mape}%`}</small></h2>
          <div className="chart sm"><Line data={{ labels: p.backtest.map((r) => monthLabel(r.month)), datasets: [
            { label: 'Actual', data: p.backtest.map((r) => r.actual), borderColor: color, tension: 0.2 },
            { label: 'Predicted', data: p.backtest.map((r) => r.predicted), borderColor: '#5b6b78', borderDash: [5, 4], tension: 0.2 }] }}
            options={{ maintainAspectRatio: false, scales: money, plugins: tip }} /></div>
          <p className="muted small">Each month was predicted using only the months before it, so this shows how the model really performs.</p>
        </section>
      )}

      <div className="grid2">
        <section className="card">
          <h2>Unusual bills</h2>
          {anoms.length === 0 ? (anomsBusy
            ? <p className="muted">Checking every month of your bills for unusual jumps…</p>
            : anomsError
              ? <p className="note warn">Could not check for unusual bills. <button className="link" onClick={loadAnomalies}>Retry</button></p>
              : <p className="muted">No unusual increases found. Needs 4+ months of history per bill type.</p>) :
            anoms.map((a) => (
              <div key={a.category + a.month} className="row">
                <div><strong>{cap(a.category)} · {monthLabel(a.month)}</strong><small>{a.reason}</small></div>
                <div className="r"><b className="num">{inr(a.amount)}</b><Badge tone={TONE[a.severity]}>{a.severity}</Badge></div>
              </div>))}
        </section>
        <section className="card">
          <h2>Ways to spend less</h2>
          {recs.length === 0 ? (recsBusy
            ? <p className="muted">Working out ways to spend less from your own bills…</p>
            : recsError
              ? <p className="note warn">Could not calculate suggestions. Refresh the page to try again.</p>
              : <p className="muted">Add a few bills to get personal suggestions.</p>) :
            recs.map((r, i) => (
              <div key={i} className="rec">
                <div className="rec-head"><strong>{r.title}</strong><Badge tone={TONE[r.priority]}>{r.priority}</Badge></div>
                <p>{r.detail}</p>
                {r.est_monthly_saving > 0 && <small className="ok">Could save about {inr(r.est_monthly_saving)}/month (estimate)</small>}
              </div>))}
        </section>
      </div>
    </>
  );
}
