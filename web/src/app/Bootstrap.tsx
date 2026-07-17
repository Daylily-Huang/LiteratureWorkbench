import { useEffect, useRef, useState } from "react";

type BootstrapProps = {
  navigate?: (path: string) => void;
};

export function Bootstrap({
  navigate = (path: string) => window.location.replace(path),
}: BootstrapProps) {
  const started = useRef(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const token = window.location.hash.slice(1);
    if (token.length === 0) {
      setFailed(true);
      return;
    }

    window.history.replaceState(null, "", "/bootstrap");
    void fetch("/api/session/bootstrap", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    }).then((response) => {
      if (response.status === 204) {
        navigate("/");
        return;
      }
      setFailed(true);
    }).catch(() => setFailed(true));
  }, [navigate]);

  if (failed) {
    return <p role="alert">启动链接已失效，请从启动器重新打开</p>;
  }
  return <p role="status">正在建立本机会话…</p>;
}
