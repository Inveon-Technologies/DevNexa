const BASE = import.meta.env.VITE_API_URL || "";

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
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
};

export const OPERATOR_KEY = "devnexa.operator";
export function operatorName() {
  try {
    return localStorage.getItem(OPERATOR_KEY) || "operator";
  } catch {
    return "operator";
  }
}
