import { useCallback, useEffect, useState } from "react";
import { api, apiKey, ApiError } from "./api.js";
import { ACTIVE } from "./components/documents/DocumentStatus.jsx";
import Search from "./features/search/Search.jsx";
import Uploader from "./features/upload/Uploader.jsx";
import DocList from "./features/documents/DocList.jsx";
import Inspector from "./features/documents/Inspector.jsx";

export default function App() {
  const [docs, setDocs] = useState([]);
  const [selected, setSelected] = useState(null);
  const [error, setError] = useState(null);

  const [ocr, setOcr] = useState(null);
  useEffect(() => { api.health().then((h) => setOcr(h.ocr)).catch(() => {}); }, []);
  const [needKey, setNeedKey] = useState(false);
  const [keyInput, setKeyInput] = useState("");

  const refresh = useCallback(async () => {
    try { setDocs((await api.listDocuments()).documents); setError(null); setNeedKey(false); }
    catch (e) {
      if (e instanceof ApiError && e.status === 401) { setNeedKey(true); setError(null); }
      else setError(`Cannot reach the API (${e.message}). Is the backend running on port 8000?`);
    }
  }, []);
  const retryFailed = async () => { try { await api.retryFailed(); await refresh(); } catch (e) { setError(e.message); } };

  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    if (!docs.some((d) => ACTIVE.has(d.status))) return;
    const t = setInterval(refresh, 1500);
    return () => clearInterval(t);
  }, [docs, refresh]);

  return (
    <div className="app">
      <header className="top"><h1>Document Intelligence</h1><Search onOpen={setSelected} /></header>
      {error && <p className="banner bad">{error}</p>}
      {ocr && !ocr.available && <p className="banner warn" role="alert"><strong>OCR is unavailable:</strong> scanned PDFs and images cannot be processed (digital PDFs, spreadsheets and CSV still work). {ocr.hint}</p>}
      {needKey && (
        <form className="banner warn keyform" onSubmit={(e) => { e.preventDefault(); apiKey.set(keyInput.trim()); refresh(); }}>
          <label>The API requires a key: <input type="password" value={keyInput} onChange={(e) => setKeyInput(e.target.value)} aria-label="API key" /></label>
          <button type="submit">Use key</button>
        </form>
      )}
      <main>
        <aside><Uploader onUploaded={refresh} />
          {docs.some((d) => d.status === "failed") && <button className="secondary wide" onClick={retryFailed}>Retry all failed ({docs.filter((d) => d.status === "failed").length})</button>}
          <DocList docs={docs} selected={selected} onSelect={setSelected} /></aside>
        <section className="detail">{selected ? <Inspector key={selected} id={selected} onChanged={refresh} onDeleted={() => { setSelected(null); refresh(); }} /> : <p className="empty">Select a document to inspect its extracted fields.</p>}</section>
      </main>
    </div>
  );
}
