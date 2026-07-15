import { useEffect, useRef } from "react";

type BootstrapProps = {
  navigate?: (path: string) => void;
};

export function Bootstrap({
  navigate = (path: string) => window.location.replace(path),
}: BootstrapProps) {
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const token = window.location.hash.slice(1);
    if (token.length === 0) return;

    window.history.replaceState(null, "", "/bootstrap");
    void fetch("/api/session/bootstrap", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    }).then((response) => {
      if (response.status === 204) navigate("/");
    });
  }, [navigate]);

  return null;
}
