export default function IssueList({ issues, onResolve, busy }) {
  if (!issues.length) return null;
  return (
    <ul className="issues">
      {issues.map((i) => (
        <li key={i.id} className={`${i.severity} ${i.resolved ? "done" : ""}`}>
          <code>{i.rule_id}</code> {i.message}
          {i.severity !== "info" && (
            <button className="link" disabled={busy} onClick={() => onResolve(i.id, !i.resolved)}>
              {i.resolved ? "Reopen" : "Mark reviewed"}
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}
