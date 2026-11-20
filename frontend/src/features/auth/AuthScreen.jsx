import { useState } from "react";
import { api, authToken } from "../../api.js";

export default function AuthScreen({ onAuthenticated }) {
  const [mode, setMode] = useState("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const result = await api[mode]({ email, password });
      authToken.set(result.access_token);
      onAuthenticated(result.user);
    } catch (err) {
      setError(err.message || "Authentication failed");
    } finally {
      setBusy(false);
    }
  }

  return <section className="auth-shell">
    <form className="auth-card" onSubmit={submit}>
      <h1>DocIntel</h1>
      <p>{mode === "login" ? "Sign in to your private document workspace." : "Create your private document workspace."}</p>
      <label>Email address<input type="email" autoComplete="email" required maxLength={254} value={email} onChange={e => setEmail(e.target.value)} /></label>
      <label>Password<input type="password" autoComplete={mode === "login" ? "current-password" : "new-password"} required minLength={mode === "login" ? 1 : 12} maxLength={256} value={password} onChange={e => setPassword(e.target.value)} /></label>
      {mode === "register" && <small>Use at least 12 characters. Your account starts with standard user permissions.</small>}
      {error && <p className="banner bad" role="alert">{error}</p>}
      <button className="wide" disabled={busy}>{busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}</button>
      <p className="auth-switch">{mode === "login" ? "New to DocIntel?" : "Already have an account?"} <button type="button" className="link" onClick={() => { setMode(mode === "login" ? "register" : "login"); setError(""); }}>{mode === "login" ? "Create an account" : "Sign in"}</button></p>
    </form>
  </section>;
}
