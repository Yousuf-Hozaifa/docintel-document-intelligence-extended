export const ACTIVE = new Set(["queued", "processing"]);

export function StatusChip({ doc }) {
  const label =
    doc.status === "completed" && doc.review_required
      ? "needs review"
      : doc.status;

  const tone =
    doc.status === "failed"
      ? "bad"
      : label === "needs review" || doc.status === "partial"
        ? "warn"
        : doc.status === "completed"
          ? "ok"
          : "busy";

  return <span className={`chip ${tone}`}>{label}</span>;
}

export function Progress({ doc }) {
  const j = doc.job;

  if (!ACTIVE.has(doc.status) || !j) return null;

  const pct = j.progress_total
    ? Math.round((100 * j.progress_done) / j.progress_total)
    : 0;

  return (
    <div
      className="progress"
      role="progressbar"
      aria-valuenow={pct}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={`Processing ${doc.filename}`}
    >
      <div style={{ width: `${pct}%` }} />
      <span>
        {j.progress_total
          ? `${j.progress_done}/${j.progress_total} pages`
          : "waiting"}
        {j.attempts > 1 ? ` · attempt ${j.attempts}` : ""}
      </span>
    </div>
  );
}
