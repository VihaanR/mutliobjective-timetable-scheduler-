/**
 * DJSCE Institutional SVG Vector Icon Library (Lucide/Feather Style)
 * Zero-dependency, resolution-independent vector icons for academic scheduling.
 * 
 * Provides:
 * - window.Icons.get(name, options): Generates sanitized inline SVG string.
 * - window.Icons.createElement(name, options): Generates an SVG DOM element.
 * - window.Icons.replace(rootOrSelector): Replaces elements matching [data-icon] with SVGs.
 * - window.Icons.definitions: Direct dictionary of raw SVG inner paths.
 * - window.SVGIcons & window.getIcon: 100% backward compatibility proxies.
 */
(function (global) {
  "use strict";

  // Clean Lucide-style inner SVG paths (24x24 viewBox coordinate system)
  const ICON_DEFINITIONS = {
    // 24 Core Required Icons
    pin: `<line x1="12" y1="17" x2="12" y2="22"></line><path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z"></path>`,
    pinned: `<line x1="12" y1="17" x2="12" y2="22"></line><path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z" fill="currentColor"></path>`,
    calendar: `<rect width="18" height="18" x="3" y="4" rx="2" ry="2"></rect><line x1="16" y1="2" x2="16" y2="6"></line><line x1="8" y1="2" x2="8" y2="6"></line><line x1="3" y1="10" x2="21" y2="10"></line>`,
    clock: `<circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline>`,
    user: `<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle>`,
    users: `<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><path d="M22 21v-2a4 4 0 0 0-3-3.87"></path><path d="M16 3.13a4 4 0 0 1 0 7.75"></path>`,
    building: `<rect width="16" height="20" x="4" y="2" rx="2" ry="2"></rect><path d="M9 22v-4h6v4"></path><path d="M8 6h.01"></path><path d="M16 6h.01"></path><path d="M8 10h.01"></path><path d="M16 10h.01"></path><path d="M8 14h.01"></path><path d="M16 14h.01"></path>`,
    book: `<path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1-2.5-2.5Z"></path><path d="M6 2v20"></path>`,
    sliders: `<line x1="4" y1="21" x2="4" y2="14"></line><line x1="4" y1="10" x2="4" y2="3"></line><line x1="12" y1="21" x2="12" y2="12"></line><line x1="12" y1="8" x2="12" y2="3"></line><line x1="20" y1="21" x2="20" y2="16"></line><line x1="20" y1="12" x2="20" y2="3"></line><line x1="1" y1="14" x2="7" y2="14"></line><line x1="9" y1="8" x2="15" y2="8"></line><line x1="17" y1="16" x2="23" y2="16"></line>`,
    cpu: `<rect width="16" height="16" x="4" y="4" rx="2"></rect><rect width="6" height="6" x="9" y="9"></rect><path d="M15 2v2"></path><path d="M15 20v2"></path><path d="M2 15h2"></path><path d="M2 9h2"></path><path d="M20 15h2"></path><path d="M20 9h2"></path><path d="M9 2v2"></path><path d="M9 20v2"></path>`,
    checkCircle: `<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline>`,
    alertTriangle: `<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line>`,
    xCircle: `<circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line>`,
    play: `<polygon points="5 3 19 12 5 21 5 3"></polygon>`,
    download: `<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line>`,
    lock: `<rect width="18" height="11" x="3" y="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path>`,
    refresh: `<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"></path><path d="M21 3v5h-5"></path><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"></path><path d="M8 16H3v5"></path>`,
    database: `<ellipse cx="12" cy="5" rx="9" ry="3"></ellipse><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"></path><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"></path>`,
    barChart: `<line x1="12" y1="20" x2="12" y2="10"></line><line x1="18" y1="20" x2="18" y2="4"></line><line x1="6" y1="20" x2="6" y2="16"></line>`,
    target: `<circle cx="12" cy="12" r="10"></circle><circle cx="12" cy="12" r="6"></circle><circle cx="12" cy="12" r="2"></circle>`,
    search: `<circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line>`,
    logOut: `<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path><polyline points="16 17 21 12 16 7"></polyline><line x1="21" y1="12" x2="9" y2="12"></line>`,
    award: `<circle cx="12" cy="8" r="7"></circle><polyline points="8.21 13.89 7 23 12 20 17 23 15.79 13.88"></polyline>`,
    shield: `<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path>`,

    // Supplementary Aliases for Platform, Login, Dashboard, Index & Emoji Purge
    check: `<polyline points="20 6 9 17 4 12"></polyline>`,
    unlock: `<rect width="18" height="11" x="3" y="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 9.9-1"></path>`,
    eye: `<path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"></path><circle cx="12" cy="12" r="3"></circle>`,
    eyeOff: `<path d="M9.88 9.88a3 3 0 1 0 4.24 4.24"></path><path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68"></path><path d="M6.61 6.61A13.526 13.526 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61"></path><line x1="2" y1="2" x2="22" y2="22"></line>`,
    logIn: `<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"></path><polyline points="10 17 15 12 10 7"></polyline><line x1="15" y1="12" x2="3" y2="12"></line>`,
    graduationCap: `<path d="M22 10v6M2 10l10-5 10 5-10 5z"></path><path d="M6 12v5c3 3 9 3 12 0v-5"></path>`,
    zap: `<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon>`,
    flaskConical: `<path d="M10 2v7.527a2 2 0 0 1-.211.896L4.72 20.55a1 1 0 0 0 .9 1.45h12.76a1 1 0 0 0 .9-1.45l-5.069-10.127A2 2 0 0 1 14 9.527V2"></path><path d="M8.5 2h7"></path><path d="M7 16h10"></path>`,
    pause: `<rect x="6" y="4" width="4" height="16"></rect><rect x="14" y="4" width="4" height="16"></rect>`,
    chevronLeft: `<polyline points="15 18 9 12 15 6"></polyline>`,
    chevronRight: `<polyline points="9 18 15 12 9 6"></polyline>`
  };

  /**
   * Sanitizes string values to prevent injection in attribute contexts.
   */
  function sanitizeAttr(str) {
    if (str === null || str === undefined) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  const Icons = {
    definitions: ICON_DEFINITIONS,

    /**
     * Checks if icon definition exists.
     * @param {string} name
     * @returns {boolean}
     */
    has(name) {
      return Boolean(name && ICON_DEFINITIONS.hasOwnProperty(name));
    },

    /**
     * Lists all available icon names.
     * @returns {string[]}
     */
    list() {
      return Object.keys(ICON_DEFINITIONS);
    },

    /**
     * Returns sanitized inline SVG markup.
     * @param {string} name - Icon name
     * @param {Object|string} [options={}] - Options object or string representing className
     * @returns {string}
     */
    get(name, options = {}) {
      if (!name || !ICON_DEFINITIONS.hasOwnProperty(name)) {
        console.warn(`[Icons] Icon "${name}" not found.`);
        return "";
      }

      // Allow shorthand string: Icons.get('calendar', 'my-class')
      const opts = typeof options === "string" ? { className: options } : (options || {});

      const size = opts.size !== undefined ? opts.size : (opts.width || 16);
      const width = opts.width || size;
      const height = opts.height || size;
      const strokeWidth = opts.strokeWidth !== undefined ? opts.strokeWidth : 2;
      const stroke = opts.stroke || "currentColor";
      const fill = opts.fill || "none";
      const className = opts.className ? ` class="${sanitizeAttr(opts.className)}"` : "";
      const ariaHidden = opts.ariaHidden !== undefined ? ` aria-hidden="${opts.ariaHidden}"` : ' aria-hidden="true"';
      const role = opts.role ? ` role="${sanitizeAttr(opts.role)}"` : ' role="img"';
      const titleTag = opts.title ? `<title>${sanitizeAttr(opts.title)}</title>` : "";

      const inner = ICON_DEFINITIONS[name];

      return `<svg width="${sanitizeAttr(width)}" height="${sanitizeAttr(height)}" viewBox="0 0 24 24" fill="${sanitizeAttr(fill)}" stroke="${sanitizeAttr(stroke)}" stroke-width="${sanitizeAttr(strokeWidth)}" stroke-linecap="round" stroke-linejoin="round"${className}${role}${ariaHidden}>${titleTag}${inner}</svg>`;
    },

    /**
     * Creates an SVG DOM element.
     * @param {string} name - Icon name
     * @param {Object|string} [options={}] - Options object or className
     * @returns {SVGElement|null}
     */
    createElement(name, options = {}) {
      const html = this.get(name, options);
      if (!html) return null;
      const template = document.createElement("template");
      template.innerHTML = html.trim();
      return template.content.firstChild;
    },

    /**
     * Declarative DOM replacement.
     * Finds all elements matching [data-icon] within the given root (or document)
     * and replaces them with corresponding inline SVG icons.
     * 
     * Supported attributes on target elements:
     * - data-icon: Name of icon (e.g. "calendar")
     * - data-size: Size in px (e.g. "18")
     * - data-stroke-width: Stroke width (e.g. "2")
     * - data-title: Tooltip / title text
     * - class: Preserves existing classes and adds 'djsce-icon'
     * 
     * @param {HTMLElement|Document|string} [rootOrSelector=document]
     * @returns {number} Count of elements replaced
     */
    replace(rootOrSelector) {
      if (typeof document === "undefined") return 0;

      let root = document;
      if (typeof rootOrSelector === "string") {
        root = document.querySelector(rootOrSelector) || document;
      } else if (rootOrSelector && rootOrSelector.nodeType) {
        root = rootOrSelector;
      }

      const elements = root.querySelectorAll("[data-icon]");
      let count = 0;

      elements.forEach((el) => {
        const name = el.getAttribute("data-icon");
        if (!name || !ICON_DEFINITIONS.hasOwnProperty(name)) return;

        const size = el.getAttribute("data-size") || undefined;
        const strokeWidth = el.getAttribute("data-stroke-width") || undefined;
        const title = el.getAttribute("data-title") || el.getAttribute("title") || undefined;
        const existingClass = el.getAttribute("class") || "";
        const combinedClass = existingClass ? `${existingClass} djsce-icon` : "djsce-icon";

        const svgEl = Icons.createElement(name, {
          size,
          strokeWidth,
          title,
          className: combinedClass
        });

        if (svgEl) {
          // Transfer any data- attributes other than data-icon
          Array.from(el.attributes).forEach((attr) => {
            if (attr.name.startsWith("data-") && attr.name !== "data-icon" && attr.name !== "data-size" && attr.name !== "data-stroke-width") {
              svgEl.setAttribute(attr.name, attr.value);
            }
          });
          el.parentNode.replaceChild(svgEl, el);
          count++;
        }
      });

      return count;
    }
  };

  // Backwards compatibility layer for existing SVGIcons and getIcon calls
  const SVGIconsProxy = {};
  Object.keys(ICON_DEFINITIONS).forEach((key) => {
    Object.defineProperty(SVGIconsProxy, key, {
      get() {
        return Icons.get(key);
      },
      enumerable: true,
      configurable: true
    });
  });

  function getIconCompat(name, extraClass = "") {
    return Icons.get(name, { className: extraClass });
  }

  // Export to global scope (Window / globalThis / self)
  if (typeof global !== "undefined") {
    global.Icons = Icons;
    global.SVGIcons = SVGIconsProxy;
    global.getIcon = getIconCompat;
  }

  // CommonJS / Node export if tested in Node
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { Icons, SVGIcons: SVGIconsProxy, getIcon: getIconCompat };
  }

})(typeof window !== "undefined" ? window : typeof globalThis !== "undefined" ? globalThis : this);
