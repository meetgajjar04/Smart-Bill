import { useState } from 'react';
import { api } from '../api';

export default function Auth({ onAuth }) {
  const [mode, setMode] = useState('login');
  const [f, setF] = useState({ name: '', email: '', password: '' });
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });

  const submit = async (e) => {
    e.preventDefault(); setErr(''); setBusy(true);
    try {
      onAuth(mode === 'login' ? await api.login({ email: f.email, password: f.password }) : await api.register(f));
    } catch (ex) { setErr(ex.message); } finally { setBusy(false); }
  };

  return (
    <div className="auth">
      <section className="auth-pitch">
        <div className="brand"><span className="dial" aria-hidden="true" />Smart Bill AI</div>
        <h1>Know what your bills will be before they arrive.</h1>
        <p>Photograph a bill. We read it, remind you before it's due, flag unusual jumps, and forecast next month's electricity.</p>
        <div className="slip demo" aria-hidden="true">
          <div><strong>Electricity</strong><small>due in 3 days</small></div><b className="num">₹2,584</b>
        </div>
      </section>
      <form className="auth-card" onSubmit={submit}>
        <h2>{mode === 'login' ? 'Sign in' : 'Create your account'}</h2>
        {mode === 'register' && <label>Your name<input required maxLength={80} value={f.name} onChange={set('name')} autoComplete="name" /></label>}
        <label>Email<input type="email" required value={f.email} onChange={set('email')} autoComplete="email" /></label>
        <label>Password
          <input type="password" required minLength={mode === 'register' ? 10 : 1} value={f.password} onChange={set('password')}
                 autoComplete={mode === 'login' ? 'current-password' : 'new-password'} />
          {mode === 'register' && <small>At least 10 characters, with letters and numbers.</small>}
        </label>
        {err && <p className="error" role="alert">{err}</p>}
        <button className="btn primary" disabled={busy}>{busy ? 'Please wait…' : mode === 'login' ? 'Sign in' : 'Create account'}</button>
        <button type="button" className="link" onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setErr(''); }}>
          {mode === 'login' ? 'New here? Create an account' : 'Already registered? Sign in'}
        </button>
      </form>
    </div>
  );
}
