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
      heartbeat: parseInt(script.dataset.hb || "0", 10),
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

    if (!Number.isInteger(cfg.heartbeat) || cfg.heartbeat < 0) {
      cfg.heartbeat = 0;
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
      scrollFlushed: false,
      uid: ensureId(window.localStorage, "logflow:uid"),
      sid: ensureId(window.sessionStorage, "logflow:sid"),
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
          send("click", {
            element: describeTarget(target),
            depth: Math.round(state.maxDepth),
          });
        },
        true
      );
    }

    if (cfg.scroll) {
      window.addEventListener(
        "scroll",
        function () {
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
          const viewport = window.innerHeight || doc.clientHeight || 0;
          const denominator = scrollHeight - viewport;
          if (denominator <= 0) {
            state.maxDepth = 100;
            return;
          }
          const depth = Math.min(100, Math.round(((scrollTop + viewport) / scrollHeight) * 100));
          if (depth > state.maxDepth) {
            state.maxDepth = depth;
          }
        },
        { passive: true }
      );

      document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "hidden") {
          flushScroll("visibility");
        }
      });

      window.addEventListener("beforeunload", function () {
        flushScroll("unload");
      });
    }

    if (cfg.heartbeat > 0) {
      const intervalMs = cfg.heartbeat * 1000;
      window.setInterval(function () {
        send("heartbeat", { sec: secondsSinceStart() });
      }, intervalMs);
    }

    function capturePage(source) {
      state.pageStart = Date.now();
      state.maxDepth = 0;
      state.scrollFlushed = false;
      send("page", { source: source, depth: 0, sec: 0 });
    }

    function secondsSinceStart() {
      return Math.max(0, Math.round((Date.now() - state.pageStart) / 1000));
    }

    function flushScroll(trigger) {
      if (state.scrollFlushed) {
        return;
      }
      state.scrollFlushed = true;
      send("scroll", {
        trigger: trigger,
        depth: Math.round(state.maxDepth),
        sec: secondsSinceStart(),
      });
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

      const body = JSON.stringify(payload);
      try {
        if (navigator.sendBeacon && navigator.sendBeacon(endpoint, body)) {
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
  } catch (err) {
    if (window && window.console && window.console.debug) {
      window.console.debug("logflow snippet error", err);
    }
  }
})();
