import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

/** Fetch a resource and keep it fresh by polling. */
export function usePoll(path, interval = 3000, deps = []) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const alive = useRef(true);

  const load = useCallback(async () => {
    if (!path) return;
    try {
      const d = await api.get(path);
      if (alive.current) {
        setData(d);
        setError(null);
      }
    } catch (e) {
      if (alive.current) setError(e.message);
    } finally {
      if (alive.current) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ...deps]);

  useEffect(() => {
    alive.current = true;
    setLoading(true);
    load();
    const id = interval ? setInterval(load, interval) : null;
    return () => {
      alive.current = false;
      if (id) clearInterval(id);
    };
  }, [load, interval]);

  return { data, error, loading, reload: load };
}

export function useToast() {
  const [toast, setToast] = useState(null);
  const show = useCallback((message, tone = "info") => {
    setToast({ message, tone, id: Date.now() });
    setTimeout(() => setToast(null), 4000);
  }, []);
  return [toast, show];
}
