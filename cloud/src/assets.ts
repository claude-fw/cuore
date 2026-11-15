/**
 * File statici della PWA serviti dal Worker (dopo Access, come la pagina): service worker con le notifiche push,
 * modulo `push.js` per le Impostazioni, manifest e icone. Stessi file del ponte del Mac (`app/bridge/serve.py`).
 * Impacchettati come testo (`.js`, `.webmanifest`) e dati (`.png`): regole in wrangler.jsonc e nei test (helpers.ts).
 */
import appleIcon from "../../app/icons/apple-touch-icon.png";
import icon192 from "../../app/icons/icon-192.png";
import icon512 from "../../app/icons/icon-512.png";
import manifest from "../../app/manifest.webmanifest";
import pushJs from "../../app/push.js";
import sw from "../../app/sw.js";

const JS = "text/javascript; charset=utf-8";
export const ASSETS = {
  "/sw.js": { body: sw, type: JS },
  "/push.js": { body: pushJs, type: JS },
  "/manifest.webmanifest": { body: manifest, type: "application/manifest+json" },
  "/icons/icon-192.png": { body: icon192, type: "image/png" },
  "/icons/icon-512.png": { body: icon512, type: "image/png" },
  "/icons/apple-touch-icon.png": { body: appleIcon, type: "image/png" },
} as const;
