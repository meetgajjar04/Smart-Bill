// All calls are same-origin (/api) so the httpOnly session cookie is attached automatically.
const OFFLINE = 'Cannot reach Smart Bill AI. Check your connection and try again.';

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

function readable(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map((d) => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`.replace(/^: /, '')).join('\n');
  return 'Something went wrong. Please try again.';
}

// The access token is HttpOnly, so the signed double-submit value travels in a
// separate readable cookie. JS echoes it back on every state-changing request.
function authHeaders() {
  const headers = { 'X-Requested-With': 'SmartBillAI' };
  const csrf = document.cookie.split('; ').find((row) => row.startsWith('csrf_token='));
  if (csrf) headers['X-CSRF-Token'] = decodeURIComponent(csrf.slice('csrf_token='.length));
  return headers;
}

async function req(path, { method = 'GET', body, form } = {}) {
  const headers = authHeaders(); // custom header + double-submit token stop cross-site writes
  let payload;
  if (form) payload = form;
  else if (body !== undefined) { headers['Content-Type'] = 'application/json'; payload = JSON.stringify(body); }
  let res;
  try { res = await fetch('/api' + path, { method, headers, body: payload, credentials: 'same-origin' }); }
  catch { throw new ApiError(OFFLINE, 0); }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    // End the session immediately on expiry instead of waiting for the next poll.
    if (res.status === 401 && !path.startsWith('/auth/')) {
      window.dispatchEvent(new CustomEvent('smartbill:unauthorized'));
    }
    throw new ApiError(readable(data?.detail), res.status);
  }
  return data;
}

/** Stream Smart Bill AI's answer, calling onDelta for each piece of text as it arrives. */
async function streamChat(question, history, onDelta, signal) {
  // The backend validates each turn at 1200 characters, whatever the UI stored.
  const turns = (history || []).map((m) => ({
    role: m.role,
    content: String(m.content || '').slice(0, 1200),
  })).filter((m) => m.content.trim());

  let res;
  try {
    res = await fetch('/api/ai/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ question, history: turns }),
      credentials: 'same-origin',
      signal,
    });
  } catch (e) {
    if (e?.name === 'AbortError') throw e;
    throw new ApiError(OFFLINE, 0);
  }

  if (!res.ok) {
    const data = await res.json().catch(() => null);
    if (res.status === 401) window.dispatchEvent(new CustomEvent('smartbill:unauthorized'));
    throw new ApiError(readable(data?.detail), res.status);
  }
  if (!res.body) {
    const data = await res.json().catch(() => null);
    if (!data?.answer) throw new ApiError('Smart Bill AI returned an empty answer.', 502);
    onDelta(data.answer);
    return data.answer;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let answer = '';

  const handle = (line) => {
    if (!line.trim()) return;
    let event;
    try { event = JSON.parse(line); } catch { return; }
    if (event.error) throw new ApiError(event.error, 502);
    if (event.delta) { answer += event.delta; onDelta(event.delta); }
  };

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 1);
        handle(line);
      }
    }
    buffer += decoder.decode();
    handle(buffer);
  } finally {
    reader.cancel().catch(() => {});
  }
  if (!answer.trim()) throw new ApiError('Smart Bill AI returned an empty answer.', 502);
  return answer;
}

export const api = {
  me: () => req('/auth/me'),
  login: (b) => req('/auth/login', { method: 'POST', body: b }),
  register: (b) => req('/auth/register', { method: 'POST', body: b }),
  logout: () => req('/auth/logout', { method: 'POST' }),
  logoutAll: () => req('/auth/logout-all', { method: 'POST' }),
  changePassword: (b) => req('/auth/change-password', { method: 'POST', body: b }),
  updateMe: (b) => req('/auth/me', { method: 'PATCH', body: b }),
  deleteMe: () => req('/auth/me', { method: 'DELETE' }),
  extract: (file) => { const f = new FormData(); f.append('file', file); return req('/bills/extract', { method: 'POST', form: f }); },
  bulkPreview: (file) => { const f = new FormData(); f.append('file', file); return req('/bills/bulk-preview', { method: 'POST', form: f }); },
  bulkImport: (file, token) => { const f = new FormData(); f.append('file', file); f.append('preview_token', token); return req('/bills/bulk-import', { method: 'POST', form: f }); },
  createBill: (b) => req('/bills', { method: 'POST', body: b }),
  confirmBill: (id, b) => req(`/bills/${id}/confirm`, { method: 'POST', body: b }),
  updateBill: (id, b) => req(`/bills/${id}`, { method: 'PUT', body: b }),
  deleteBill: (id) => req(`/bills/${id}`, { method: 'DELETE' }),
  setPaid: (id, paid) => req(`/bills/${id}/payment`, { method: 'PATCH', body: { paid } }),
  bills: (params = {}) => req('/bills?' + new URLSearchParams(Object.entries(params).filter(([, v]) => v))),
  summary: () => req('/analytics/summary'),
  predict: (category) => req('/analytics/predict?category=' + category),
  anomalies: () => req('/analytics/anomalies'),
  recommendations: () => req('/analytics/recommendations'),
  profile: () => req('/profile'),
  saveProfile: (b) => req('/profile', { method: 'PUT', body: b }),
  whatIf: (b) => req('/analytics/what-if', { method: 'POST', body: b }),
  feedback: (id, b) => req(`/bills/${id}/feedback`, { method: 'POST', body: b }),
  alerts: () => req('/alerts'),
  readAlert: (id) => req(`/alerts/${id}/read`, { method: 'POST' }),
  aiStatus: () => req('/ai/status'),
  chatStream: streamChat,
  emailStatus: () => req('/notifications/status'),
  sendTestEmail: () => req('/notifications/test', { method: 'POST' }),
  billAi: (id) => req(`/bills/${id}/ai-analysis`),
  analyzeBill: (id) => req(`/bills/${id}/ai-analysis`, { method: 'POST' }),
  portfolioAi: () => req('/analytics/ai-summary'),
  analyzePortfolio: () => req('/analytics/ai-summary', { method: 'POST' }),
};
