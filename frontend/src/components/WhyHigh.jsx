import { useState } from 'react';
import { api } from '../api';
import { useToast } from '../ui.jsx';

const REASONS = [
  ['guests', 'Guests or more people at home'], ['travel', 'Travel / unusual usage'], ['new_appliance', 'New appliance (AC, geyser, EV…)'],
  ['tariff_change', 'Tariff or plan changed'], ['meter_issue', 'Looks like a billing or meter error'], ['seasonal', 'Normal for this season'], ['other', 'Something else'],
];

/** Asks the user why a flagged bill was high. The answer changes how the model treats that month. */
export default function WhyHigh({ billId, onDone }) {
  const [open, setOpen] = useState(false);
  const toast = useToast();
  const send = async (reason) => {
    try { const r = await api.feedback(billId, { reason }); toast(r.message); setOpen(false); onDone && onDone(); }
    catch (e) { toast(e.message, 'bad'); }
  };
  if (!open) return <button className="link" onClick={() => setOpen(true)}>Tell us why</button>;
  return (
    <span className="why" role="group" aria-label="Why was this bill high?">
      {REASONS.map(([k, label]) => <button key={k} className="chip" onClick={() => send(k)}>{label}</button>)}
      <button className="link" onClick={() => setOpen(false)}>Cancel</button>
    </span>
  );
}
