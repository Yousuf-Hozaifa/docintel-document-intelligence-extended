import { useState } from "react";
import { api, ApiError, safeSnippet } from "../../api.js";

export default function Search({ onOpen }) {
  const [q, setQ] = useState("");
  const [mode, setMode] = useState("keyword");
  const [res, setRes] = useState(null);
  const [err, setErr] = useState(null);

  const run = async (e) => {
    e.preventDefault();
    try {
      const r = mode === "keyword"
        ? await api.search(q.trim() ? { q } : {})
        : await api.semanticSearch({ q: q.trim(), mode: "hybrid" });
      setRes(r.results);
      setErr(null);
    } catch (x) {
      setRes(null);
      setErr(
        x instanceof ApiError && x.status === 503
          ? "Search by meaning is not enabled on this server (start it with DOCINTEL_SEMANTIC=glove)."
          : x.message
      );
    }
  };

  return (
    <section aria-label="Search documents" className="search">
      <form onSubmit={run}>
        <select
          value={mode}
          onChange={(e) => {
            setMode(e.target.value);
            setRes(null);
            setErr(null);
          }}
          aria-label="Search mode"
        >
          <option value="keyword">Keywords</option>
          <option value="hybrid">Meaning + keywords</option>
        </select>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={
            mode === "keyword"
              ? "Search text, supplier, invoice number…"
              : "Describe what you are looking for…"
          }
          aria-label="Search query"
          required={mode !== "keyword"}
        />
        <button type="submit">Search</button>
      </form>
      {err && <p className="err" role="alert">{err}</p>}
      {res && (
        <ul className="hits">
          {res.length === 0 && (
            <li className="empty">No matching documents.</li>
          )}
          {res.map((r) => (
            <li key={`${r.document_id}-${r.invoice_id ?? "x"}`}>
              <button onClick={() => onOpen(r.document_id)}>
                <strong>{r.filename}</strong>{" "}
                {r.invoice_number && <span>{r.invoice_number}</span>}{" "}
                {r.supplier && <span>{r.supplier}</span>}
                {r.total && (
                  <span className="num">{r.total} {r.currency}</span>
                )}
                {r.sources && r.sources.map((s) => (
                  <span
                    key={s}
                    className={`chip ${s === "semantic" ? "busy" : "ok"}`}
                    title={s === "semantic" ? "matched by meaning" : "matched by words"}
                  >
                    {s}
                  </span>
                ))}
              </button>
              {r.snippets?.map((s, i) => (
                <p
                  key={i}
                  className="snippet"
                  dangerouslySetInnerHTML={{
                    __html: `p.${s.page_no}: ${safeSnippet(s.snippet)}`
                  }}
                />
              ))}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
