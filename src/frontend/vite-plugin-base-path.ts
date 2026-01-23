import type { Plugin } from 'vite';

/**
 * Vite plugin to inject dynamic base path script before any resource tags
 * This ensures <base> tag is created before browser starts loading assets
 */
export function dynamicBasePathPlugin(): Plugin {
  return {
    name: 'dynamic-base-path',
    transformIndexHtml(html) {
      // Insert the base path script right after <head> tag
      const baseScript = `
    <script>
      // CRITICAL: This must execute BEFORE any resource loading
      // Dynamically set base URL for sub-path deployments
      (function() {
        const pathname = window.location.pathname;
        const match = pathname.match(/^\\/(qwen3|qwen2\\.5|deepseek|llama-3\\.1|llama-3\\.3)/);
        if (match) {
          const base = document.createElement('base');
          base.href = '/' + match[1] + '/';
          // Insert as first child of head to ensure it's processed first
          document.head.insertBefore(base, document.head.firstChild);
        }
      })();
    </script>`;

      // Insert right after <head> opening tag
      return html.replace(/<head>/, `<head>${baseScript}`);
    },
  };
}
