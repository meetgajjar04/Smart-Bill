// Chat history is kept per account so a shared browser cannot show one user's
// bill amounts to the next. Only the last few turns are sent to the model.

const PREFIX = 'smartbill.chat.history.v1.';
export const MAX_STORED = 30;
export const MAX_CONTEXT = 12;
const MAX_TURN_CHARS = 1200;  // matches ChatTurn.content on the backend

const keyFor = (userId) => `${PREFIX}${userId ?? 'anon'}`;

export function loadHistory(userId) {
  try {
    const saved = JSON.parse(localStorage.getItem(keyFor(userId)) || '[]');
    if (!Array.isArray(saved)) return [];
    return saved
      .filter((m) => m && (m.role === 'user' || m.role === 'assistant')
        && typeof m.content === 'string' && m.content.trim())
      .map((m) => ({ role: m.role, content: m.content.slice(0, MAX_TURN_CHARS) }))
      .slice(-MAX_STORED);
  } catch {
    return [];
  }
}

export function saveHistory(userId, messages) {
  try {
    localStorage.setItem(keyFor(userId), JSON.stringify(messages.slice(-MAX_STORED)));
  } catch {
    // Storage can be full or blocked; chat still works for this session.
  }
}

export function clearHistory(userId) {
  try { localStorage.removeItem(keyFor(userId)); } catch { /* ignore */ }
}

export function clearAllHistories() {
  try {
    Object.keys(localStorage).filter((k) => k.startsWith(PREFIX)).forEach((k) => localStorage.removeItem(k));
  } catch { /* ignore */ }
}

export function contextHistory(messages) {
  return messages.slice(-MAX_CONTEXT).map((m) => ({
    role: m.role,
    content: String(m.content || '').slice(0, MAX_TURN_CHARS),
  })).filter((m) => m.content.trim());
}
