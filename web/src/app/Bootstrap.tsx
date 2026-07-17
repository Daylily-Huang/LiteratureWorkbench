import { useCallback, useEffect, useRef, useState } from "react";

type BootstrapProps = {
  navigate?: (path: string) => void;
};

export function Bootstrap({
  navigate = (path: string) => window.location.replace(path),
}: BootstrapProps) {
  const started = useRef(false);
  const token = useRef<string | null>(null);
  const requestInFlight = useRef(false);
  const [status, setStatus] = useState<"pending" | "invalid" | "transient">("pending");

  const establishSession = useCallback(async () => {
    if (requestInFlight.current) return;
    const currentToken = token.current;
    if (currentToken === null) {
      setStatus("invalid");
      return;
    }
    requestInFlight.current = true;
    setStatus("pending");
    try {
      const response = await fetch("/api/session/bootstrap", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: currentToken }),
      });
      if (response.status === 204) {
        token.current = null;
        navigate("/");
      } else if (response.status === 403) {
        token.current = null;
        setStatus("invalid");
      } else {
        setStatus("transient");
      }
    } catch {
      setStatus("transient");
    } finally {
      requestInFlight.current = false;
    }
  }, [navigate]);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const fragmentToken = window.location.hash.slice(1);
    if (fragmentToken.length === 0) {
      setStatus("invalid");
      return;
    }

    token.current = fragmentToken;
    window.history.replaceState(null, "", "/bootstrap");
    void establishSession();
  }, [establishSession]);

  if (status === "invalid") {
    return <p role="alert">启动链接已失效，请从启动器重新打开</p>;
  }
  if (status === "transient") {
    return <div><p role="alert">暂时无法建立本机会话，请重试</p><button onClick={() => void establishSession()} type="button">重试建立会话</button></div>;
  }
  return <p role="status">正在建立本机会话…</p>;
}
