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
