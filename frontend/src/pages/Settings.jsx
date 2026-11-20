import { useEffect, useState } from 'react';
import { api } from '../api';
import { clearAllHistories } from '../chatHistory.js';
import { useToast } from '../ui.jsx';

export default function Settings({ user, setUser }) {
  const toast = useToast();
  const [mail, setMail] = useState(null);
  const [testing, setTesting] = useState(false);

  useEffect(() => { api.emailStatus().then(setMail).catch(() => {}); }, []);

  const toggle = async (e) => { try { setUser(await api.updateMe({ notify_email: e.target.checked })); toast('Preference saved.'); } catch (x) { toast(x.message, 'bad'); } };
  const testMail = async () => {
    setTesting(true);
    try { const r = await api.sendTestEmail(); toast(`Test e-mail sent to ${r.to}. Check your inbox.`); }
    catch (x) { toast(x.message, 'bad'); }
    finally { setTesting(false); }
  };
  const erase = async () => {
    if (!window.confirm('Delete your account and ALL bills and uploaded files? This cannot be undone.')) return;
    try {
      await api.deleteMe();
      clearAllHistories();
      setUser(null);
    } catch (e) {
      toast(e.message, 'bad');
    }
  };
  return (
    <>
      <header className="page-head"><h1>Settings</h1></header>
      <section className="card narrow">
        <p><strong>{user.name}</strong><br /><span className="muted">{user.email}</span></p>
        <label className="check"><input type="checkbox" checked={user.notify_email} onChange={toggle} /> E-mail me deadline reminders and unusual-bill alerts</label>
        {mail && (
          <div className="mail-panel">
            <p className="muted small">
              Deadline reminders are sent {mail.reminder_days_before} day{mail.reminder_days_before === 1 ? '' : 's'} before a bill is due.
              Unusual-bill alerts are sent when a newly added bill looks far above your normal use.
            </p>
            {mail.configured ? (
              <p className="note ok">E-mail delivery is ready via <b>{mail.host}</b>. Messages are sent to <b>{user.email}</b>.</p>
            ) : (
              <p className="note warn">
                E-mail delivery is not set up yet. Add <code>SMTP_HOST</code>, <code>SMTP_PORT</code>, <code>SMTP_USERNAME</code>, <code>SMTP_PASSWORD</code> and <code>SMTP_FROM</code> to <code>.env</code>, then restart the backend. Until then, alerts still appear in the app.
              </p>
            )}
            <button className="btn sm" onClick={testMail} disabled={testing || !mail.configured}>
              {testing ? 'Sending…' : 'Send me a test e-mail'}
            </button>
          </div>
        )}
        <p><a className="btn" href="/api/bills/export.csv">Download my bills (CSV)</a></p>
      </section>
      <section className="card narrow danger">
        <h2>Delete account</h2>
        <p>Removes your profile, every bill and every uploaded file.</p>
        <button className="btn bad" onClick={erase}>Delete my account</button>
      </section>
    </>
  );
}
