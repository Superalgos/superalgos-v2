import { createHash } from "node:crypto";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

/**
 * Stamps the build id into index.html as <meta name="sa-build">. The id hashes the emitted js/css
 * file names (content-hashed by Vite) plus the html source, so it changes whenever the bundle does.
 * The API reads the same tag from the served index.html and reports it in the X-SA-Build header.
 */
function buildId(): Plugin {
  return {
    name: "sa-build-id",
    transformIndexHtml: {
      order: "post",
      handler(html, ctx) {
        if (!ctx.bundle) return undefined; // dev server: no build id, so no reload checks
        const files = Object.keys(ctx.bundle)
          .filter((f) => /\.(js|css)$/.test(f))
          .sort();
        const id = createHash("sha256")
          .update(files.join("\n"))
          .update(html)
          .digest("hex")
          .slice(0, 16);
        return [{ tag: "meta", attrs: { name: "sa-build", content: id }, injectTo: "head" }];
      },
    },
  };
}

export default defineConfig({
  plugins: [react(), buildId()],
  server: {
    // the designer bundles the engine's JSON Schema and examples from packages/sa-engine (one level above web/)
    fs: { allow: [".."] },
    proxy: { "/api": { target: "http://127.0.0.1:8787", changeOrigin: false } },
  },
  build: { sourcemap: true },
});
