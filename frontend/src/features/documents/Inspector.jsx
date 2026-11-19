import { useCallback, useEffect, useRef, useState } from "react";
import { api, openOriginal } from "../../api.js";
import { ACTIVE, StatusChip } from "../../components/documents/DocumentStatus.jsx";
import InvoiceCard from "../invoices/InvoiceCard.jsx";

export default function Inspector({ id, onChanged, onDeleted }) {
  const [doc, setDoc] = useState(null);
  const [limit, setLimit] = useState(25);
  const [err, setErr] = useState(null);
  const timer = useRef(null);
  const load = useCallback(async () => {
    clearTimeout(timer.current);
    try {
      const d = await api.getDocument(id, limit);
      setDoc(d); setErr(null);
      if (ACTIVE.has(d.status)) timer.current = setTimeout(load, 1500);
    } catch (e) { setErr(e.message); }
  }, [id, limit]);
  useEffect(() => { load(); return () => clearTimeout(timer.current); }, [load]);

  const changed = async () => { await load(); onChanged(); };
  const reprocess = async () => {
    if (!window.confirm("Reprocess this document? Results and manual corrections on it will be replaced.")) return;
    try { await api.reprocess(id); await changed(); } catch (e) { setErr(e.message); }
  };

  const remove = async () => {
    if (!window.confirm("Permanently delete this document, its extracted data and the stored file?")) return;
    try { await api.deleteDocument(id); onDeleted(); } catch (e) { setErr(e.message); }
  };

  if (err && !doc) return <p className="err">Could not load document: {err}</p>;
  if (!doc) return <p className="empty">Loading…</p>;
  return (
    <div className="inspector">
      <header>
        <h2>{doc.filename}</h2>
        <span className="actions"><StatusChip doc={doc} />
          <button className="secondary" onClick={() => openOriginal(id, doc.filename).catch((e) => setErr(e.message))}>Open original</button>
          {!ACTIVE.has(doc.status) && <button className="secondary" onClick={reprocess}>Reprocess</button>}
          {!ACTIVE.has(doc.status) && <button className="secondary danger" onClick={remove}>Delete</button>}</span>
      </header>
      {err && <p className="err" role="alert">{err}</p>}
      {doc.status === "failed" && <p className="banner bad">Processing failed ({doc.error_code}): {doc.error_message}</p>}
      {doc.status === "partial" && <p className="banner warn">{doc.pages_failed} page(s) could not be processed. Results cover the remaining pages.</p>}
      {ACTIVE.has(doc.status) && <p className="banner busy">Processing… {doc.job?.progress_done ?? 0}/{doc.job?.progress_total ?? "?"} pages</p>}
      {doc.invoice_total > doc.invoices.length && (
        <p className="banner busy">Showing {doc.invoices.length} of {doc.invoice_total} invoices in this document.{" "}
          <button className="link" onClick={() => setLimit((l) => Math.min(l + 25, 500))}>Show 25 more</button></p>
      )}
      {doc.invoices.map((inv) => <InvoiceCard key={`${inv.id}-${inv.correction_count}-${inv.issues.length}-${inv.needs_review}`} inv={inv} onChanged={changed} />)}
      <details>
        <summary>Processing history</summary>
        <ol className="history">{doc.history.map((h, i) => <li key={i}><time>{h.ts}</time> {h.event} {h.detail && <small>{h.detail}</small>}</li>)}</ol>
      </details>
    </div>
  );
}
