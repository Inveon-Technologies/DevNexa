const BASE = import.meta.env.VITE_API_URL || "";

export const ADMIN_KEY = "devnexa.adminKey";
export function adminKey() {
  try {
    return localStorage.getItem(ADMIN_KEY) || "";
  } catch {
    return "";
  }
}

/** URL for an EventSource (which cannot send headers), with the admin key as a query parameter. */
export function streamUrl(path) {
  const key = adminKey();
  return `${BASE}${path}${key ? `${path.includes("?") ? "&" : "?"}key=${encodeURIComponent(key)}` : ""}`;
}

async function request(path, options = {}) {
  const key = adminKey();
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(key ? { "X-DevNexa-Key": key } : {}) },
    ...options,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch {
      /* not json */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json();
}

export const api = {
  get: (p) => request(p),
  post: (p, body = {}) => request(p, { method: "POST", body }),
  patch: (p, body = {}) => request(p, { method: "PATCH", body }),
  put: (p, body = {}) => request(p, { method: "PUT", body }),
  del: (p) => request(p, { method: "DELETE" }),
};

export const OPERATOR_KEY = "devnexa.operator";
export function operatorName() {
  try {
    return localStorage.getItem(OPERATOR_KEY) || "operator";
  } catch {
    return "operator";
  }
}

async function rawFetch(path, options = {}) {
  const key = adminKey();
  const res = await fetch(`${BASE}${path}`, { ...options, headers: { ...(key ? { "X-DevNexa-Key": key } : {}), ...(options.headers || {}) } });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch {
      /* not json */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res;
}

/** Upload a file as the raw request body (used for backup imports, which can be large). */
export async function uploadFile(path, file) {
  const res = await rawFetch(path, { method: "POST", body: file, headers: { "Content-Type": "application/octet-stream" } });
  return res.json();
}

/** Download a file from the API (with the admin key) and hand it to the browser to save. */
export async function downloadFile(path, fallbackName = "download") {
  const res = await rawFetch(path);
  const cd = res.headers.get("content-disposition") || "";
  const name = (cd.match(/filename="?([^";]+)"?/) || [])[1] || fallbackName;
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
