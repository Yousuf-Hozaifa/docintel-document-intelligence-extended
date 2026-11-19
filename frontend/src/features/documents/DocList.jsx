import { StatusChip, Progress } from "../../components/documents/DocumentStatus.jsx";

export default function DocList({ docs, selected, onSelect }) {
  if (!docs.length) return <p className="empty">No documents yet. Upload invoices to begin.</p>;
  return (
    <ul className="doclist">
      {docs.map((d) => (
        <li key={d.id}>
          <button className={`docrow ${selected === d.id ? "sel" : ""}`} onClick={() => onSelect(d.id)}>
            <span className="name">{d.filename}</span>
            <StatusChip doc={d} />
            <span className="meta">{d.file_type || "…"}{d.page_count ? ` · ${d.page_count} p` : ""}{d.invoice_count > 1 ? ` · ${d.invoice_count} invoices` : ""}</span>
            <Progress doc={d} />
            {d.status === "failed" && <span className="err">{d.error_message}</span>}
          </button>
        </li>
      ))}
    </ul>
  );
}
