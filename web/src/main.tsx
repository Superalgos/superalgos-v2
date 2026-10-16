import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "@xyflow/react/dist/style.css";
import "./styles.css";

const el = document.getElementById("root");
if (!el) throw new Error("Missing #root element in index.html; the app cannot mount.");
createRoot(el).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
