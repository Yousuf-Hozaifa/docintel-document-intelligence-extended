import { useCallback, useEffect, useState } from "react";
import { api } from "../../api.js";

const MAP_KEYS = ["invoice_number", "invoice_date", "supplier", "total", "currency"];

export default function BusinessDashboard() {
  const [data, setData] = useState(null);
  const [suppliers, setSuppliers] = useState([]);
  const [purchaseOrders, setPurchaseOrders] = useState([]);
  const [mappings, setMappings] = useState([]);
  const [googleSheets, setGoogleSheets] = useState(null);
  const [sheetsMessage, setSheetsMessage] = useState("");
  const [error, setError] = useState("");
  const [templateName, setTemplateName] = useState("My accounting export");
  const [columns, setColumns] = useState({
    invoice_number: "Invoice No.",
    invoice_date: "Invoice Date",
    supplier: "Vendor",
    total: "Invoice Amount",
    currency: "Currency",
  });
  const [poNumber, setPoNumber] = useState("");
  const [poSupplier, setPoSupplier] = useState("");
  const [poTotal, setPoTotal] = useState("");
  const [poCurrency, setPoCurrency] = useState("BDT");
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const [summary, supplierData, mappingData, health, poData] = await Promise.all([
        api.dashboard(), api.suppliers(), api.mappings(), api.googleSheetsStatus(), api.purchaseOrders(),
      ]);
      setData(summary);
      setSuppliers(supplierData.suppliers || []);
      setMappings(mappingData.mappings || []);
      setGoogleSheets(health || null);
      setPurchaseOrders(poData.purchase_orders || []);
      setError("");
    } catch (e) { setError(e.message); }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 15000);
    return () => clearInterval(timer);
  }, [load]);

  const saveMapping = async (event) => {
    event.preventDefault();
    const mapping = Object.fromEntries(
      MAP_KEYS.filter((key) => columns[key].trim())
        .map((key) => [columns[key].trim(), key])
    );
    if (!templateName.trim() || !Object.keys(mapping).length) return;
    setSaving(true);
    try {
      await api.createMapping({ name: templateName.trim(), mapping });
      await load();
    } catch (e) { setError(e.message); }
    finally { setSaving(false); }
  };

  const exportSheets = async (mappingId) => {
    setSaving(true); setSheetsMessage("");
    try {
      const result = await api.exportGoogleSheets(mappingId);
      setSheetsMessage("Exported " + result.exported_rows + " invoice row(s) to " + (result.updated_range || "Google Sheets") + ".");
    } catch (e) { setError(e.message); }
    finally { setSaving(false); }
  };

  const savePurchaseOrder = async (event) => {
    event.preventDefault();
    if (!poNumber.trim() || !poSupplier.trim() || !poTotal.trim()) return;
    setSaving(true);
    try {
      await api.createPurchaseOrder({
        po_number: poNumber.trim(),
        supplier_name: poSupplier.trim(),
        currency: poCurrency,
        total: poTotal.trim(),
        line_items: [],
      });
      setPoNumber(""); setPoTotal("");
      await load();
    } catch (e) { setError(e.message); }
    finally { setSaving(false); }
  };

  if (!data && !error) return <section className="business-dashboard"><p>Loading business dashboard…</p></section>;

  const metrics = data?.metrics || {};
  return (
    <section className="business-dashboard">
      <div className="business-heading">
        <div><h2>Accounts Payable Overview</h2><p>Live metrics calculated from this workspace's invoice records.</p></div>
        <button className="secondary" onClick={load}>Refresh</button>
      </div>
      {error && <p className="err" role="alert">{error}</p>}
      <div className="business-kpis">
        <article><span>Documents</span><strong>{metrics.documents ?? 0}</strong></article>
        <article><span>Invoices</span><strong>{metrics.invoices ?? 0}</strong></article>
        <article><span>Needs review</span><strong>{metrics.needs_review ?? 0}</strong></article>
        <article><span>Pending approval</span><strong>{metrics.pending_approval ?? 0}</strong></article>
        <article><span>Approved</span><strong>{metrics.approved ?? 0}</strong></article>
        <article><span>Open exceptions</span><strong>{metrics.invoices_with_open_issues ?? 0}</strong></article>
      </div>
      <div className="business-panels">
        <article className="business-panel">
          <h3>Invoice value by currency</h3>
          {(data?.totals_by_currency || []).length ? (
            <ul>{data.totals_by_currency.map((row) => <li key={row.currency || "unknown"}><strong>{row.currency || "Unspecified"}</strong><span>{row.invoices} invoices · {Number(row.total || 0).toLocaleString(undefined, { maximumFractionDigits: 2 })}</span></li>)}</ul>
          ) : <p className="empty">Upload invoices to populate this summary.</p>}
        </article>
        <article className="business-panel">
          <h3>Supplier directory</h3>
          <p>{data?.supplier_count ?? suppliers.length} suppliers detected or added.</p>
          {suppliers.length ? <ul>{suppliers.slice(0, 6).map((s) => <li key={s.id}><strong>{s.name}</strong><span>{s.invoice_count} invoices</span></li>)}</ul> : <p className="empty">Supplier names will be added automatically as invoices are processed.</p>}
        </article>
      </div>
      <div className="business-panels">
        <article className="business-panel">
          <h3>Smart Data Mapping</h3>
          <p>Save your company's spreadsheet column names once, then reuse them for future invoice exports.</p>
          <p>Google Sheets: <strong>{googleSheets?.configured ? "Configured" : "Not configured"}</strong></p>
          <button className="secondary" disabled={saving || !googleSheets?.configured} onClick={() => exportSheets()}>Export invoice register to Google Sheets</button>
          {sheetsMessage && <p className="banner busy">{sheetsMessage}</p>}
          <form onSubmit={saveMapping} className="business-form">
            <label>Template name<input value={templateName} onChange={(e) => setTemplateName(e.target.value)} /></label>
            {MAP_KEYS.map((key) => <label key={key}>{key.replaceAll("_", " ")} column<input value={columns[key]} onChange={(e) => setColumns((current) => ({ ...current, [key]: e.target.value }))} /></label>)}
            <button disabled={saving}>{saving ? "Saving…" : "Save mapping template"}</button>
          </form>
          {mappings.length > 0 && <ul className="mapping-list">{mappings.map((item) => <li key={item.id}><strong>{item.name}</strong><span><button className="secondary" onClick={() => api.exportInvoicesXlsx(item.id).catch((e) => setError(e.message))}>Export mapped Excel</button> <button className="secondary" onClick={() => api.exportInvoicesCsv(item.id).catch((e) => setError(e.message))}>Export mapped CSV</button> <button className="secondary" disabled={!googleSheets?.configured || saving} onClick={() => exportSheets(item.id)}>Send to Google Sheets</button></span></li>)}</ul>}
        </article>
        <article className="business-panel">
          <h3>Purchase-order matching</h3>
          <p>Add a demo purchase order. Matching checks supplier, currency and invoice total.</p>
          <form onSubmit={savePurchaseOrder} className="business-form">
            <label>PO number<input value={poNumber} onChange={(e) => setPoNumber(e.target.value)} placeholder="PO-2026-001" /></label>
            <label>Supplier<input value={poSupplier} onChange={(e) => setPoSupplier(e.target.value)} placeholder="Supplier name from invoice" /></label>
            <label>PO total<input value={poTotal} onChange={(e) => setPoTotal(e.target.value)} placeholder="1000.00" /></label>
            <label>Currency<select value={poCurrency} onChange={(e) => setPoCurrency(e.target.value)}><option>BDT</option><option>USD</option><option>EUR</option><option>GBP</option></select></label>
            <button disabled={saving}>{saving ? "Saving…" : "Add sample purchase order"}</button>
          </form>
          {purchaseOrders.length > 0 && <ul>{purchaseOrders.slice(0, 5).map((po) => <li key={po.id}><strong>{po.po_number}</strong><span>{po.supplier_name} · {po.currency || ""} {po.total || ""}</span></li>)}</ul>}
        </article>
      </div>
    </section>
  );
}
