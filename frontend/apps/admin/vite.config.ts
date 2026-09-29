import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Same path layout as the nginx container, so the app never hardcodes service hosts.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api/core": { target: "http://localhost:8000", rewrite: (p) => p.replace(/^\/api\/core/, "") },
      "/api/agent": { target: "http://localhost:8090", rewrite: (p) => p.replace(/^\/api\/agent/, "") },
      "/api/notify": { target: "http://localhost:8095", rewrite: (p) => p.replace(/^\/api\/notify/, "") },
      "/api/erp": { target: "http://localhost:8020", rewrite: (p) => p.replace(/^\/api\/erp/, "") },
      "/api/mock-sap": { target: "http://localhost:8030", rewrite: (p) => p.replace(/^\/api\/mock-sap/, "") },
    },
  },
});
