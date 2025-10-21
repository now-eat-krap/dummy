(function () {
  try {
    const script = document.currentScript;
    if (!script) {
      return;
    }

    const cfg = {
      site: script.dataset.site || "",
      endpoint: script.dataset.endpoint || "",
      sample: parseFloat(script.dataset.sample || "1"),
      click: script.dataset.click === "true",
      scroll: script.dataset.scroll === "true",
      spa: script.dataset.spa === "true",
      xBins: parseBinCount(script.dataset.xbins || script.dataset.gridx, 12),
      yBins: parseBinCount(script.dataset.ybins || script.dataset.gridy, 8),
    };

    if (!cfg.site || !cfg.endpoint) {
      return;
    }

    if (Number.isNaN(cfg.sample) || cfg.sample <= 0 || cfg.sample > 1) {
      cfg.sample = 1;
    }

    if (Math.random() > cfg.sample) {
      return;
    }

    if (!cfg.endpoint.endsWith("/ba")) {
      cfg.endpoint = cfg.endpoint.replace(/\/+$/, "");
    }

    if (!cfg.endpoint) {
      return;
    }

    if (!window.localStorage || !window.sessionStorage) {
      console.debug("logflow: storage unavailable");
    }

    const endpoint = cfg.endpoint;
    const state = {
      pageStart: Date.now(),
      maxDepth: 0,
      lastClickTs: 0,
      uid: ensureId(window.localStorage, "logflow:uid"),
      sid: ensureId(window.sessionStorage, "logflow:sid"),
      pointer: {
        x: Math.round(window.innerWidth ? window.innerWidth / 2 : 0),
        y: Math.round(window.innerHeight ? window.innerHeight / 2 : 0),
      },
      scrollTimer: null,
      closeSent: false,
      lastPageUrl: "",
      lastPageSentAt: 0,
    };

    capturePage("load");
    if (cfg.spa) {
      wrapHistory("pushState");
      wrapHistory("replaceState");
      window.addEventListener("popstate", function () {
        capturePage("popstate");
      });
    }

    if (cfg.click) {
      document.addEventListener(
        "click",
        function (ev) {
          if (!ev || ev.button !== 0 || ev.metaKey || ev.ctrlKey) {
            return;
          }
          const now = Date.now();
          if (now - state.lastClickTs < 250) {
            return;
          }
          state.lastClickTs = now;
          const target = ev.target;
          state.pointer = {
            x: Math.round(ev.clientX || 0),
            y: Math.round(ev.clientY || 0),
          };
          const vp = viewport();
          const xBin = toBin(ev.clientX, vp.w, cfg.xBins);
          const yBin = toBin(ev.clientY, vp.h, cfg.yBins);
          const section = resolveSection(target);
          send("click", {
            element: describeTarget(target),
            element_text: elementText(target),
            depth: Math.round(state.maxDepth),
            coords: {
              x: Math.round(ev.clientX || 0),
              y: Math.round(ev.clientY || 0),
            },
            x_bin: xBin,
            y_bin: yBin,
            section: section,
          });
        },
        true
      );
      window.addEventListener(
        "pointermove",
        function (ev) {
          if (!ev) {
            return;
          }
          state.pointer = {
            x: Math.round(ev.clientX || 0),
            y: Math.round(ev.clientY || 0),
          };
        },
        true
      );
      window.addEventListener(
        "mousemove",
        function (ev) {
          if (!ev) {
            return;
          }
          state.pointer = {
            x: Math.round(ev.clientX || 0),
            y: Math.round(ev.clientY || 0),
          };
        },
        true
      );
    }

    if (cfg.scroll) {
      window.addEventListener(
        "scroll",
        function () {
          const metrics = currentScrollMetrics();
          if (metrics.depth > state.maxDepth) {
            state.maxDepth = metrics.depth;
          }
          if (state.scrollTimer) {
            window.clearTimeout(state.scrollTimer);
          }
          state.scrollTimer = window.setTimeout(function () {
            state.scrollTimer = null;
            dispatchScroll("scroll");
          }, 250);
        },
        { passive: true }
      );

      document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "hidden") {
          dispatchScroll("visibility");
          captureClose("visibility");
        }
      });

      window.addEventListener("beforeunload", function () {
        dispatchScroll("unload");
        captureClose("unload");
      });
    } else {
      window.addEventListener("beforeunload", function () {
        captureClose("unload");
      });
      document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "hidden") {
          captureClose("visibility");
        }
      });
    }

    function capturePage(source) {
      const now = Date.now();
      const currentUrl = window.location.href || "";
      if (state.lastPageUrl === currentUrl && now - state.lastPageSentAt < 500) {
        state.pageStart = now;
        state.maxDepth = 0;
        state.closeSent = false;
        return;
      }
      state.lastPageUrl = currentUrl;
      state.lastPageSentAt = now;
      state.pageStart = now;
      state.maxDepth = 0;
      state.closeSent = false;
      send("page", { source: source, depth: 0, sec: 0 });
    }

    function secondsSinceStart() {
      return Math.max(0, Math.round((Date.now() - state.pageStart) / 1000));
    }

    function ensureId(storage, key) {
      if (!storage) {
        return fallbackId();
      }
      try {
        let value = storage.getItem(key);
        if (!value) {
          value = fallbackId();
          storage.setItem(key, value);
        }
        return value;
      } catch (_) {
        return fallbackId();
      }
    }

    function fallbackId() {
      if (window.crypto && window.crypto.randomUUID) {
        return window.crypto.randomUUID();
      }
      return "lf-" + Math.random().toString(16).slice(2) + Date.now().toString(16);
    }

    function wrapHistory(name) {
      const original = window.history[name];
      if (typeof original !== "function") {
        return;
      }
      window.history[name] = function () {
        const result = original.apply(this, arguments);
        window.setTimeout(function () {
          capturePage("spa");
        }, 0);
        return result;
      };
    }

    function describeTarget(target) {
      if (!target || typeof target !== "object") {
        return "unknown";
      }
      const el = target.closest ? target.closest("[data-track]") : null;
      if (el && el.getAttribute) {
        const track = el.getAttribute("data-track");
        if (track) {
          return track.slice(0, 64);
        }
      }
      if (target.getAttribute) {
        const dataTrack = target.getAttribute("data-track");
        if (dataTrack) {
          return dataTrack.slice(0, 64);
        }
      }
      if (target.id) {
        return (target.tagName || "element").toLowerCase() + "#" + String(target.id).slice(0, 48);
      }
      const role = target.getAttribute ? target.getAttribute("role") : "";
      if (role) {
        return (target.tagName || "element").toLowerCase() + "[" + role + "]";
      }
      return (target.tagName || "element").toLowerCase();
    }

    function viewport() {
      return {
        w: Math.round(window.innerWidth || document.documentElement.clientWidth || 0),
        h: Math.round(window.innerHeight || document.documentElement.clientHeight || 0),
        dpr: Number(window.devicePixelRatio || 1),
      };
    }

    function currentScrollMetrics() {
      const doc = document.documentElement;
      const body = document.body;
      const scrollTop = window.pageYOffset || doc.scrollTop || body.scrollTop || 0;
      const scrollHeight = Math.max(
        body.scrollHeight || 0,
        doc.scrollHeight || 0,
        body.offsetHeight || 0,
        doc.offsetHeight || 0,
        body.clientHeight || 0,
        doc.clientHeight || 0
      );
      const viewportHeight = window.innerHeight || doc.clientHeight || 0;
      const denominator = scrollHeight - viewportHeight;
      let depth = 0;
      if (denominator <= 0) {
        depth = 100;
      } else {
        depth = Math.min(100, Math.round(((scrollTop + viewportHeight) / scrollHeight) * 100));
      }
      return {
        scrollTop: Math.round(scrollTop),
        scrollHeight: Math.round(scrollHeight),
        viewportHeight: Math.round(viewportHeight),
        depth: depth,
      };
    }

    function pointerCoords() {
      return {
        x: state.pointer.x === null ? null : Math.round(state.pointer.x),
        y: state.pointer.y === null ? null : Math.round(state.pointer.y),
      };
    }

    function dispatchScroll(trigger) {
      const metrics = currentScrollMetrics();
      const depthValue = Math.max(state.maxDepth, metrics.depth);
      state.maxDepth = depthValue;
      if (
        trigger === "visibility" &&
        secondsSinceStart() <= 1 &&
        metrics.scrollTop === 0 &&
        depthValue === 0
      ) {
        return;
      }
      send("scroll", {
        trigger: trigger,
        depth: depthValue,
        sec: secondsSinceStart(),
        coords: {
          x: 0,
          y: metrics.scrollTop,
        },
        scroll_top: metrics.scrollTop,
        scroll_height: metrics.scrollHeight,
        viewport_height: metrics.viewportHeight,
      });
    }

    function captureClose(trigger) {
      if (state.closeSent) {
        return;
      }
      state.closeSent = true;
      const coords = pointerCoords();
      send("close", {
        trigger: trigger,
        depth: Math.round(state.maxDepth),
        sec: secondsSinceStart(),
        coords: coords,
      });
    }

    function send(type, extra) {
      const payload = Object.assign(
        {
          site: cfg.site,
          type: type,
          route: window.location.pathname || "/",
          path: window.location.pathname || "/",
          url: window.location.href,
          ref: document.referrer || "",
          title: document.title || "",
          uid: state.uid,
          sid: state.sid,
          vp: viewport(),
          depth: Math.round(state.maxDepth),
          sec: secondsSinceStart(),
          ts: Date.now(),
        },
        extra || {}
      );

      try {
        if (window.console && typeof window.console.debug === "function") {
          window.console.debug("logflow ->", endpoint, payload);
        }
      } catch (_) {
        // ignore logging errors
      }

      const body = JSON.stringify(payload);
      try {
        const blob = new Blob([body], { type: "application/json" });
        if (navigator.sendBeacon && navigator.sendBeacon(endpoint, blob)) {
          return;
        }
      } catch (_) {
        // ignore
      }
      fetch(endpoint, {
        method: "POST",
        body: body,
        keepalive: true,
        mode: "cors",
        credentials: "omit",
        headers: {
          "Content-Type": "application/json",
        },
      }).catch(function () {
        // swallow network errors
      });
    }

    function parseBinCount(raw, fallback) {
      const value = parseInt(raw || "", 10);
      if (!Number.isInteger(value) || value <= 0) {
        return fallback;
      }
      return value;
    }

    function toBin(coord, size, bins) {
      if (!Number.isFinite(coord) || !Number.isFinite(size) || size <= 0 || bins <= 0) {
        return null;
      }
      let ratio = coord / size;
      if (!Number.isFinite(ratio)) {
        return null;
      }
      ratio = Math.min(Math.max(ratio, 0), 1);
      let index = Math.floor(ratio * bins);
      if (index >= bins) {
        index = bins - 1;
      }
      return index;
    }

    function resolveSection(target) {
      if (!target || typeof target !== "object") {
        return "";
      }
      const el = target.closest ? target.closest("[data-section]") : null;
      if (el && el.getAttribute) {
        const value = el.getAttribute("data-section");
        if (value) {
          return value.slice(0, 64);
        }
      }
      return "";
    }

    function elementText(target) {
      if (!target || !target.textContent) {
        return "";
      }
      const text = target.textContent.trim();
      return text.slice(0, 120);
    }
  } catch (err) {
    if (window && window.console && window.console.debug) {
      window.console.debug("logflow snippet error", err);
    }
  }
})();
