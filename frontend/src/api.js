
const KEY = "docintel_api_key";
const TOKEN = "docintel_session_token";

export const authToken = {
  get: () => sessionStorage.getItem(TOKEN) || "",
  set: (value) => value ? sessionStorage.setItem(TOKEN, value) : sessionStorage.removeItem(TOKEN),
};

// Empty in local development; set VITE_API_BASE_URL for deployment.
const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/+$/, "");

export const apiKey = {
  get: () => sessionStorage.getItem(KEY) || "",
  set: (v) => sessionStorage.setItem(KEY, v),
};

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (authToken.get()) headers.Authorization = "Bearer " + authToken.get();
  else if (apiKey.get()) headers["X-API-Key"] = apiKey.get();

  const res = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers,
  });

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string"
        ? body.detail
        : JSON.stringify(body.detail);
    } catch {
      // Non-JSON error body.
    }
    throw new ApiError(res.status, detail);
  }

  return res.json();
}

const json = (method, body) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body ?? {}),
});

async function downloadFile(path, filename) {
  const headers = {};
  if (authToken.get()) headers.Authorization = "Bearer " + authToken.get();
  else if (apiKey.get()) headers["X-API-Key"] = apiKey.get();
  const response = await fetch(`${API_BASE_URL}${path}`, { headers });
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch {}
    throw new ApiError(response.status, detail);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

export const api = {
  exportInvoicesXlsx: (mappingId) => downloadFile("/api/exports/invoices.xlsx" + (mappingId ? "?mapping_id=" + mappingId : ""), "docintel-invoices.xlsx"),
  exportInvoicesCsv: (mappingId) => downloadFile("/api/exports/invoices.csv" + (mappingId ? "?mapping_id=" + mappingId : ""), "docintel-invoices.csv"),
  dashboard: () => request("/api/business/dashboard"),
  suppliers: () => request("/api/business/suppliers"),
  saveSupplier: (body) => request("/api/business/suppliers", json("POST", body)),
  purchaseOrders: () => request("/api/business/purchase-orders"),
  createPurchaseOrder: (body) => request("/api/business/purchase-orders", json("POST", body)),
  mappings: () => request("/api/business/mappings"),
  createMapping: (body) => request("/api/business/mappings", json("POST", body)),
  getApproval: (id) => request("/api/invoices/" + id + "/approval"),
  updateApproval: (id, body) => request("/api/invoices/" + id + "/approval", json("POST", body)),
  matchPurchaseOrder: (id) => request("/api/invoices/" + id + "/purchase-order-match"),
  downloadCorrectedXlsx: (id) => downloadFile(`/api/invoices/${id}/corrected.xlsx`, `invoice-${id}-corrected.xlsx`),
  downloadCorrectedPdf: (id) => downloadFile(`/api/invoices/${id}/corrected.pdf`, `invoice-${id}-corrected.pdf`),
  health: () => request("/api/health"),
  register: (credentials) => request("/api/auth/register", json("POST", credentials)),
  login: (credentials) => request("/api/auth/login", json("POST", credentials)),
  me: () => request("/api/auth/me"),
  logout: () => request("/api/auth/logout", json("POST")),
  adminOverview: () => request("/api/admin/overview"),
  adminUsers: (params = {}) => request("/api/admin/users?" + new URLSearchParams(params)),
  setUserActive: (id, is_active) => request("/api/admin/users/" + id, json("PATCH", { is_active })),
  listDocuments: () => request("/api/documents?limit=200"),
  getDocument: (id, limit = 25) =>
    request(`/api/documents/${id}?invoice_limit=${limit}`),
  deleteDocument: (id) =>
    request(`/api/documents/${id}`, { method: "DELETE" }),
  search: (params) =>
    request(`/api/search?${new URLSearchParams(params)}`),
  upload: (files) => {
    const body = new FormData();
    for (const f of files) body.append("files", f);
    return request("/api/documents", { method: "POST", body });
  },
  semanticSearch: (params) =>
    request(`/api/search/semantic?${new URLSearchParams(params)}`),
  patchInvoice: (id, patch) =>
    request(`/api/invoices/${id}`, json("PATCH", patch)),
  corrections: (id) =>
    request(`/api/invoices/${id}/corrections`),
  resolveIssue: (id, resolved) =>
    request(`/api/issues/${id}/resolve`, json("POST", { resolved })),
  reprocess: (id) =>
    request(`/api/documents/${id}/reprocess`, json("POST")),
  retryFailed: () =>
    request("/api/jobs/retry-failed", json("POST")),
};

// Escape document text while preserving server-generated mark tags.
export function safeSnippet(s = "") {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/&lt;mark&gt;/g, "<mark>")
    .replace(/&lt;\/mark&gt;/g, "</mark>");
}

// Fetch and open/download the original uploaded file.
export async function openOriginal(id, filename) {
  const headers = authToken.get()
    ? { Authorization: "Bearer " + authToken.get() }
    : apiKey.get() ? { "X-API-Key": apiKey.get() } : {};

  const res = await fetch(
    `${API_BASE_URL}/api/documents/${id}/file`,
    { headers }
  );

  if (!res.ok) {
    throw new ApiError(
      res.status,
      res.status === 404
        ? "The original file is not available"
        : res.statusText
    );
  }

  const blob = await res.blob();
  const url = URL.createObjectURL(blob);

  if (["application/pdf", "image/png", "image/jpeg"].includes(blob.type)) {
    window.open(url, "_blank");
  } else {
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
  }

  setTimeout(() => URL.revokeObjectURL(url), 60000);
}
