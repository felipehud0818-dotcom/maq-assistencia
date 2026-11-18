import axios from "axios";

export const api = axios.create({ baseURL: `${process.env.REACT_APP_BACKEND_URL}/api`, timeout: 60000 });

const UNLOCK_KEY = "fin_unlock";
export const getUnlock = () => {
  try {
    const u = JSON.parse(sessionStorage.getItem(UNLOCK_KEY) || "null");
    return u && u.expires_at * 1000 > Date.now() ? u : null;
  } catch { return null; }
};
export const setUnlock = (u) => sessionStorage.setItem(UNLOCK_KEY, JSON.stringify(u));
export const clearUnlock = () => {
  sessionStorage.removeItem(UNLOCK_KEY);
  window.dispatchEvent(new Event("finance-locked"));
};

api.interceptors.request.use((config) => {
  const token = localStorage.getItem("token");
  if (token) config.headers.Authorization = `Bearer ${token}`;
  const u = getUnlock();
  if (u) config.headers["X-Finance-Unlock"] = u.token;
  return config;
});

api.interceptors.response.use((r) => r, (e) => {
  if (e?.response?.status === 423) clearUnlock();
  return Promise.reject(e);
});

export const errMsg = (e) => {
  if (!e?.response) return "Sem conexão com o servidor";
  const d = e.response.data?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => x.msg).join(", ");
  return "Erro inesperado";
};
