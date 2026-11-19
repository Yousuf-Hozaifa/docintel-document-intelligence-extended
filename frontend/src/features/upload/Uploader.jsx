import { useRef, useState } from "react";
import { api } from "../../api.js";

export default function Uploader({ onUploaded }) {
  const input = useRef(null);
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState([]);
  const [drag, setDrag] = useState(false);

  const send = async (files) => {
    if (!files.length) return;
    setBusy(true);
    try {
      const { results } = await api.upload(files);
      setReport(results);
      onUploaded();
    } catch (e) {
      setReport([{ filename: "Upload", status: "rejected", error: e.message }]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-label="Upload documents">
      <div
        className={`drop ${drag ? "over" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDrag(false);
          send([...e.dataTransfer.files]);
        }}
      >
        <p>Drop invoices here, or</p>
        <button onClick={() => input.current.click()} disabled={busy}>
          {busy ? "Uploading…" : "Choose files"}
        </button>
        <input
          ref={input}
          type="file"
          multiple
          hidden
          accept=".pdf,.jpg,.jpeg,.png,.tif,.tiff,.xlsx,.xls,.csv"
          onChange={(e) => {
            send([...e.target.files]);
            e.target.value = "";
          }}
        />
        <small>PDF, JPG, PNG, TIFF, XLSX, XLS, CSV</small>
      </div>
      {report.length > 0 && (
        <ul className="report" aria-live="polite">
          {report.map((r, i) => (
            <li
              key={i}
              className={
                r.status === "rejected"
                  ? "bad"
                  : r.status === "duplicate"
                    ? "warn"
                    : "ok"
              }
            >
              <strong>{r.filename}</strong>{" "}
              {r.status === "queued" && "queued for processing"}
              {r.status === "duplicate" &&
                `identical file already uploaded (document ${r.duplicate_of})`}
              {r.status === "rejected" && `rejected: ${r.error}`}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
