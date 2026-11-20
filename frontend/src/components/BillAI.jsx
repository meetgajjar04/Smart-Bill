import { useState } from 'react';
import { api } from '../api';
import { Badge, useToast } from '../ui.jsx';

const VERDICT_TONE = { normal: 'ok', low: 'ok', watch: 'warn', high: 'bad', unclear: 'muted' };

/** Deep analysis of a single bill, produced by Smart Bill AI on demand. */
export default function BillAI({ billId }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  const show = async () => {
    if (open) { setOpen(false); return; }
    setOpen(true);
    if (data) return;
    try { const r = await api.billAi(billId); if (r.analysis) setData(r); } catch { /* no cached analysis yet */ }
  };
  const run = async () => {
    setBusy(true);
    try { setData(await api.analyzeBill(billId)); } catch (e) { toast(e.message, 'bad'); } finally { setBusy(false); }
  };

  const a = data?.analysis;
  return (
    <div className="ai-inline">
      <button className="link ai-toggle" onClick={show} aria-expanded={open}>✨ Deep analysis</button>
      {open && (
        <div className="ai-body">
          {!a && <button className="btn sm" onClick={run} disabled={busy}>{busy ? 'Thinking…' : 'Analyse this bill'}</button>}
          {a && (
            <>
              <div className="ai-title"><Badge tone={VERDICT_TONE[a.verdict] || 'muted'}>{a.verdict}</Badge><small className="muted">Smart Bill AI</small></div>
              <p>{a.summary}</p>
              {a.key_points?.length > 0 && <ul className="ai-list">{a.key_points.map((x) => <li key={x}>{x}</li>)}</ul>}
              {a.savings_tips?.length > 0 && (
                <><strong className="ai-label">Ways to save</strong><ul className="ai-list">{a.savings_tips.map((x) => <li key={x}>{x}</li>)}</ul></>
              )}
              {a.questions?.length > 0 && (
                <><strong className="ai-label">Worth checking</strong><ul className="ai-list">{a.questions.map((x) => <li key={x}>{x}</li>)}</ul></>
              )}
              <button className="link" onClick={run} disabled={busy}>{busy ? 'Thinking…' : 'Re-run'}</button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
