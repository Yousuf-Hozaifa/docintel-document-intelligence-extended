import { useCallback, useEffect, useState } from "react";
import { api, authToken, ApiError } from "./api.js";
import AuthScreen from "./features/auth/AuthScreen.jsx";
import AdminPanel from "./features/admin/AdminPanel.jsx";
import { ACTIVE } from "./components/documents/DocumentStatus.jsx";
import Search from "./features/search/Search.jsx";
import Uploader from "./features/upload/Uploader.jsx";
import DocList from "./features/documents/DocList.jsx";
import Inspector from "./features/documents/Inspector.jsx";
import BusinessDashboard from "./features/business/BusinessDashboard.jsx";

function Workspace({ user, onLogout }) {
  const [docs, setDocs] = useState([]);
  const [selected, setSelected] = useState(null);
  const [error, setError] = useState(null);

  const [ocr, setOcr] = useState(null);
  const [llm, setLlm] = useState(null);
  useEffect(() => { api.health().then((h) => { setOcr(h.ocr); setLlm(h.llm); }).catch(() => {}); }, []);

  const refresh = useCallback(async () => {
    try { setDocs((await api.listDocuments()).documents); setError(null); }
    catch (e) {
      if (e instanceof ApiError && e.status === 401) { onLogout(); setError(null); }
      else setError(`Cannot reach the API (${e.message}). Is the backend running on port 8000?`);
    }
  }, [onLogout]);
  const retryFailed = async () => { try { await api.retryFailed(); await refresh(); } catch (e) { setError(e.message); } };

  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    if (!docs.some((d) => ACTIVE.has(d.status))) return;
    const t = setInterval(refresh, 1500);
    return () => clearInterval(t);
  }, [docs, refresh]);

  return (
    <div className="app">
      <header className="top"><h1>DocIntel</h1><Search onOpen={setSelected} /><button className="secondary" onClick={() => api.exportInvoicesXlsx().catch((e) => setError(e.message))}>Export Excel</button><button className="secondary" onClick={() => api.exportInvoicesCsv().catch((e) => setError(e.message))}>Export CSV</button><span className="account-label">{user.email}</span><button className="secondary" onClick={onLogout}>Sign out</button></header>
      {error && <p className="banner bad">{error}</p>}
      {ocr && !ocr.available && <p className="banner warn" role="alert"><strong>OCR is unavailable:</strong> scanned PDFs and images cannot be processed (digital PDFs, spreadsheets and CSV still work). {ocr.hint}</p>}
      {llm?.enabled && !llm.available && <p className="banner warn" role="status">AI enrichment is enabled but not configured. Set the Gemini API key on the backend; deterministic extraction remains available.</p>}
      {llm?.available && <p className="banner busy" role="status">Gemini AI enrichment is enabled ({llm.model}). AI suggestions are marked for review.</p>}
      {user.role === "admin" && <AdminPanel />}
      <BusinessDashboard />
      <main>
        <aside><Uploader onUploaded={refresh} />
          {docs.some((d) => d.status === "failed") && <button className="secondary wide" onClick={retryFailed}>Retry all failed ({docs.filter((d) => d.status === "failed").length})</button>}
          <DocList docs={docs} selected={selected} onSelect={setSelected} /></aside>
        <section className="detail">{selected ? <Inspector key={selected} id={selected} onChanged={refresh} onDeleted={() => { setSelected(null); refresh(); }} /> : <p className="empty">Select a document to inspect its extracted fields.</p>}</section>
      </main>
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState(null);
  const [checking, setChecking] = useState(true);
  const signOut = useCallback(async () => {
    try { if (authToken.get()) await api.logout(); } catch {}
    authToken.set("");
    setUser(null);
  }, []);

  useEffect(() => {
    if (!authToken.get()) { setChecking(false); return; }
    api.me().then(setUser).catch(() => authToken.set("")).finally(() => setChecking(false));
  }, []);

  if (checking) return <p className="empty auth-loading">Loading DocIntel…</p>;
  if (!user) return <AuthScreen onAuthenticated={setUser} />;
  return <Workspace user={user} onLogout={signOut} />;
}
