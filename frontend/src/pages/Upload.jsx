import { useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api';
import { BILL_ACCEPT, BULK_ACCEPT, fileError } from '../files.js';
import BillForm from '../components/BillForm.jsx';
import { inr, useToast } from '../ui.jsx';

export default function Upload() {
  const [draft, setDraft] = useState(null);   // ExtractOut
  const [manual, setManual] = useState(false);
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkPreview, setBulkPreview] = useState(null);   // BulkPreviewOut
  const [bulkFile, setBulkFile] = useState(null);
  const [bulk, setBulk] = useState(null);   // BulkImportOut
  const input = useRef();
  const bulkInput = useRef();
  const nav = useNavigate();
  const toast = useToast();

  const pick = async (file) => {
    if (!file) return;
    const problem = fileError(file, 'bill');
    if (problem) return toast(problem, 'bad');
    setBusy(true);
    try { setDraft(await api.extract(file)); setManual(false); }
    catch (e) { toast(e.message, 'bad'); }
    finally { setBusy(false); if (input.current) input.current.value = ''; }
  };

  const previewBulk = async (file) => {
    if (!file) return;
    const problem = fileError(file, 'bulk');
    if (problem) return toast(problem, 'bad');
    setBulkBusy(true); setBulk(null); setBulkPreview(null); setBulkFile(null);
    try {
      const result = await api.bulkPreview(file);
      setBulkPreview(result);
      setBulkFile(file);
    } catch (e) { toast(e.message, 'bad'); }
    finally {
      setBulkBusy(false);
      if (bulkInput.current) bulkInput.current.value = '';
    }
  };

  const confirmBulk = async () => {
    if (!bulkPreview || !bulkFile) return;
    setBulkBusy(true);
    try {
      const result = await api.bulkImport(bulkFile, bulkPreview.token);
      setBulk(result);
      setBulkPreview(null);
      setBulkFile(null);
      toast(`${result.created} bill${result.created === 1 ? '' : 's'} imported.`);
    } catch (e) { toast(e.message, 'bad'); }
    finally { setBulkBusy(false); }
  };

  const cancelBulk = () => { setBulkPreview(null); setBulkFile(null); setBulk(null); };

  const done = (msg) => { toast(msg); nav('/'); };
  const confirm = async (body) => {
    try { await api.confirmBill(draft.bill.id, body); done('Bill saved. Analysis is running.'); } catch (e) { toast(e.message, 'bad'); }
  };
  const discard = async () => { await api.deleteBill(draft.bill.id).catch(() => {}); setDraft(null); };
  const create = async (body) => {
    try { await api.createBill(body); done('Bill saved. Analysis is running.'); } catch (e) { toast(e.message, 'bad'); }
  };

  return (
    <>
      <header className="page-head"><h1>Add a bill</h1><p>Upload a photo or PDF. We read it for you — you check it before it's saved.</p></header>

      {!draft && !manual && (
        <div className={`drop ${drag ? 'over' : ''}`} role="button" tabIndex={0}
             onClick={() => input.current.click()}
             onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') { e.preventDefault(); input.current.click(); } }}
             onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
             onDrop={(e) => { e.preventDefault(); setDrag(false); pick(e.dataTransfer.files[0]); }}>
          <input ref={input} type="file" hidden accept={BILL_ACCEPT} onChange={(e) => pick(e.target.files[0])} />
          {busy ? <strong>Reading your bill…</strong> : <><strong>Drop a bill here, or click to choose</strong><span>PDF, JPG or PNG · up to 15 MB</span></>}
        </div>
      )}
      {!draft && !manual && <p className="center"><button className="link" onClick={() => setManual(true)}>Enter details by hand instead</button></p>}

      {!draft && !manual && (
        <section className="card bulk-card">
          <h2>Add many bills from Excel</h2>
          <p className="muted small">Download the template, add one row per bill, then upload it. We show every row first — nothing is saved until you confirm.</p>
          <div className="actions bulk-actions">
            <a className="btn" href="/api/bills/bulk-template">Download Excel template</a>
            <button className="btn" onClick={() => bulkInput.current.click()} disabled={bulkBusy}>
              {bulkBusy && !bulkPreview ? 'Reading file…' : 'Upload Excel or CSV'}
            </button>
            <input ref={bulkInput} type="file" hidden accept={BULK_ACCEPT} onChange={(e) => previewBulk(e.target.files[0])} />
          </div>
          <p className="muted small">CSV works too. Columns: category, provider, account_number, bill_date, due_date, amount, units, notes.</p>

          {bulkPreview && (
            <div className="bulk-preview">
              <div className="bulk-preview-head">
                <div>
                  <strong>{bulkPreview.total} row{bulkPreview.total === 1 ? '' : 's'} to review</strong>
                  <p className="muted small">
                    {bulkPreview.ready} will be imported
                    {bulkPreview.duplicates > 0 ? ` · ${bulkPreview.duplicates} duplicate${bulkPreview.duplicates === 1 ? '' : 's'} will be skipped` : ''}
                    {bulkPreview.invalid > 0 ? ` · ${bulkPreview.invalid} row${bulkPreview.invalid === 1 ? '' : 's'} need${bulkPreview.invalid === 1 ? 's' : ''} fixing` : ''}.
                    Nothing has been saved yet.
                  </p>
                </div>
                <span className="badge warn">Not imported yet</span>
              </div>
              <div className="table-wrap bulk-preview-wrap">
                <table className="bulk-table">
                  <thead>
                    <tr>
                      <th>Row</th><th>Status</th><th>Category</th><th>Provider</th><th>Account</th>
                      <th>Bill date</th><th>Due date</th><th className="r">Amount</th><th className="r">Units</th><th>Notes</th>
                    </tr>
                  </thead>
                  <tbody>
                    {bulkPreview.rows.map((r) => (
                      <tr key={r.row} className={`bulk-row ${r.status}`}>
                        <td>{r.row}</td>
                        <td>
                          <span className={`badge ${r.status === 'ready' ? 'ok' : r.status === 'duplicate' ? 'muted' : 'bad'}`}>
                            {r.status === 'ready' ? 'Ready' : r.status === 'duplicate' ? 'Duplicate' : 'Fix'}
                          </span>
                          {r.message && <span className="bulk-msg">{r.message}</span>}
                        </td>
                        <td>{r.category || '—'}</td>
                        <td>{r.provider || '—'}</td>
                        <td>{r.account_number || '—'}</td>
                        <td>{r.bill_date || '—'}</td>
                        <td>{r.due_date || '—'}</td>
                        <td className="r num">{r.amount ? inr(r.amount) : '—'}</td>
                        <td className="r num">{r.units || '—'}</td>
                        <td>{r.notes || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="actions bulk-confirm">
                <button className="btn primary" onClick={confirmBulk} disabled={bulkBusy || bulkPreview.ready === 0}>
                  {bulkBusy ? 'Importing…' : `Confirm & import ${bulkPreview.ready} bill${bulkPreview.ready === 1 ? '' : 's'}`}
                </button>
                <button className="btn" onClick={cancelBulk} disabled={bulkBusy}>Cancel</button>
                {bulkPreview.ready === 0 && <span className="muted small">No valid rows to import. Fix the file and upload it again.</span>}
              </div>
            </div>
          )}

          {bulk && (
            <div className="bulk-result">
              <p className={`note ${bulk.created > 0 ? '' : 'warn'}`}>
                Read {bulk.total} row{bulk.total === 1 ? '' : 's'} · saved {bulk.created}
                {bulk.skipped > 0 ? ` · skipped ${bulk.skipped} duplicate${bulk.skipped === 1 ? '' : 's'}` : ''}
                {bulk.errors.length > 0 ? ` · ${bulk.errors.length} row${bulk.errors.length === 1 ? '' : 's'} need attention` : ''}.
              </p>
              {bulk.errors.length > 0 && (
                <details className="raw-text" open>
                  <summary>Rows that were not imported</summary>
                  <ul className="ai-list">
                    {bulk.errors.map((e) => <li key={e.row}><b>Row {e.row}:</b> {e.message}</li>)}
                  </ul>
                  <p className="muted small">Fix these rows in the same file and upload it again. Rows already saved are skipped as duplicates.</p>
                </details>
              )}
            </div>
          )}
        </section>
      )}

      {manual && <section className="card"><h2>Enter bill details</h2>
        <BillForm submitLabel="Save bill" onSubmit={create} onCancel={() => setManual(false)} /></section>}

      {draft && (
        <div className="split">
          <section className="card preview">
            <h2>Your upload</h2>
            {draft.bill.file_mime === 'application/pdf'
              ? <a className="btn" href={`/api/bills/${draft.bill.id}/file`} target="_blank" rel="noreferrer">Open PDF</a>
              : <img alt="Uploaded bill" src={`/api/bills/${draft.bill.id}/file`} />}
          </section>
          <section className="card">
            <h2>Check the details</h2>
            <div className="meter" aria-label={`Reading confidence ${Math.round(draft.confidence * 100)}%`}>
              <i style={{ width: `${draft.confidence * 100}%` }} /><span>{Math.round(draft.confidence * 100)}% read automatically</span>
            </div>
            {draft.warnings.map((w) => <p key={w} className="note warn">{w}</p>)}
            {draft.ai_used && draft.ai && (
              <div className="ai-extract">
                <p className="ai-tag">✨ Read with Smart Bill AI{draft.ai.summary ? ` — ${draft.ai.summary}` : ''}</p>
                {draft.ai.line_items?.length > 0 && (
                  <details className="raw-text" open>
                    <summary>Charges on this bill ({draft.ai.line_items.length})</summary>
                    <ul className="ai-list charges">
                      {draft.ai.line_items.map((it) => (
                        <li key={it.label}><span>{it.label}</span><b className="num">{inr(it.amount)}</b></li>
                      ))}
                    </ul>
                  </details>
                )}
              </div>
            )}
            {draft.raw_text && (
              <details className="raw-text">
                <summary>What we read (raw OCR text)</summary>
                <pre>{draft.raw_text}</pre>
              </details>
            )}
            <BillForm initial={draft.bill} flagged={draft.missing} submitLabel="Confirm & save" onSubmit={confirm} onCancel={discard} />
          </section>
        </div>
      )}
    </>
  );
}
