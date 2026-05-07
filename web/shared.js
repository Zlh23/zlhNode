/**
 * 共享工具函数：defaultApiBase / apiRoot / fetchJson
 * 供 app.js 和 bridge-app.js 共用。
 */

function defaultApiBase() {
  const loc = window.location;
  if (loc.protocol === "file:") return "http://127.0.0.1:8188";
  const port = loc.port || (loc.protocol === "https:" ? "443" : "80");
  if (port === "8188") return loc.origin;
  const host = loc.hostname || "127.0.0.1";
  return `http://${host}:8188`;
}

function apiRoot() {
  const el = document.getElementById("apiBase");
  if (el) {
    const raw = el.value.trim();
    if (raw) return raw.replace(/\/$/, "");
  }
  return defaultApiBase();
}

async function fetchJson(url, options) {
  let res;
  try {
    res = await fetch(url, options);
  } catch (e) {
    const hint =
      e && e.message === "Failed to fetch"
        ? "（网络被拒/CORS）"
        : "";
    throw new Error((e && e.message || "fetch") + hint);
  }
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    throw new Error(`非 JSON 响应: ${res.status} ${(text || "").slice(0, 200)}`);
  }
  if (!res.ok) {
    let msg = data && data.message;
    if (!msg && data && data.error != null) {
      const err = data.error;
      if (typeof err === "string") msg = err;
      else if (typeof err === "object" && err.message) msg = String(err.message);
      else msg = JSON.stringify(err);
    }
    if (!msg) msg = res.statusText;
    throw new Error(msg);
  }
  return data;
}
