import { useEffect, useState } from "react";
import { api } from "../../api.js";

export default function AdminPanel() {
  const [overview, setOverview] = useState(null);
  const [users, setUsers] = useState([]);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState(null);

  async function load() {
    try {
      const [summary, result] = await Promise.all([api.adminOverview(), api.adminUsers()]);
      setOverview(summary);
      setUsers(result.users);
      setError("");
    } catch (err) { setError(err.message); }
  }
  useEffect(() => { load(); }, []);

  async function toggle(user) {
    setBusyId(user.id);
    try {
      await api.setUserActive(user.id, !user.is_active);
      await load();
    } catch (err) { setError(err.message); }
    finally { setBusyId(null); }
  }

  return <section className="admin-panel">
    <h2>Administrator dashboard</h2>
    {error && <p className="banner bad">{error}</p>}
    {overview && <div className="admin-stats">
      <div><strong>{overview.users}</strong><span>Total accounts</span></div>
      <div><strong>{overview.active_users}</strong><span>Active accounts</span></div>
      <div><strong>{overview.documents}</strong><span>User documents</span></div>
      <div><strong>{overview.jobs}</strong><span>Processing jobs</span></div>
    </div>}
    <h3>Account management</h3>
    <div className="table-scroll"><table><thead><tr><th>Email</th><th>Role</th><th>Documents</th><th>Status</th><th>Action</th></tr></thead>
      <tbody>{users.map(user => <tr key={user.id}><td>{user.email}</td><td>{user.role}</td><td>{user.document_count}</td><td>{user.is_active ? "Active" : "Disabled"}</td><td><button className="secondary" disabled={user.role === "admin" || busyId === user.id} onClick={() => toggle(user)}>{user.is_active ? "Disable" : "Enable"}</button></td></tr>)}</tbody>
    </table></div>
  </section>;
}
