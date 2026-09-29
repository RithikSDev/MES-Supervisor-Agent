import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Same path layout as the nginx container, so the app never hardcodes service hosts.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      "/api/core": { target: "http://localhost:8000", rewrite: (p) => p.replace(/^\/api\/core/, "") },
      "/api/agent": { target: "http://localhost:8090", rewrite: (p) => p.replace(/^\/api\/agent/, "") },
    },
  },
});
