import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { BILL_ACCEPT, fileError } from '../files.js';
import { clearHistory, contextHistory, loadHistory, saveHistory } from '../chatHistory.js';
import BillForm from '../components/BillForm.jsx';
import Markdown from '../components/Markdown.jsx';
import { cap, inr, useToast } from '../ui.jsx';

const SUGGESTIONS = [
  'Where can I save the most money this month?',
  'Why was my electricity bill higher than usual?',
  'Which bills are due soon?',
  'Is a fixed plan or my current tariff better for me?',
  'How can I lower my mobile bill?',
];

/** Free-form Q&A about the user's own bills, answered by Smart Bill AI. */
export default function AskAI({ user }) {
  const userId = user?.id;
  const [messages, setMessages] = useState(() => loadHistory(userId)); // [{ role, content }]
  const [question, setQuestion] = useState('');
  const [busy, setBusy] = useState(false);
  const [chatStatus, setChatStatus] = useState('');
  const [status, setStatus] = useState(null);
  const [draft, setDraft] = useState(null);        // ExtractOut from an upload
  const [uploadBusy, setUploadBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const endRef = useRef();
  const logRef = useRef();
  const boxRef = useRef();
  const fileRef = useRef();
  const abortRef = useRef(null);
  const toast = useToast();

  useEffect(() => { api.aiStatus().then(setStatus).catch(() => {}); }, []);
  useEffect(() => () => abortRef.current?.abort(), []);
  useEffect(() => { saveHistory(userId, messages); }, [messages, userId]);
  useEffect(() => {
    const log = logRef.current;
    if (!log) return;
    // Do not yank the view back while the user is reading earlier messages.
    if (log.scrollHeight - log.scrollTop - log.clientHeight < 160) {
      endRef.current?.scrollIntoView({ behavior: 'auto', block: 'nearest' });
    }
  }, [messages, busy, draft]);

  const ask = async (text) => {
    const q = (text ?? question).trim();
    if (q.length < 2 || busy) return;
    const previous = messages;
    const base = [...previous, { role: 'user', content: q }];
    setMessages([...base, { role: 'assistant', content: '' }]);
    setQuestion('');
    setBusy(true);
    const controller = new AbortController();
    abortRef.current = controller;
    let answer = '';
    try {
      await api.chatStream(q, contextHistory(previous), (delta) => {
        answer += delta;
        setMessages([...base, { role: 'assistant', content: answer }]);
      }, controller.signal);
      setChatStatus('Answer ready.');
    } catch (e) {
      if (e?.name === 'AbortError') { setChatStatus('Answer cancelled.'); return; }
      toast(e.message, 'bad');
      setChatStatus('Answer failed. Please try again.');
      // Keep the question so the user can retry without retyping.
      setMessages(previous);
      setQuestion(q);
    } finally {
      abortRef.current = null;
      setBusy(false);
      boxRef.current?.focus();
    }
  };

  const clearChat = () => {
    if (busy) return;
    setMessages([]);
    clearHistory(userId);
    setChatStatus('Chat cleared.');
    boxRef.current?.focus();
  };

  // Same flow as the Add a bill page: read the upload, let the user check it, then save.
  const pick = async (file) => {
    if (!file) return;
    const problem = fileError(file, 'bill');
    if (problem) { toast(problem, 'bad'); return; }
    const previousId = draft?.bill?.id;
    setUploadBusy(true);
    try {
      const next = await api.extract(file);
      // Remove the old draft only once the new one exists, so a failed upload
      // never costs the user a draft they had not finished reviewing.
      if (previousId && previousId !== next.bill.id) {
        await api.deleteBill(previousId).catch(() => {});
      }
      setDraft(next);
      setChatStatus('Bill ready to review.');
    } catch (e) {
      toast(e.message, 'bad');
    } finally {
      setUploadBusy(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  const confirmDraft = async (body) => {
    try {
      await api.confirmBill(draft.bill.id, body);
      const provider = body.provider ? ` from **${body.provider}**` : '';
      const note = `Saved your **${cap(body.category)}** bill${provider} for **${inr(body.amount)}**, due **${body.due_date}**. Ask me anything about it.`;
      setMessages((prev) => [...prev, { role: 'assistant', content: note }]);
      setDraft(null);
      setChatStatus('Bill saved.');
      toast('Bill saved. Analysis is running.');
    } catch (e) {
      toast(e.message, 'bad');
    }
  };

  const discardDraft = async () => {
    const id = draft?.bill?.id;
    setDraft(null);
    setChatStatus('Draft discarded.');
    if (id) await api.deleteBill(id).catch(() => {});
  };

  const disabled = busy || status?.enabled === false;

  const onDragOver = (e) => {
    if (!e.dataTransfer?.types?.includes('Files')) return;
    e.preventDefault();
    setDrag(true);
  };

  return (
    <>
      <header className="page-head">
        <h1>Ask AI</h1>
        <p>Ask anything about your bills and how to spend less. Answers use your own bill history and never leave this computer. It remembers your last few questions.</p>
      </header>

      {status && !status.enabled && (
        <p className="note warn">Smart Bill AI is disabled. Set <code>OLLAMA_ENABLED=true</code> in <code>.env</code> and restart the backend.</p>
      )}
      {status?.enabled && !status.available && (
        <p className="note warn">Smart Bill AI is not running at <code>{status.url}</code>. Start it with <code>ollama serve</code>, then reload this page.</p>
      )}
      {status?.enabled && status.available && !status.model_ready && (
        <p className="note warn">{status.error}</p>
      )}

      <section
        className={`card chat-card${drag ? ' dragging' : ''}`}
        onDragOver={onDragOver}
        onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setDrag(false); }}
        onDrop={(e) => { e.preventDefault(); setDrag(false); pick(e.dataTransfer.files?.[0]); }}
      >
        <input
          ref={fileRef}
          type="file"
          hidden
          accept={BILL_ACCEPT}
          onChange={(e) => pick(e.target.files?.[0])}
        />

        {uploadBusy && <p className="note chat-upload-note">Reading your bill… this can take a moment.</p>}
        {drag && <p className="note chat-upload-note">Drop your bill here to read it.</p>}

        <p className="sr-only" role="status" aria-live="polite">{chatStatus}</p>
        <div className="chat-log">
          {messages.length === 0 && (
            <div className="chat-empty">
              <strong>How can I help you save?</strong>
              <p className="muted">I can compare your months, explain an unusual bill, list what is due, and suggest practical ways to cut costs. You can also upload a bill here.</p>
              <div className="chat-suggestions">
                {SUGGESTIONS.map((s) => (
                  <button key={s} className="chip" onClick={() => ask(s)} disabled={disabled}>{s}</button>
                ))}
              </div>
            </div>
          )}
          {messages.map((m, i) => {
            const waiting = busy && m.role === 'assistant' && !m.content;
            return (
              <div key={i} className={`msg ${m.role}${waiting ? ' thinking' : ''}`}>
                <span className="msg-who">{m.role === 'user' ? 'You' : 'Smart Bill AI'}</span>
                {waiting
                  ? <p>Thinking…</p>
                  : m.role === 'assistant'
                    ? <Markdown text={m.content} />
                    : <p>{m.content}</p>}
              </div>
            );
          })}
          <div ref={endRef} />
        </div>

        {draft && (
          <section className="chat-draft" aria-label="Review your uploaded bill">
            <div className="chat-draft-head">
              <div>
                <h2>Check your bill before saving</h2>
                <p className="muted small">We read your upload. Fix anything that looks wrong, then confirm to add it to your bills.</p>
              </div>
              <button type="button" className="btn sm" onClick={discardDraft}>Discard</button>
            </div>

            <div className="chat-draft-body">
              <div className="chat-draft-preview">
                {draft.bill.file_mime === 'application/pdf'
                  ? <a className="btn sm" href={`/api/bills/${draft.bill.id}/file`} target="_blank" rel="noreferrer">Open PDF</a>
                  : (
                    <a href={`/api/bills/${draft.bill.id}/file`} target="_blank" rel="noreferrer">
                      <img alt="Uploaded bill" src={`/api/bills/${draft.bill.id}/file`} />
                    </a>
                  )}
              </div>
              <div className="chat-draft-detail">
                <div className="meter" aria-label={`Reading confidence ${Math.round((draft.confidence || 0) * 100)}%`}>
                  <i style={{ width: `${(draft.confidence || 0) * 100}%` }} />
                  <span>{Math.round((draft.confidence || 0) * 100)}% read automatically</span>
                </div>
                {draft.warnings?.map((w) => <p key={w} className="note warn">{w}</p>)}
                {draft.ai_used && draft.ai && (
                  <p className="ai-tag">✨ Read with Smart Bill AI{draft.ai.summary ? ` — ${draft.ai.summary}` : ''}</p>
                )}
                {draft.ai_used && draft.ai?.line_items?.length > 0 && (
                  <details className="raw-text">
                    <summary>Charges on this bill ({draft.ai.line_items.length})</summary>
                    <ul className="ai-list charges">
                      {draft.ai.line_items.map((it) => (
                        <li key={it.label}><span>{it.label}</span><b className="num">{inr(it.amount)}</b></li>
                      ))}
                    </ul>
                  </details>
                )}
              </div>
            </div>

            <BillForm
              initial={draft.bill}
              flagged={draft.missing}
              submitLabel="Confirm & save"
              onSubmit={confirmDraft}
              onCancel={discardDraft}
            />

            {draft.raw_text && (
              <details className="raw-text">
                <summary>What we read (raw OCR text)</summary>
                <pre>{draft.raw_text}</pre>
              </details>
            )}
          </section>
        )}

        <form className="chat-form" onSubmit={(e) => { e.preventDefault(); ask(); }}>
          <label className="sr-only" htmlFor="chat-question">Your question</label>
          <input id="chat-question" ref={boxRef} autoComplete="off" maxLength={600}
                 placeholder={messages.length ? 'Ask a follow-up…' : 'e.g. Where am I spending the most?'}
                 value={question} onChange={(e) => setQuestion(e.target.value)} disabled={disabled} />
          <button className="btn primary" type="submit" disabled={disabled || question.trim().length < 2}>
            {busy ? 'Answering…' : 'Ask'}
          </button>
        </form>
        <div className="chat-tools">
          <p className="muted small chat-hint">Smart Bill AI uses your saved bills and remembers the last 12 messages. It can be wrong — check important amounts against your bill.</p>
          <div className="chat-actions">
            <button type="button" className="btn sm" onClick={() => fileRef.current?.click()} disabled={uploadBusy}>
              {uploadBusy ? 'Reading bill…' : '📎 Upload bill'}
            </button>
            {messages.length > 0 && (
              <button type="button" className="btn sm" onClick={clearChat} disabled={busy}>New chat</button>
            )}
          </div>
        </div>
      </section>
    </>
  );
}
