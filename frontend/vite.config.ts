import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";

function fromFileUrl(url: URL): string {
  const pathname = decodeURIComponent(url.pathname);
  return /^\/[A-Za-z]:\//.test(pathname) ? pathname.slice(1) : pathname;
}

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fromFileUrl(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5188,
    proxy: {
      "/api": {
        target: "http://localhost:8088",
        changeOrigin: true,
      },
      "/charts": {
        target: "http://localhost:8088",
        changeOrigin: true,
      },
      "/health": {
        target: "http://localhost:8088",
        changeOrigin: true,
      },
    },
  },
});
