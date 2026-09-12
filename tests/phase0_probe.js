(function () {
  if (window.ShirabePhase0) return;
  const events = [];
  const storageKey = "shirabe.phase0.persistence.v1";
  const originalFetch = window.fetch.bind(window);
  const write = (event) => {
    events.push({ at: new Date().toISOString(), ...event });
    console.log("SHIRABE_PHASE0 " + JSON.stringify(event));
  };
  window.fetch = async function (input, init) {
    const url = typeof input === "string" ? input : input.url;
    const request = { kind: "fetch", url, method: init?.method || "GET", body: typeof init?.body === "string" ? init.body : null };
    const start = performance.now();
    try {
      const result = await originalFetch(input, init);
      write({ ...request, status: result.status, ms: performance.now() - start });
      return result;
    } catch (error) {
      write({ ...request, error: String(error), ms: performance.now() - start });
      throw error;
    }
  };
  const display = (value) => {
    document.querySelectorAll("[data-phase0-result]").forEach((node) => {
      node.textContent = JSON.stringify(value, null, 2);
    });
  };
  const api = () => {
    if (typeof AnkiDroidJS !== "function") throw new Error("AnkiDroidJS is unavailable");
    return new AnkiDroidJS({ version: "0.0.3", developer: "https://github.com/davafons/shirabe" });
  };
  window.ShirabePhase0 = {
    events,
    attach() {
      write({ kind: "attach", origin: location.origin, case: document.querySelector("[data-phase0-case]")?.dataset.phase0Case });
    },
    async run() {
      const result = { userAgent: navigator.userAgent, href: location.href, baseURI: document.baseURI, origin: location.origin, online: navigator.onLine, localScript: true };
      try {
        result.previousStorage = localStorage.getItem(storageKey);
        result.storageToken = result.previousStorage || String(Date.now());
        localStorage.setItem(storageKey, result.storageToken);
        result.storageReadBack = localStorage.getItem(storageKey);
      } catch (error) {
        result.storageError = String(error);
      }
      for (const name of ["_shirabe_phase0_snapshot_v1.json", "_shirabe_phase0_invalid_v1.json", "_shirabe_phase0_missing_v1.json"]) {
        try {
          const response = await fetch(name);
          const entry = { status: response.status, type: response.headers.get("content-type") };
          try { entry.data = await response.json(); } catch (error) { entry.parseError = String(error); }
          result[name] = entry;
        } catch (error) {
          result[name] = { error: String(error) };
        }
      }
      if (typeof AnkiDroidJS === "function") {
        const native = api();
        result.nativeMethods = Object.getOwnPropertyNames(AnkiDroidJS.prototype).sort();
        for (const method of ["ankiGetCardId", "ankiGetCardNid", "ankiIsDisplayingAnswer"]) {
          try { result[method] = await native[method](); } catch (error) { result[method] = { error: String(error) }; }
        }
      }
      this.lastResult = result;
      display(result);
      write({ kind: "result", ...result });
      return result;
    },
    async search() {
      try {
        const result = await api().ankiSearchCard("tag:shirabe_phase0");
        write({ kind: "native-search", result });
        return result;
      } catch (error) {
        display({ nativeSearchError: String(error) });
        throw error;
      }
    },
  };
})();
