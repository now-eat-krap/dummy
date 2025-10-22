(function () {
  try {
    var script = document.currentScript;
    if (!script || typeof window === "undefined") {
      return;
    }

    var endpointHint = (script.dataset.endpoint || "").trim();
    if (!endpointHint) {
      endpointHint = "/snapshot/request";
    }
    var endpoint = resolveEndpoint(endpointHint, script.src || window.location.href);
    if (!endpoint) {
      return;
    }

    var site = (script.dataset.site || "").trim();
    var snapshot = (script.dataset.snapshot || "default").trim();
    var vp = (script.dataset.vp || "").trim();
    var grid = (script.dataset.grid || "").trim();
    var section = (script.dataset.section || "").trim();
    var route = normalizeRoute(window.location.pathname || "/");

    var dedupeKey = ["logflow:snapshot", site || "default", snapshot || "default", route, vp, grid, section].join(":");
    var today = new Date().toISOString().slice(0, 10);
    try {
      if (window.localStorage) {
        var last = window.localStorage.getItem(dedupeKey);
        if (last === today) {
          return;
        }
        window.localStorage.setItem(dedupeKey, today);
      }
    } catch (_) {
      /* ignore storage errors */
    }

    var payload = {
      url: window.location.href,
      site: site || undefined,
      route: route,
      snapshot: snapshot || undefined,
      vp: vp || undefined,
      grid: grid || undefined,
      section: section || undefined,
    };

    var body = JSON.stringify(payload);
    if (navigator.sendBeacon) {
      var blob = new Blob([body], { type: "application/json" });
      navigator.sendBeacon(endpoint, blob);
    } else {
      fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body,
        keepalive: true,
      }).catch(function () {
        /* ignore network errors */
      });
    }
  } catch (err) {
    console.debug("logflow snapshot beacon failed", err);
  }

  function normalizeRoute(pathname) {
    if (!pathname) {
      return "/";
    }
    var path = String(pathname).split("?")[0];
    if (path.charAt(0) !== "/") {
      path = "/" + path;
    }
    path = path.replace(/\/([0-9]+|[0-9a-fA-F]{12,})/g, "/:id");
    if (path.length > 1 && path.endsWith("/")) {
      path = path.slice(0, -1);
    }
    return path || "/";
  }

  function resolveEndpoint(raw, baseUrl) {
    try {
      if (/^https?:\/\//i.test(raw)) {
        return raw;
      }
      return new URL(raw, baseUrl).toString();
    } catch (_) {
      return "";
    }
  }
})();
