import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests_js/**/*.test.js"],
    environment: "node",
    coverage: { include: ["transcria/web/static/js/srt_time.js"], reporter: ["text"] },
  },
});
