import "./App.css";

import { Bootstrap } from "./Bootstrap";
import { Workbench } from "./Workbench";

export function App() {
  if (window.location.pathname === "/bootstrap") return <Bootstrap />;
  return <Workbench />;
}
