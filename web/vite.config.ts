import { defineConfig } from "vite";

export default defineConfig({
  server: {
    port: 5173,
    proxy: {
      "/audio": "http://127.0.0.1:8089",
      "/voices": "http://127.0.0.1:8089",
      "/ws": {
        target: "ws://127.0.0.1:8089",
        ws: true,
      },
    },
  },
});
