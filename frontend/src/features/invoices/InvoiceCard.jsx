import { useEffect, useState } from "react";
import { api } from "../../api.js";
import IssueList from "./IssueList.jsx";

const FIELD_LABELS = {
  invoice_number: "Invoice number", invoice_date: "Invoice date", supplier: "Supplier", customer: "Customer",
  subtotal: "Subtotal", tax: "Tax", total: "Total", currency: "Currency",
};



const FIELD_KEYS = Object.keys(FIELD_LABELS);
const ITEM_ATTRS = ["description", "quantity", "unit_price", "amount"];

export default function InvoiceCard({ inv, onChanged }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [approval, setApproval] = useState(null);
  const [poMatch, setPoMatch] = useState(null);

  useEffect(() => {
    api.getApproval(inv.id).then(setApproval).catch(() => {});
  }, [inv.id, inv.correction_count]);

  const setApprovalStatus = async (status) => {
    setBusy(true); setErr(null);
    try {
      setApproval(await api.updateApproval(inv.id, { status, note: "Updated from invoice review" }));
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  };

  const checkPo = async () => {
    setBusy(true); setErr(null);
    try { setPoMatch(await api.matchPurchaseOrder(inv.id)); }
    catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  };

  const start = () => {
    setDraft({
      fields: Object.fromEntries(FIELD_KEYS.map((k) => [k, inv.fields[k].value ?? ""])),
      items: inv.line_items.map((li) => ({ idx: li.idx, description: li.description ?? "", quantity: li.quantity ?? "", unit_price: li.unit_price ?? "", amount: li.amount ?? "", delete: false })),
      added: [],
    });
    setNote(""); setErr(null); setEditing(true);
  };

  const save = async () => {
    const fields = {};
    for (const k of FIELD_KEYS) if ((inv.fields[k].value ?? "") !== draft.fields[k].trim()) fields[k] = draft.fields[k].trim() || null;
    const line_items = [];
    for (const it of draft.items) {
      const orig = inv.line_items.find((x) => x.idx === it.idx);
      if (it.delete) { line_items.push({ idx: it.idx, delete: true }); continue; }
      const ch = {};
      for (const a of ITEM_ATTRS) if ((orig[a] ?? "") !== it[a].trim()) ch[a] = it[a].trim() || null;
      if (Object.keys(ch).length) line_items.push({ idx: it.idx, ...ch });
    }
    for (const n of draft.added) if (ITEM_ATTRS.some((a) => n[a].trim())) line_items.push(Object.fromEntries(ITEM_ATTRS.filter((a) => n[a].trim()).map((a) => [a, n[a].trim()])));
    if (!Object.keys(fields).length && !line_items.length) { setEditing(false); return; }
    setBusy(true); setErr(null);
    try { await api.patchInvoice(inv.id, { fields, line_items, note: note.trim() || null }); setEditing(false); await onChanged(); }
    catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  const resolve = async (issueId, resolved) => {
    setBusy(true); setErr(null);
    try { await api.resolveIssue(issueId, resolved); await onChanged(); } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  const setF = (k, v) => setDraft((d) => ({ ...d, fields: { ...d.fields, [k]: v } }));
  const setItem = (i, a, v) => setDraft((d) => ({ ...d, items: d.items.map((x, j) => (j === i ? { ...x, [a]: v } : x)) }));
  const setAdded = (i, a, v) => setDraft((d) => ({ ...d, added: d.added.map((x, j) => (j === i ? { ...x, [a]: v } : x)) }));

  return (
    <article className="invoice">
      <div className="invhead">
        <span className="actions">
          {inv.extractor?.includes("gemini") && <span className="chip warn">AI-assisted fields — verify</span>}
          {!editing && <button className="secondary" onClick={() => api.downloadCorrectedPdf(inv.id).catch((e) => setErr(e.message))}>Download corrected PDF</button>}
          {!editing && <button className="secondary" onClick={() => api.downloadCorrectedXlsx(inv.id).catch((e) => setErr(e.message))}>Download corrected Excel</button>}
        </span>
                <h3>Invoice {inv.seq}{inv.page_start !== inv.page_end ? ` (pages ${inv.page_start}–${inv.page_end})` : ""}
          {inv.correction_count > 0 && <span className="chip busy" title="Number of audited manual changes"> {inv.correction_count} correction(s)</span>}</h3>
        {!editing && <button onClick={start}>Edit</button>}
      </div>
      {err && <p className="err" role="alert">{err}</p>}
      <IssueList issues={inv.issues} onResolve={resolve} busy={busy} />
      <div className="editbar">
        <span className="chip">Approval: {approval?.status || "pending_review"}</span>
        <button className="secondary" disabled={busy} onClick={() => setApprovalStatus("approved")}>Approve</button>
        <button className="secondary" disabled={busy} onClick={() => setApprovalStatus("rejected")}>Reject</button>
        <button className="secondary" disabled={busy} onClick={checkPo}>Check PO match</button>
      </div>
      {poMatch && <p className="banner busy">Purchase-order match: <strong>{poMatch.status}</strong> · {poMatch.matches.length} candidate(s)</p>}
      <table className="fields">
        <thead><tr><th>Field</th><th>Value</th><th title="Heuristic rule confidence, not a calibrated probability">Confidence</th><th>Source text</th></tr></thead>
        <tbody>
          {FIELD_KEYS.map((k) => {
            const f = inv.fields[k];
            return (
              <tr key={k} className={f.value == null && !editing ? "missing" : ""}>
                <th scope="row"><label htmlFor={`f-${inv.id}-${k}`}>{FIELD_LABELS[k]}</label></th>
                <td className={editing ? "" : "num"}>
                  {editing ? <input id={`f-${inv.id}-${k}`} value={draft.fields[k]} onChange={(e) => setF(k, e.target.value)} />
                    : (f.value ?? <em>not found</em>)}
                </td>
                <td className="num">{f.value != null ? `${Math.round(f.confidence * 100)}%` : "—"}</td>
                <td className="ev">{f.source === "manual" ? <em>corrected manually{f.original_value != null ? ` (was ${f.original_value})` : ""}</em> : <>{f.evidence ?? ""}{f.note ? <em> — {f.note}</em> : null}{f.ocr_confidence != null ? <em className={f.ocr_confidence < 60 ? "lowocr" : ""}> · OCR word confidence {Math.round(f.ocr_confidence)}%</em> : null}</>}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <h4>Line items</h4>
      {(editing ? draft.items.length + draft.added.length : inv.line_items.length) ? (
        <table className="items">
          <thead><tr><th>Description</th><th>Qty</th><th>Unit price</th><th>Amount</th>{editing && <th />}</tr></thead>
          <tbody>
            {!editing && inv.line_items.map((li) => (
              <tr key={li.idx} title={li.evidence}><td>{li.description}</td><td className="num">{li.quantity ?? <em>unreadable</em>}</td><td className="num">{li.unit_price}</td><td className="num">{li.amount}</td></tr>
            ))}
            {editing && draft.items.map((it, i) => (
              <tr key={it.idx} className={it.delete ? "deleted" : ""}>
                {ITEM_ATTRS.map((a) => <td key={a}><input aria-label={`${a} of line ${it.idx}`} value={it[a]} disabled={it.delete} onChange={(e) => setItem(i, a, e.target.value)} /></td>)}
                <td><button className="link" onClick={() => setItem(i, "delete", !it.delete)}>{it.delete ? "Undo" : "Delete"}</button></td>
              </tr>
            ))}
            {editing && draft.added.map((it, i) => (
              <tr key={`n${i}`}>
                {ITEM_ATTRS.map((a) => <td key={a}><input aria-label={`new ${a} ${i + 1}`} value={it[a]} onChange={(e) => setAdded(i, a, e.target.value)} /></td>)}
                <td><button className="link" onClick={() => setDraft((d) => ({ ...d, added: d.added.filter((_, j) => j !== i) }))}>Remove</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : <p className="empty">No line items.</p>}
      {editing && (
        <div className="editbar">
          <button className="secondary" onClick={() => setDraft((d) => ({ ...d, added: [...d.added, { description: "", quantity: "", unit_price: "", amount: "" }] }))}>Add line item</button>
          <input className="note" placeholder="Reason for correction (optional)" value={note} onChange={(e) => setNote(e.target.value)} aria-label="Correction note" />
          <button onClick={save} disabled={busy}>{busy ? "Saving…" : "Save corrections"}</button>
          <button className="secondary" onClick={() => setEditing(false)} disabled={busy}>Cancel</button>
        </div>
      )}
    </article>
  );
}
