import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { WorkspaceShell } from "@/components/workspace/workspace-shell";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <WorkspaceShell />
  </StrictMode>,
);
