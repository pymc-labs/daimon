// Daimon page runtime: window.daimon, and window.claude.use for pages written for Claude.
// Injected first into every page. The page runs in a sandboxed frame with no network;
// every call goes to the shell over postMessage, and the server checks it.
(() => {
  "use strict";
  const config = window.__daimon_boot;
  if (!config || window.daimon) return;
  // WebRTC reaches any host, past connect-src. CSP `webrtc 'block'` stops it only where
  // browsers support it (Chrome does not), so this is best effort: no constructors here,
  // and none in a child frame, which would bring its own. Frames the page makes without
  // an https address (srcdoc, about:blank) are removed, and the windows a frame element
  // hands out are stripped. What it cannot reach is in docs/artifacts.md.
  const RTC = ["RTCPeerConnection", "webkitRTCPeerConnection", "RTCDataChannel",
    "RTCRtpSender", "RTCRtpReceiver", "RTCIceCandidate", "RTCSessionDescription"];
  const hardened = new WeakSet();
  function harden(win) {
    try {
      for (const name of RTC) delete win[name];
      if (hardened.has(win)) return win;
      for (const kind of ["HTMLIFrameElement", "HTMLFrameElement", "HTMLObjectElement"]) {
        const proto = win[kind].prototype;
        for (const key of ["contentWindow", "contentDocument"]) {
          const get = Object.getOwnPropertyDescriptor(proto, key)?.get;
          if (get) Object.defineProperty(proto, key, { configurable: true, enumerable: true, get() {
            const value = get.call(this);
            if (value) harden(key === "contentWindow" ? value : value.defaultView);
            return value;
          } });
        }
      }
      hardened.add(win);
    } catch { /* a cross-origin window: nothing of ours to strip */ }
    return win;
  }
  harden(window);
  // Only an iframe with an https address stays; it still starts as about:blank, see the docs.
  const kept = (el) => /^(IFRAME|FRAME)$/.test(el.tagName) && !el.hasAttribute("srcdoc")
    && /^https:/i.test(el.src);
  const dropFrames = () => document.querySelectorAll("iframe, frame, object, embed")
    .forEach((el) => { if (!kept(el)) el.remove(); });
  new MutationObserver(dropFrames).observe(document, {
    childList: true, subtree: true, attributes: true, attributeFilter: ["src", "srcdoc"],
  });
  const POLL_MS = 3000;
  const FRAGMENT = /^#[A-Za-z0-9_.:-]{1,128}$/;
  const validHash = (hash) => hash === "" || FRAGMENT.exec(hash)?.[0] === hash;
  const framed = window.parent !== window;
  const pending = new Map();
  let lastId = 0;

  const fail = (code, message) => Object.assign(new Error(message), { code });
  const autoId = () => [...crypto.getRandomValues(new Uint8Array(10))]
    .map((b) => b.toString(16).padStart(2, "0")).join("");
  const json = (value) => (value === undefined ? null : JSON.parse(JSON.stringify(value)));
  const notFramed = () => Promise.reject(fail("unavailable", "Open this page at its /a/ address."));

  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (event.source !== window.parent || event.origin !== config.shellOrigin
        || !msg || typeof msg !== "object" || Array.isArray(msg) || msg.daimon !== 1) return;
    if (msg.nav === "hash") {
      if (typeof msg.hash === "string" && validHash(msg.hash)) location.hash = msg.hash;
      return;
    }
    const call = pending.get(msg.id);
    if (!call) return;
    if ("progress" in msg) {
      if (call.progress) call.progress(msg.progress);
      return;
    }
    pending.delete(msg.id);
    if (msg.error) call.reject(fail(msg.error.code, msg.error.message));
    else call.resolve(msg.result);
  });

  function send(message, progress) {
    if (!framed) return notFramed();
    return new Promise((resolve, reject) => {
      const id = ++lastId;
      pending.set(id, { resolve, reject, progress });
      window.parent.postMessage({ daimon: 1, id, ...message }, config.shellOrigin);
    });
  }

  // The server enforces these too; refusing here fails fast with a clear reason.
  const WRITES = new Set(["db.set", "db.add", "db.delete"]);
  function refusal(method) {
    const { data, events, ask } = config.artifact;
    if (method === "notify") return events ? null : "This page sends no events.";
    if (method === "ask" || method === "thread") return ask ? null : "This page cannot ask the agent.";
    if (data === "off") return "This page has no data.";
    return WRITES.has(method) && data !== "write" ? "This page's data is read-only." : null;
  }
  function call(method, params, progress) {
    const refused = refusal(method);
    if (refused) return Promise.reject(fail("forbidden", refused));
    return send({ method, params: json(params || {}) }, progress);
  }

  // onSnapshot: one poll of the data's revision for all listeners; each refetches
  // when it has not seen that revision. Transient errors back off and retry, as
  // Firestore does; only a refusal (forbidden, invalid, ...) ends a listener.
  const TRANSIENT = new Set(["unavailable", "rate_limited", "unauthenticated"]);
  const listeners = new Set();
  let timer = null;
  let failures = 0;
  function schedule(ms) {
    if (listeners.size && timer === null) timer = setTimeout(poll, ms);
  }
  function kick() {
    clearTimeout(timer);
    timer = null;
    schedule(0);
  }
  async function poll() {
    timer = null;
    if (listeners.size && document.visibilityState === "visible") {
      try {
        const { revision } = await call("db.revision");
        failures = 0;
        await Promise.all([...listeners].filter((l) => l.seen !== revision).map((l) => l.refresh()));
      } catch (error) {
        failures++;
        if (!TRANSIENT.has(error.code)) [...listeners].forEach((l) => l.fail(error));
      }
    }
    schedule(Math.min(POLL_MS * 2 ** failures, 60000));
  }
  document.addEventListener("visibilitychange", kick);
  // load() resolves to a server reply carrying `revision`; snapshot() shapes it for onNext.
  function listen(load, snapshot, onNext, onError) {
    const listener = {
      seen: null,
      loading: null, // one refetch at a time, even when a poll overlaps the first
      refresh: () => listener.loading || (listener.loading = load().then((reply) => {
        if (!listeners.has(listener)) return;
        listener.seen = reply.revision;
        onNext(snapshot(reply));
      }, (error) => {
        if (!TRANSIENT.has(error.code)) listener.fail(error);
      }).finally(() => { listener.loading = null; })),
      fail: (error) => {
        if (!listeners.delete(listener)) return;
        if (onError) onError(error);
        else console.error("daimon onSnapshot", error);
      },
    };
    listeners.add(listener);
    listener.refresh();
    schedule(POLL_MS);
    return () => { listeners.delete(listener); };
  }

  const docSnapshot = (id, data) => ({
    id,
    exists: data !== null,
    data: () => (data === null ? undefined : json(data)),
  });
  const querySnapshot = (result) => {
    const docs = result.docs.map((doc) => docSnapshot(doc.id, doc.data));
    return { docs, size: docs.length, empty: docs.length === 0, forEach: (fn) => docs.forEach(fn) };
  };

  function docRef(path) {
    const write = (data, mode) => call("db.set", { path, data, write: mode }).then(() => { kick(); });
    const load = () => call("db.get", { path });
    const snapshot = (r) => docSnapshot(r.id, r.data);
    return {
      id: path.slice(path.indexOf("/") + 1),
      path,
      get: () => load().then(snapshot),
      set: (data, options) => write(data, options && options.merge ? "merge" : "set"),
      update: (data) => write(data, "update"),
      delete: () => call("db.delete", { path }).then(() => { kick(); }),
      onSnapshot: (onNext, onError) => listen(load, snapshot, onNext, onError),
    };
  }
  function query(collection, orderBy, direction, limit) {
    const load = () => call("db.list", { collection, orderBy, direction, limit });
    return {
      orderBy: (field, dir) => query(collection, field, dir === "desc" ? "desc" : "asc", limit),
      limit: (n) => query(collection, orderBy, direction, n),
      get: () => load().then(querySnapshot),
      onSnapshot: (onNext, onError) => listen(load, querySnapshot, onNext, onError),
    };
  }
  const db = Object.freeze({
    doc: docRef,
    collection: (name) => ({
      ...query(name, null, "asc", null),
      doc: (id) => docRef(`${name}/${id === undefined ? autoId() : id}`),
      add: (data) => call("db.add", { collection: name, data }).then((r) => { kick(); return docRef(`${name}/${r.id}`); }),
    }),
  });

  const use = (name) => Promise.resolve(name === "db" && config.capabilities.includes("db") ? db : null);

  window.daimon = Object.freeze({
    artifact: Object.freeze({ ...config.artifact }),
    capabilities: Object.freeze([...config.capabilities]),
    use,
    me: () => Promise.resolve(config.viewer),
    notify: (event, data) => call("notify", { event, data }).then((r) => r.id),
    // Runs a turn as the viewer, in their own thread for this page. Call it from a click.
    ask: (text, options) => call("ask", { text }, options && options.onText),
    thread: () => call("thread").then((r) => r.messages),
    // Copies the page's shareable address, ending in hash (e.g. "#faq-3"), and
    // resolves to it. Call it from a click: clipboards need a user gesture.
    copyLink: (hash) => {
      if (typeof hash !== "string" || !validHash(hash)) {
        return Promise.reject(fail("invalid", "Use a section hash such as #pricing."));
      }
      return send({ nav: "copy", hash });
    },
  });
  window.claude = Object.freeze({ use });

  // Deep links: keep the shell's address bar on this page's #fragment, so a
  // reader can share /a/<id>/<slug>#section. The shell forwards it back on load.
  const sendHash = () => framed
    && window.parent.postMessage({ daimon: 1, nav: "hash", hash: location.hash }, config.shellOrigin);
  window.addEventListener("hashchange", sendHash);

  // The frame may neither navigate nor open popups: links and window.open go to the
  // shell, which asks the viewer before opening another site. #section links stay here.
  const openOutside = (href) => {
    const url = new URL(href, location.href);
    if (framed && /^(https?|mailto):$/.test(url.protocol)) {
      window.parent.postMessage({ daimon: 1, nav: "open", url: url.href }, config.shellOrigin);
    }
  };
  window.open = (href) => { if (href) openOutside(String(href)); return null; };
  window.addEventListener("click", (event) => {
    const link = event.target instanceof Element ? event.target.closest("a[href]") : null;
    if (!link || event.defaultPrevented || link.hasAttribute("download")) return;
    const url = new URL(link.href, location.href);
    if (url.href.split("#")[0] === location.href.split("#")[0]) return;
    event.preventDefault();
    openOutside(url.href);
  });

  // A thumbnail for the shell: the top of the page drawn as an SVG foreignObject, the way
  // dom-to-image (MIT) does it, with computed styles inlined. Images from other hosts and
  // web fonts do not load inside it, so the thumbnail shows the page's layout and text.
  const THUMB_W = 640, THUMB_H = 400, MAX_PNG = 200 * 1024;
  function inline(source, height) {
    const copy = source.cloneNode(false);
    if (source.nodeType !== Node.ELEMENT_NODE) return copy;
    if (/^(SCRIPT|NOSCRIPT|STYLE|LINK|IFRAME|TEMPLATE)$/.test(source.tagName)) return null;
    if (source.getBoundingClientRect().top + scrollY > height) return null;
    const style = getComputedStyle(source);
    for (const name of style) copy.style.setProperty(name, style.getPropertyValue(name));
    if (source instanceof HTMLCanvasElement) {
      try { const img = document.createElement("img"); img.src = source.toDataURL(); img.style.cssText = copy.style.cssText; return img; } catch { return copy; }
    }
    if (source instanceof HTMLInputElement || source instanceof HTMLTextAreaElement) copy.setAttribute("value", source.value);
    for (const child of source.childNodes) {
      const inner = inline(child, height);
      if (inner) copy.appendChild(inner);
    }
    return copy;
  }
  async function capture() {
    const width = Math.min(document.documentElement.clientWidth || 1280, 1280);
    const height = Math.round(width * THUMB_H / THUMB_W);
    const body = inline(document.body, height);
    const page = getComputedStyle(document.body).backgroundColor;
    const root = getComputedStyle(document.documentElement).backgroundColor;
    body.style.margin = getComputedStyle(document.body).margin;
    const html = new XMLSerializer().serializeToString(body);
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}"><foreignObject width="100%" height="100%"><div xmlns="http://www.w3.org/1999/xhtml" style="width:${width}px;height:${height}px;overflow:hidden;background:${root}">${html}</div></foreignObject></svg>`;
    const image = new Image();
    image.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
    await image.decode();
    for (const scale of [1, 0.75, 0.5]) {
      const canvas = document.createElement("canvas");
      canvas.width = THUMB_W * scale;
      canvas.height = THUMB_H * scale;
      const context = canvas.getContext("2d");
      context.fillStyle = page === "rgba(0, 0, 0, 0)" ? (root === "rgba(0, 0, 0, 0)" ? "#fff" : root) : page;
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      const png = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
      if (png && png.size <= MAX_PNG) return png;
    }
    return null;
  }
  window.addEventListener("message", (event) => {
    if (event.source !== window.parent || event.origin !== config.shellOrigin || event.data?.daimon !== 1
        || event.data.nav !== "capture") return;
    capture().then((png) => {
      if (png) window.parent.postMessage({ daimon: 1, nav: "thumbnail", png }, config.shellOrigin);
    }, (error) => console.warn("daimon thumbnail", error));
  });
})();
