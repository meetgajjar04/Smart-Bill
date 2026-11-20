import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { Chart as ChartJS, ArcElement, BarElement, CategoryScale, Filler, Legend, LineElement, LinearScale, PointElement, Tooltip } from 'chart.js';

ChartJS.register(ArcElement, BarElement, CategoryScale, Filler, Legend, LineElement, LinearScale, PointElement, Tooltip);
ChartJS.defaults.font.family = 'inherit';
ChartJS.defaults.color = '#5b6b78';

export const CATS = ['electricity', 'mobile', 'internet', 'water', 'gas', 'other'];
export const CAT_COLOR = { electricity: '#E8A317', mobile: '#2F6FDE', internet: '#7B4FD6', water: '#1FA6B8', gas: '#E0603A', other: '#8A97A3' };
export const CAT_ICON = { electricity: '⚡', mobile: '📱', internet: '🌐', water: '💧', gas: '🔥', other: '🧾' };

export const inr = (n) => (n == null ? '—' : '₹' + Number(n).toLocaleString('en-IN', { maximumFractionDigits: 0 }));
export const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
export const monthLabel = (ym) => new Date(ym + '-01T00:00').toLocaleString('en-IN', { month: 'short', year: '2-digit' });

export function dueText(iso) {
  const days = Math.round((new Date(iso + 'T00:00') - new Date(new Date().toDateString())) / 864e5);
  if (days < 0) return { text: `${-days} day${days === -1 ? '' : 's'} overdue`, tone: 'bad' };
  if (days === 0) return { text: 'due today', tone: 'warn' };
  if (days <= 3) return { text: `due in ${days} day${days === 1 ? '' : 's'}`, tone: 'warn' };
  return { text: `due ${new Date(iso + 'T00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })}`, tone: 'ok' };
}

export const Badge = ({ tone = 'ok', children }) => <span className={`badge ${tone}`}>{children}</span>;
export const STATE_TONE = { paid: 'ok', unpaid: 'warn', overdue: 'bad', draft: 'muted' };

export const Empty = ({ title, children }) => (
  <div className="empty"><strong>{title}</strong><p>{children}</p></div>
);

const ToastCtx = createContext(() => {});
export const useToast = () => useContext(ToastCtx);
export function ToastProvider({ children }) {
  const [t, setT] = useState(null);
  const timer = useRef();
  const show = useCallback((msg, tone = 'ok') => {
    clearTimeout(timer.current);
    setT({ msg, tone });
    timer.current = setTimeout(() => setT(null), 4500);
  }, []);
  useEffect(() => () => clearTimeout(timer.current), []);
  return (
    <ToastCtx.Provider value={show}>
      {children}
      {t && <div className={`toast ${t.tone}`} role="status" aria-live="polite">{t.msg}</div>}
    </ToastCtx.Provider>
  );
}
