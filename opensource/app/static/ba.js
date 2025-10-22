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
      snapshotUpload: script.dataset.snapshotUpload !== "false",
      snapshotEndpoint: (script.dataset.snapshotEndpoint || "").trim(),
    };
    cfg.snapshotHash = (script.dataset.snapshot || "").trim() || "default";
    cfg.gridId = (script.dataset.grid || "").trim();
    if (!cfg.gridId) {
      cfg.gridId = cfg.xBins + "x" + cfg.yBins;
    }

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

    try {
      const endpointUrl = new URL(cfg.endpoint, window.location.href);
      const snapshotBase = endpointUrl.href.endsWith("/") ? endpointUrl.href : endpointUrl.href + "/";
      if (!cfg.snapshotEndpoint) {
        const snapshotUrl = new URL("snapshot", snapshotBase);
        cfg.snapshotEndpoint = snapshotUrl.toString();
      } else {
        cfg.snapshotEndpoint = new URL(cfg.snapshotEndpoint, snapshotBase).toString();
      }
    } catch (_) {
      cfg.snapshotEndpoint = "";
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
        pageX: Math.round((window.pageXOffset || 0) + (window.innerWidth ? window.innerWidth / 2 : 0)),
        pageY: Math.round((window.pageYOffset || 0) + (window.innerHeight ? window.innerHeight / 2 : 0)),
      },
      scrollTimer: null,
      closeSent: false,
      lastPageUrl: "",
      lastPageSentAt: 0,
      routeNorm: normalizeRoute(window.location.pathname || "/"),
    };
    const snapshotState = {
      inFlightKey: null,
    };
    const SNAPSHOT_FLAG_PREFIX = "logflow:snapshot:";

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
            pageX: Math.round(ev.pageX || ev.clientX || 0),
            pageY: Math.round(ev.pageY || ev.clientY || 0),
          };
          const vp = viewport();
          const xBin = toBin(ev.clientX, vp.w, cfg.xBins);
          const yBin = toBin(ev.clientY, vp.h, cfg.yBins);
          const section = resolveSection(target);
          const docMetrics = documentMetrics();
          const docPos = relativeDocumentPosition(state.pointer.pageX, state.pointer.pageY, docMetrics);
          send("click", {
            element: describeTarget(target),
            element_text: elementText(target),
            el_hash: elementHash(target),
            depth: Math.round(state.maxDepth),
            coords: {
              x: Math.round(ev.clientX || 0),
              y: Math.round(ev.clientY || 0),
            },
            x_bin: xBin,
            y_bin: yBin,
            section: section,
            doc_x: docPos.x,
            doc_y: docPos.y,
            doc_w: docMetrics.width,
            doc_h: docMetrics.height,
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
            pageX: Math.round(ev.pageX || ev.clientX || 0),
            pageY: Math.round(ev.pageY || ev.clientY || 0),
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
            pageX: Math.round(ev.pageX || ev.clientX || 0),
            pageY: Math.round(ev.pageY || ev.clientY || 0),
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
      state.routeNorm = normalizeRoute(window.location.pathname || "/");
      send("page", { source: source, depth: 0, sec: 0 });
      scheduleSnapshotUpload();
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

    function documentMetrics() {
      const doc = document.documentElement;
      const body = document.body;
      const width = Math.max(
        doc ? doc.scrollWidth || 0 : 0,
        body ? body.scrollWidth || 0 : 0,
        doc ? doc.clientWidth || 0 : 0,
        body ? body.clientWidth || 0 : 0
      );
      const height = Math.max(
        doc ? doc.scrollHeight || 0 : 0,
        body ? body.scrollHeight || 0 : 0,
        doc ? doc.clientHeight || 0 : 0,
        body ? body.clientHeight || 0 : 0
      );
      return {
        width: Math.max(1, Math.round(width)),
        height: Math.max(1, Math.round(height)),
      };
    }

    function relativeDocumentPosition(pageX, pageY, metrics) {
      const dims = metrics || documentMetrics();
      if (pageX == null || pageY == null) {
        return { x: null, y: null };
      }
      const xRatio = clamp(pageX / dims.width, 0, 1);
      const yRatio = clamp(pageY / dims.height, 0, 1);
      return {
        x: Number.isFinite(xRatio) ? Number(xRatio.toFixed(4)) : null,
        y: Number.isFinite(yRatio) ? Number(yRatio.toFixed(4)) : null,
      };
    }

    function viewportBucket(vp) {
      const bucketSize = 120;
      const width = vp && Number.isFinite(vp.w) ? Math.max(0, vp.w) : 0;
      const height = vp && Number.isFinite(vp.h) ? Math.max(0, vp.h) : 0;
      const bucketW = Math.round(width / bucketSize) * bucketSize || Math.round(width || 0);
      const bucketH = Math.round(height / bucketSize) * bucketSize || Math.round(height || 0);
      return "vp-" + bucketW + "x" + bucketH;
    }

    function normalizeRoute(raw) {
      if (!raw) {
        return "/";
      }
      let path = String(raw).split("#", 1)[0].split("?", 1)[0];
      if (!path.startsWith("/")) {
        path = "/" + path;
      }
      path = path.replace(/\/([0-9]+|[0-9a-fA-F]{12,})(?=\/|$)/g, "/:id");
      if (path.length > 1) {
        path = path.replace(/\/+$/, "");
      }
      return path || "/";
    }

    function clamp(value, min, max) {
      if (!Number.isFinite(value)) {
        return min;
      }
      return Math.min(Math.max(value, min), max);
    }

    function elementHash(target) {
      if (!target || typeof target !== "object") {
        return "";
      }
      try {
        const segments = [];
        let node = target;
        let depth = 0;
        while (node && node !== document && depth < 6) {
          const tag = (node.tagName || "node").toLowerCase();
          let descriptor = tag;
          if (node.id) {
            descriptor += "#" + String(node.id).slice(0, 48);
          } else if (node.classList && node.classList.length) {
            descriptor += "." + Array.from(node.classList)
              .slice(0, 3)
              .map(function (cls) {
                return cls.slice(0, 24);
              })
              .join(".");
          }
          const siblingIndex = siblingPosition(node);
          descriptor += ":" + siblingIndex;
          segments.push(descriptor);
          node = node.parentElement;
          depth += 1;
        }
        if (!segments.length) {
          return "";
        }
        const raw = segments.join(">");
        return "el_" + simpleHash(raw);
      } catch (_) {
        return "";
      }
    }

    function siblingPosition(node) {
      if (!node || !node.parentElement) {
        return 0;
      }
      let index = 0;
      let current = node;
      while (current.previousElementSibling) {
        index += 1;
        current = current.previousElementSibling;
      }
      return index;
    }

    function simpleHash(input) {
      let hash = 0;
      const text = String(input);
      for (let i = 0; i < text.length; i += 1) {
        hash = (hash << 5) - hash + text.charCodeAt(i);
        hash |= 0;
      }
      return Math.abs(hash).toString(16);
    }

    function pointerCoords() {
      return {
        x: state.pointer.x === null ? null : Math.round(state.pointer.x),
        y: state.pointer.y === null ? null : Math.round(state.pointer.y),
        pageX: state.pointer.pageX === null ? null : Math.round(state.pointer.pageX),
        pageY: state.pointer.pageY === null ? null : Math.round(state.pointer.pageY),
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
      if (
        trigger === "scroll" &&
        secondsSinceStart() <= 1 &&
        metrics.scrollTop === 0 &&
        depthValue === 0
      ) {
        return;
      }
      const docMetrics = documentMetrics();
      const docRatio = metrics.scrollHeight > 0 ? clamp(metrics.scrollTop / metrics.scrollHeight, 0, 1) : 0;
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
        doc_y: docRatio,
        doc_w: docMetrics.width,
        doc_h: docMetrics.height,
      });
    }

    function captureClose(trigger) {
      if (state.closeSent) {
        return;
      }
      state.closeSent = true;
      const coords = pointerCoords();
      const docMetrics = documentMetrics();
      const docPos = relativeDocumentPosition(
        state.pointer.pageX == null ? 0 : state.pointer.pageX,
        state.pointer.pageY == null ? 0 : state.pointer.pageY,
        docMetrics
      );
      send("close", {
        trigger: trigger,
        depth: Math.round(state.maxDepth),
        sec: secondsSinceStart(),
        coords: coords,
        doc_x: docPos.x,
        doc_y: docPos.y,
        doc_w: docMetrics.width,
        doc_h: docMetrics.height,
      });
    }

    function send(type, extra) {
      const vpData = viewport();
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
          route_norm: state.routeNorm,
          snapshot_hash: cfg.snapshotHash,
          grid_id: cfg.gridId,
          vp_bucket: viewportBucket(vpData),
          vp: vpData,
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

    function scheduleSnapshotUpload() {
      if (!cfg.snapshotUpload || !cfg.snapshotEndpoint) {
        return;
      }
      const key = snapshotStorageKey();
      if (!key || hasSentSnapshot(key) || snapshotState.inFlightKey === key) {
        return;
      }
      const skeleton = captureSkeleton();
      if (!skeleton || !Array.isArray(skeleton.boxes) || skeleton.boxes.length === 0) {
        return;
      }
      snapshotState.inFlightKey = key;
      sendSnapshot(key, skeleton);
    }

    function sendSnapshot(storageKey, skeleton) {
      if (!cfg.snapshotEndpoint) {
        snapshotState.inFlightKey = null;
        return;
      }
      if (!skeleton || !Array.isArray(skeleton.boxes) || !skeleton.boxes.length) {
        snapshotState.inFlightKey = null;
        return;
      }
      const vpData = viewport();
      const payload = {
        site: cfg.site,
        route: window.location.pathname || "/",
        route_norm: state.routeNorm,
        snapshot_hash: cfg.snapshotHash,
        grid_id: cfg.gridId,
        vp_bucket: viewportBucket(vpData),
        section: "",
        skeleton: skeleton,
      };
      const body = JSON.stringify(payload);
      const onSuccess = function () {
        markSnapshotSent(storageKey);
        snapshotState.inFlightKey = null;
      };
      const onFailure = function () {
        snapshotState.inFlightKey = null;
      };
      try {
        const blob = new Blob([body], { type: "application/json" });
        if (navigator.sendBeacon && navigator.sendBeacon(cfg.snapshotEndpoint, blob)) {
          onSuccess();
          return;
        }
      } catch (_) {
        // ignore
      }
      fetch(cfg.snapshotEndpoint, {
        method: "POST",
        body: body,
        keepalive: true,
        mode: "cors",
        credentials: "omit",
        headers: {
          "Content-Type": "application/json",
        },
      })
        .then(function (response) {
          if (response && response.ok) {
            onSuccess();
          } else {
            onFailure();
          }
        })
        .catch(onFailure);
    }

    function captureSkeleton() {
      try {
        const dims = documentMetrics();
        const vp = viewport();
        const boxes = [];
        const seen = new WeakSet();
        const limit = 220;

        pushElement(document.querySelector("body"), "canvas", boxes, seen, dims, { minWidth: 120, minHeight: 120, priority: -1 });

        const rules = [
          { selector: "header", kind: "header", opts: { minHeight: 48 } },
          { selector: "nav", kind: "nav", opts: { minHeight: 48 } },
          { selector: "main", kind: "main", opts: { minHeight: 80 } },
          { selector: "footer", kind: "footer", opts: { minHeight: 48 } },
          { selector: "section, article", kind: "section", opts: { minHeight: 60 } },
          { selector: "aside", kind: "aside", opts: { minHeight: 60 } },
          { selector: "form", kind: "form", opts: { minHeight: 80 } },
          { selector: "img, picture, video, iframe, canvas", kind: "media", opts: { minHeight: 60, minWidth: 80 } },
          { selector: "button, [role='button'], input[type='button'], input[type='submit'], input[type='reset']", kind: "button", opts: { minHeight: 28, minWidth: 72 } },
          { selector: "input, textarea, select", kind: "input", opts: { minHeight: 28, minWidth: 72 } },
          { selector: "h1, h2, h3", kind: "heading", opts: { minHeight: 32, minWidth: 120 } },
          { selector: "h4, h5, h6, p", kind: "text", opts: { minHeight: 20, minWidth: 80 } },
          { selector: "ul, ol, dl", kind: "list", opts: { minHeight: 40, minWidth: 80 } },
          { selector: "table", kind: "table", opts: { minHeight: 80, minWidth: 120 } },
        ];

        rules.forEach(function (rule) {
          const nodes = document.querySelectorAll(rule.selector);
          for (let i = 0; i < nodes.length && boxes.length < limit; i += 1) {
            pushElement(nodes[i], rule.kind, boxes, seen, dims, rule.opts);
          }
        });

        scanContainers(document.body, boxes, seen, dims, 0, limit);

        const divCandidates = document.querySelectorAll("div");
        for (let i = 0; i < divCandidates.length && boxes.length < limit; i += 1) {
          const el = divCandidates[i];
          const kind = deriveSkeletonKind(el);
          if (kind === "panel" || kind === "card") {
            pushElement(el, kind, boxes, seen, dims, { minWidth: 120, minHeight: 60 });
          }
        }

        if (!boxes.length) {
          return null;
        }

        return {
          viewport: {
            width: Math.round(dims.width),
            height: Math.round(dims.height),
            dpr: vp.dpr || 1,
          },
          boxes: boxes.slice(0, limit),
          captured_at: Date.now(),
        };
      } catch (_) {
        return null;
      }
    }

    function scanContainers(node, boxes, seen, dims, depth, limit) {
      if (!node || depth > 3 || boxes.length >= limit) {
        return;
      }
      const children = node.children || [];
      for (let i = 0; i < children.length && boxes.length < limit; i += 1) {
        const child = children[i];
        if (!child || seen.has(child)) {
          continue;
        }
        const kind = deriveSkeletonKind(child);
        const opts =
          kind === "text"
            ? { minWidth: 60, minHeight: 10 }
            : { minWidth: depth === 0 ? 160 : 100, minHeight: depth === 0 ? 80 : 60 };
        pushElement(child, kind, boxes, seen, dims, opts);
        if (child.children && child.children.length && depth < 2) {
          scanContainers(child, boxes, seen, dims, depth + 1, limit);
        }
      }
    }

    function pushElement(el, preferredKind, boxes, seen, dims, opts) {
      if (!el || seen.has(el) || boxes.length >= 220) {
        return;
      }
      if (el === document.documentElement) {
        return;
      }
      if (el instanceof HTMLElement === false && el instanceof SVGElement === false) {
        return;
      }
      const rect = el.getBoundingClientRect();
      if (!rect || Number.isNaN(rect.width) || Number.isNaN(rect.height)) {
        return;
      }
      const minWidth = (opts && opts.minWidth) || 32;
      const minHeight = (opts && opts.minHeight) || 24;
      if (rect.width < minWidth && rect.height < minHeight) {
        return;
      }
      const style = window.getComputedStyle ? window.getComputedStyle(el) : null;
      if (style && (style.display === "none" || style.visibility === "hidden" || parseFloat(style.opacity || "1") === 0)) {
        return;
      }
      if (style && style.position === "fixed") {
        return;
      }
      const widthRatio = clamp(rect.width / Math.max(1, dims.width), 0, 1);
      const heightRatio = clamp(rect.height / Math.max(1, dims.height), 0, 1);
      if (widthRatio < 0.01 && heightRatio < 0.01) {
        return;
      }
      const scrollX = window.pageXOffset || document.documentElement.scrollLeft || 0;
      const scrollY = window.pageYOffset || document.documentElement.scrollTop || 0;
      const left = rect.left + scrollX;
      const top = rect.top + scrollY;
      const xRatio = clamp(left / Math.max(1, dims.width), 0, 1);
      const yRatio = clamp(top / Math.max(1, dims.height), 0, 1);

      const kind = preferredKind || deriveSkeletonKind(el);
      const label = deriveSkeletonLabel(el, kind);

      boxes.push({
        tag: (el.tagName || "node").toLowerCase(),
        kind: kind,
        x: Number(xRatio.toFixed(4)),
        y: Number(yRatio.toFixed(4)),
        w: Number(widthRatio.toFixed(4)),
        h: Number(heightRatio.toFixed(4)),
        label: label,
      });
      seen.add(el);
    }

    function deriveSkeletonKind(el) {
      if (!el || !el.tagName) {
        return "panel";
      }
      const tag = el.tagName.toLowerCase();
      const className = (el.className || "").toString().toLowerCase();
      if (tag === "header") return "header";
      if (tag === "nav") return "nav";
      if (tag === "main") return "main";
      if (tag === "footer") return "footer";
      if (tag === "aside") return "aside";
      if (tag === "form") return "form";
      if (tag === "button") return "button";
      if (tag === "input" || tag === "textarea" || tag === "select") return "input";
      if (tag === "img" || tag === "picture" || tag === "video" || tag === "iframe" || tag === "canvas") return "media";
      if (tag === "h1" || tag === "h2" || tag === "h3") return "heading";
      if (tag === "h4" || tag === "h5" || tag === "h6" || tag === "label") return "text";
      if (tag === "p" || tag === "span") return "text";
      if (tag === "ul" || tag === "ol" || tag === "dl" || tag === "li") return "list";
      if (tag === "table" || tag === "tbody" || tag === "thead") return "table";

      if (className.includes("button") || className.includes("btn")) return "button";
      if (className.includes("nav") || className.includes("menu")) return "nav";
      if (className.includes("hero") || className.includes("banner")) return "section";
      if (className.includes("card") || className.includes("panel") || className.includes("tile")) return "card";
      if (className.includes("form") || className.includes("field")) return "form";
      if (className.includes("footer")) return "footer";
      if (className.includes("sidebar")) return "aside";
      if (className.includes("list") || className.includes("items")) return "list";

      return "panel";
    }

    function deriveSkeletonLabel(el, kind) {
      if (!el) {
        return "";
      }
      const attrs = ["data-track", "data-section", "aria-label", "alt", "title", "placeholder", "name"];
      for (let i = 0; i < attrs.length; i += 1) {
        if (el.hasAttribute && el.hasAttribute(attrs[i])) {
          const value = el.getAttribute(attrs[i]);
          if (value) {
            return String(value).trim().slice(0, 36);
          }
        }
      }
      if (kind === "heading") {
        return "Heading";
      }
      if (kind === "button") {
        return "Button";
      }
      if (kind === "input") {
        return "Input";
      }
      if (kind === "media") {
        return "Media";
      }
      if (kind === "form") {
        return "Form";
      }
      if (kind === "nav") {
        return "Navigation";
      }
      if (kind === "footer") {
        return "Footer";
      }
      if (kind === "header") {
        return "Header";
      }
      if (kind === "aside") {
        return "Sidebar";
      }
      if (kind === "card") {
        return "Card";
      }
      if (kind === "list") {
        return "List";
      }
      if (kind === "table") {
        return "Table";
      }
      return "";
    }

    function snapshotStorageKey() {
      const vpTag = viewportBucket(viewport()) || "any";
      const parts = [cfg.site || "default", state.routeNorm || "/", cfg.snapshotHash || "default", cfg.gridId || "grid", vpTag];
      return SNAPSHOT_FLAG_PREFIX + parts.join("|");
    }

    function hasSentSnapshot(key) {
      if (!key || !window.localStorage) {
        return false;
      }
      try {
        return window.localStorage.getItem(key) === "1";
      } catch (_) {
        return false;
      }
    }

    function markSnapshotSent(key) {
      if (!key || !window.localStorage) {
        return;
      }
      try {
        window.localStorage.setItem(key, "1");
      } catch (_) {
        // ignore storage quota errors
      }
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
