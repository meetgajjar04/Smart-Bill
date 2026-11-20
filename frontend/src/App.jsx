import { useEffect, useState } from 'react';
import { BrowserRouter, Navigate, NavLink, Route, Routes } from 'react-router-dom';
import { api } from './api';
import { clearHistory } from './chatHistory.js';
import { ToastProvider, useToast } from './ui.jsx';
import Auth from './pages/Auth.jsx';
import Dashboard from './pages/Dashboard.jsx';
import Upload from './pages/Upload.jsx';
import Bills from './pages/Bills.jsx';
import Insights from './pages/Insights.jsx';
import AskAI from './pages/AskAI.jsx';
import Settings from './pages/Settings.jsx';
import Home from './pages/Home.jsx';

function Shell({ user, setUser }) {
  const toast = useToast();
  const logout = async () => {
    await api.logout().catch(() => {});
    clearHistory(user.id);
    setUser(null);
  };
  // Expired sessions end immediately, whether detected by a request or the poll.
  useEffect(() => {
    const expired = () => {
      toast('Your session expired. Please sign in again.', 'bad');
      setUser(null);
    };
    window.addEventListener('smartbill:unauthorized', expired);
    const t = setInterval(() => api.me().catch((e) => e.status === 401 && expired()), 5 * 60 * 1000);
    return () => { clearInterval(t); window.removeEventListener('smartbill:unauthorized', expired); };
  }, [toast, setUser]);
  const links = [['/', 'Dashboard'], ['/upload', 'Add a bill'], ['/bills', 'All bills'], ['/insights', 'Insights'], ['/ask', 'Ask AI'], ['/home', 'My home'], ['/settings', 'Settings']];
  return (
    <div className="shell">
      <aside className="side">
        <div className="brand"><span className="dial" aria-hidden="true" />Smart Bill AI</div>
        <nav aria-label="Main">
          {links.map(([to, label]) => <NavLink key={to} to={to} end={to === '/'}>{label}</NavLink>)}
        </nav>
        <div className="who">
          <span>{user.name}</span>
          <button className="link" onClick={logout}>Sign out</button>
        </div>
      </aside>
      <main id="main">
        <Routes>
          <Route path="/" element={<Dashboard user={user} />} />
          <Route path="/upload" element={<Upload />} />
          <Route path="/bills" element={<Bills />} />
          <Route path="/insights" element={<Insights />} />
          <Route path="/ask" element={<AskAI user={user} />} />
          <Route path="/home" element={<Home />} />
          <Route path="/settings" element={<Settings user={user} setUser={setUser} />} />
          <Route path="*" element={<Navigate to="/" />} />
        </Routes>
      </main>
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState(undefined);
  const [bootError, setBootError] = useState(false);
  const load = () => {
    setBootError(false);
    api.me().then(setUser).catch((e) => {
      if (e.status === 401) setUser(null);
      else setBootError(true);   // a network hiccup is not a sign-out
    });
  };
  useEffect(load, []);
  if (user === undefined) {
    return bootError
      ? <div className="boot">Could not reach Smart Bill AI. <button className="link" onClick={load}>Retry</button></div>
      : <div className="boot">Loading…</div>;
  }
  return (
    <ToastProvider>
      {user ? <BrowserRouter><Shell user={user} setUser={setUser} /></BrowserRouter> : <Auth onAuth={setUser} />}
    </ToastProvider>
  );
}
