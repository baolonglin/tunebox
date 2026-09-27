/*
 * tunebox remote (PLAN.md Phase 4) — search, ingest progress, now-playing, transport, one screen.
 *
 * Three rules this file follows, each for a reason worth writing down.
 *
 * 1. **Nothing untrusted is ever assigned to innerHTML.** Every string on this page came from
 *    somewhere else: track titles come from YouTube by way of yt-dlp, and file names come off a
 *    disk anyone in the house can write to. A remote that interpolated them into markup would
 *    hand whoever uploaded a video called `<img onerror=…>` a script-execution primitive inside
 *    a page that holds the API token. So the DOM is built from createElement/textContent, and the
 *    one helper that sets markup (`clear`) only ever removes nodes.
 *
 * 2. **The server is the only source of truth about playback.** The buttons do not guess what the
 *    next state will be; they send a command and redraw from the response. This matters because
 *    the session belongs to the host (PLAN.md 2) and there can be two phones, OwnTone's own web UI
 *    and the soundbar's remote all touching it — optimistic local state would be wrong regularly
 *    and confusingly rather than rarely.
 *
 * 3. **Polling stops when the page is hidden.** A phone in a pocket must not spend its battery
 *    asking about a track it is not showing. The Page Visibility API is how a web remote gets to be
 *    nearly free when idle, and it is also why there is no websocket here: the data is two small
 *    JSON documents, and a socket would be a connection to keep alive for no gain.
 */

(() => {
  "use strict";

  const CONST = document.body.dataset;
  const TOKEN_KEY = "tunebox.token";
  const POLL_MS = 2000;

  /** Elements are looked up once: this page's DOM is static, only its contents change. */
  const el = (id) => document.getElementById(id);

  const state = {
    token: null,
    tab: "now",
    player: null,
    /** Locally ticked between polls so the progress bar moves at one frame a second, not one per poll. */
    progressMs: 0,
    /** True while a volume drag is in flight, so a poll cannot yank the slider out from under a thumb. */
    holdingVolume: false,
    jobsActive: false,
    timer: null,
    ticker: null,
  };

  // ----------------------------------------------------------------- DOM helpers

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function button(label, onClick, className) {
    const node = make("button", className, label);
    node.type = "button";
    node.addEventListener("click", onClick);
    return node;
  }

  /** One list row: a title, a subtitle, and some buttons. Every list on this page is this shape. */
  function row(title, subtitle, actions, className) {
    const li = make("li", className);
    const main = make("div", "row-main");
    main.appendChild(make("span", "row-title", title));
    if (subtitle) main.appendChild(make("span", "row-sub", subtitle));
    li.appendChild(main);
    if (actions && actions.length) {
      const box = make("div", "row-actions");
      actions.forEach((a) => box.appendChild(a));
      li.appendChild(box);
    }
    return li;
  }

  function fill(list, items, emptyText) {
    clear(list);
    if (!items.length) {
      const li = make("li", "empty", emptyText);
      list.appendChild(li);
      return;
    }
    items.forEach((item) => list.appendChild(item));
  }

  function duration(seconds) {
    if (!seconds && seconds !== 0) return "–:––";
    const total = Math.max(0, Math.round(seconds));
    const mins = Math.floor(total / 60);
    return `${mins}:${String(total % 60).padStart(2, "0")}`;
  }

  function banner(message) {
    const node = el("banner");
    node.textContent = message || "";
    node.hidden = !message;
  }

  // --------------------------------------------------------------------- the API

  class ApiError extends Error {
    constructor(status, message) {
      super(message);
      this.status = status;
    }
  }

  async function api(path, options = {}) {
    const init = {
      method: options.method || "GET",
      headers: { "X-Tunebox-Token": state.token, Accept: "application/json" },
      // The token is in a header, so a cross-site page cannot make the browser send it for us;
      // saying so explicitly keeps that true if a future route ever sets a cookie.
      credentials: "omit",
    };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }

    let response;
    try {
      response = await fetch(path, init);
    } catch (cause) {
      // Offline, or the host went away mid-song. Not an API error: there was no answer at all.
      throw new ApiError(0, "cannot reach tunebox");
    }
    if (response.status === 401) {
      // The token was rejected — it changed, or this device has a stale one. Drop it and ask again
      // rather than looping on 401s for as long as the page is open.
      forgetToken("That token was rejected.");
      throw new ApiError(401, "unauthorized");
    }
    if (response.status === 204) return null;

    let payload = null;
    try {
      payload = await response.json();
    } catch (cause) {
      payload = null;
    }
    if (!response.ok) {
      const detail = payload && payload.error ? payload.error : `HTTP ${response.status}`;
      throw new ApiError(response.status, detail);
    }
    return payload;
  }

  /** Run an API call, showing its failure in the banner instead of losing it to the console. */
  async function attempt(work, what) {
    try {
      const result = await work();
      banner("");
      return result;
    } catch (error) {
      if (error.status !== 401) banner(`${what}: ${error.message}`);
      return undefined;
    }
  }

  // ------------------------------------------------------------------ the token

  function readStoredToken() {
    try {
      return window.localStorage.getItem(TOKEN_KEY);
    } catch (cause) {
      // Private browsing, or storage disabled. The remote still works for this session.
      return null;
    }
  }

  function storeToken(token) {
    try {
      window.localStorage.setItem(TOKEN_KEY, token);
    } catch (cause) {
      /* not fatal — see readStoredToken */
    }
  }

  /**
   * A token handed over in the URL *fragment*, e.g. http://host:8080/#token=…
   *
   * The fragment specifically, never a query string: a fragment is not sent to the server, so it
   * stays out of uvicorn's access log — and that log is the audit trail PLAN.md 3.2 asks for on a
   * network full of devices nobody is proud of. Writing the shared secret into it to save a paste
   * would be a poor trade. It is removed from the address bar as soon as it has been read.
   */
  function tokenFromFragment() {
    const hash = window.location.hash || "";
    const match = /(?:^#|&)token=([^&]+)/.exec(hash);
    if (!match) return null;
    let token;
    try {
      token = decodeURIComponent(match[1]);
    } catch (cause) {
      return null;
    }
    history.replaceState(null, "", window.location.pathname + window.location.search);
    return token.trim() || null;
  }

  function forgetToken(message) {
    state.token = null;
    try {
      window.localStorage.removeItem(TOKEN_KEY);
    } catch (cause) {
      /* see readStoredToken */
    }
    stopPolling();
    showGate(message || "");
  }

  /**
   * Show the token card. ``undefined`` leaves whatever error is already on screen alone, which
   * matters because two code paths reach here in sequence: ``api()`` clears a rejected token and
   * explains why, and then the caller that was awaiting it also wants the gate up. Passing an empty
   * string from the second would wipe the only useful thing on the page.
   */
  function showGate(message) {
    el("app").hidden = true;
    el("gate").hidden = false;
    if (message !== undefined) {
      const error = el("gate-error");
      error.textContent = message;
      error.hidden = !message;
    }
    el("gate-token").focus();
  }

  async function enter(token) {
    const candidate = (token || "").trim();
    if (!candidate) return false;
    state.token = candidate;
    try {
      await api("/healthz");
    } catch (error) {
      state.token = null;
      if (error.status !== 401) showGate(`Could not check the token: ${error.message}`);
      return false;
    }
    storeToken(candidate);
    el("gate").hidden = true;
    el("app").hidden = false;
    el("gate-token").value = "";
    startPolling();
    loadLibrary();
    return true;
  }

  // --------------------------------------------------------------------- tabs

  const TABS = ["now", "add", "library"];

  function selectTab(name) {
    state.tab = name;
    TABS.forEach((tab) => {
      const trigger = el(`tab-${tab}`);
      const panel = el(`panel-${tab}`);
      const active = tab === name;
      trigger.setAttribute("aria-selected", active ? "true" : "false");
      // Roving tabindex: one stop for the whole tablist, arrows move between the tabs inside it.
      trigger.tabIndex = active ? 0 : -1;
      panel.hidden = !active;
    });
    if (name === "library" && !el("library").childElementCount) loadLibrary();
    if (name === "add") refreshJobs();
  }

  function wireTabs() {
    TABS.forEach((tab, index) => {
      const trigger = el(`tab-${tab}`);
      trigger.addEventListener("click", () => selectTab(tab));
      trigger.addEventListener("keydown", (event) => {
        const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
        if (!step) return;
        event.preventDefault();
        const next = TABS[(index + step + TABS.length) % TABS.length];
        selectTab(next);
        el(`tab-${next}`).focus();
      });
    });
  }

  // ------------------------------------------------------------- now playing

  function renderPlayer(player) {
    state.player = player;
    state.progressMs = player.item_progress_ms || 0;

    const now = player.now_playing;
    el("np-title").textContent = now && now.title ? now.title : "Nothing loaded";
    const sub = now ? [now.artist, now.album].filter(Boolean).join(" · ") : "";
    el("np-artist").textContent = sub;

    drawProgress();

    const playing = player.state === "play";
    const toggle = el("btn-toggle");
    // Glyph *and* label: colour alone would leave a screen reader with nothing, and a play/pause
    // button whose icon is the only state signal is the classic failure of a hand-built player.
    toggle.textContent = playing ? "⏸" : "▶";
    toggle.setAttribute("aria-label", playing ? "Pause" : "Play");

    if (!state.holdingVolume && player.volume !== null && player.volume !== undefined) {
      el("vol").value = String(player.volume);
      el("vol-out").textContent = String(player.volume);
    }

    const speaker = el("speaker-state");
    const select = el("btn-select");
    if (!player.output) {
      speaker.textContent = "Speaker: not visible on the network";
      select.hidden = true;
    } else if (player.output_selected) {
      speaker.textContent = `Speaker: ${player.output.name} — on`;
      select.hidden = true;
    } else {
      speaker.textContent = `Speaker: ${player.output.name} — off`;
      select.hidden = false;
    }

    const queue = player.queue || [];
    el("queue-count").textContent = queue.length ? `(${queue.length})` : "";
    el("btn-clear").disabled = queue.length === 0;
    fill(
      el("queue"),
      queue.map((item) =>
        row(
          item.title || item.path || "(untitled)",
          [item.artist, item.album].filter(Boolean).join(" · "),
          [],
          item.id === player.item_id ? "current" : ""
        )
      ),
      "The queue is empty."
    );
  }

  function drawProgress() {
    const player = state.player;
    const length = (player && player.item_length_ms) || 0;
    const position = Math.min(state.progressMs, length || state.progressMs);
    const percent = length > 0 ? Math.min(100, (position / length) * 100) : 0;
    el("np-bar").style.width = `${percent}%`;
    el("np-track").setAttribute("aria-valuenow", String(Math.round(percent)));
    el("np-elapsed").textContent = duration(position / 1000);
    el("np-length").textContent = length ? duration(length / 1000) : "–:––";
  }

  function command(path, body) {
    return attempt(async () => {
      const player = await api(path, { method: "PUT", body });
      if (player) renderPlayer(player);
    }, "player");
  }

  function wireTransport() {
    el("btn-toggle").addEventListener("click", () => command("/player/toggle"));
    el("btn-prev").addEventListener("click", () => command("/player/previous"));
    el("btn-next").addEventListener("click", () => command("/player/next"));
    el("btn-stop").addEventListener("click", () => command("/player/stop"));
    el("btn-select").addEventListener("click", () => command("/player/output/select"));

    el("btn-volup").addEventListener("click", () => command("/player/volume", { step: 5 }));
    el("btn-voldown").addEventListener("click", () => command("/player/volume", { step: -5 }));

    const slider = el("vol");
    slider.addEventListener("input", () => {
      state.holdingVolume = true;
      el("vol-out").textContent = slider.value;
    });
    // On `change`, not `input`: a drag across the slider fires input for every pixel, and each one
    // would be a request to the daemon. The number beside it updates live regardless.
    slider.addEventListener("change", async () => {
      await command("/player/volume", { volume: Number(slider.value) });
      state.holdingVolume = false;
    });

    el("btn-clear").addEventListener("click", () =>
      attempt(async () => {
        await api("/player/queue", { method: "DELETE" });
        await refreshPlayer();
      }, "queue")
    );
  }

  async function refreshPlayer() {
    try {
      const player = await api("/player");
      renderPlayer(player);
      banner("");
    } catch (error) {
      if (error.status === 401) return;
      // 503 here is the honest answer and the common one: OwnTone is not running, or was restarted.
      // Ingest is unaffected, which is why this is a banner rather than a takeover.
      banner(
        error.status === 503
          ? "The player is not answering — is OwnTone running?"
          : `player: ${error.message}`
      );
    }
  }

  // ------------------------------------------------------------ search + ingest

  /** A link, or a bare 11-character video id — the two things POST /ingest accepts directly. */
  const LOOKS_LIKE_URL = /^(?:https?:\/\/|www\.)\S+$/i;
  const LOOKS_LIKE_ID = /^[A-Za-z0-9_-]{11}$/;

  function wireSearch() {
    el("search-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const query = el("q").value.trim();
      if (!query) return;
      if (LOOKS_LIKE_URL.test(query) || LOOKS_LIKE_ID.test(query)) {
        // One field that does the right thing, because "paste a link" and "search for a song" are
        // the same intent on a phone and asking which one it was would be a question with an
        // obvious answer.
        const job = await ingest(query);
        if (job) el("q").value = "";
        return;
      }
      el("search-note").textContent = "Searching…";
      clear(el("results"));
      const results = await attempt(
        () => api(`/search?q=${encodeURIComponent(query)}`),
        "search"
      );
      if (!results) {
        el("search-note").textContent = "";
        return;
      }
      el("search-note").textContent = results.length
        ? `${results.length} result${results.length === 1 ? "" : "s"}`
        : "No results.";
      fill(
        el("results"),
        results.map((item) =>
          row(
            item.title,
            [item.uploader, duration(item.duration)].filter(Boolean).join(" · "),
            [button("Add", () => ingest(item.url), "primary")]
          )
        ),
        "No results."
      );
    });
  }

  async function ingest(url) {
    const job = await attempt(() => api("/ingest", { method: "POST", body: { url } }), "ingest");
    if (job) {
      el("jobs-note").textContent = `Queued ${job.video_id}.`;
      selectTab("add");
      await refreshJobs();
    }
    return job;
  }

  const ACTIVE = new Set(["queued", "running"]);

  async function refreshJobs() {
    const jobs = await attempt(() => api("/jobs?limit=15"), "jobs");
    if (!jobs) return;
    state.jobsActive = jobs.some((job) => ACTIVE.has(job.state));
    fill(
      el("jobs"),
      jobs.map((job) => {
        const title = job.tags && job.tags.title ? job.tags.title : job.video_id;
        const artist = job.tags && job.tags.artist ? job.tags.artist : "";
        const li = row(title, artist || job.url, []);
        const main = li.firstChild;

        const label = make("span", `state ${job.state}`, jobLabel(job));
        main.appendChild(label);

        if (job.state === "running" || (job.state === "queued" && job.progress)) {
          const bar = make("div", "job-bar");
          const inner = make("div");
          inner.style.width = `${Math.min(100, Math.max(0, job.progress || 0))}%`;
          bar.appendChild(inner);
          main.appendChild(bar);
        }
        if (job.state === "done" && job.path) {
          // The reason this list is on the same screen as the transport: the thing you want to do
          // with a track that just finished downloading is play it.
          li.appendChild(
            (() => {
              const box = make("div", "row-actions");
              box.appendChild(
                button("Play", () => enqueue(job.path, "replace", true), "primary")
              );
              box.appendChild(button("Queue", () => enqueue(job.path, "append", false)));
              return box;
            })()
          );
        }
        if (job.state === "queued") {
          li.appendChild(
            (() => {
              const box = make("div", "row-actions");
              box.appendChild(
                button("Cancel", () =>
                  attempt(async () => {
                    await api(`/jobs/${encodeURIComponent(job.id)}`, { method: "DELETE" });
                    await refreshJobs();
                  }, "cancel")
                )
              );
              return box;
            })()
          );
        }
        return li;
      }),
      "Nothing ingested yet."
    );
  }

  function jobLabel(job) {
    if (job.state === "running") {
      return job.progress ? `running ${Math.round(job.progress)}%` : "running";
    }
    if (job.state === "failed") return `failed — ${job.error || "unknown error"}`;
    if (job.state === "done" && job.already_present) return "already in the library";
    if (job.state === "done" && job.warnings && job.warnings.length) {
      return `done — ${job.warnings[0]}`;
    }
    return job.state;
  }

  // -------------------------------------------------------------------- library

  /**
   * Queue one track. `path` is whatever the API gave us for it: `GET /library` answers with paths
   * relative to the music root, `GET /jobs` with the absolute path the ingest wrote. Both are
   * accepted and both are re-checked against the music root server-side, so traversal is not
   * filtered here — it is impossible there.
   */
  function enqueue(path, mode, start) {
    return attempt(async () => {
      await api("/player/queue", {
        method: "POST",
        body: { paths: [path], mode, start },
      });
      await refreshPlayer();
      selectTab("now");
    }, "queue");
  }

  async function loadLibrary() {
    const query = el("lq").value.trim();
    const note = el("lib-note");
    note.textContent = "Reading the library…";
    const page = await attempt(
      () => api(`/library?limit=100${query ? `&q=${encodeURIComponent(query)}` : ""}`),
      "library"
    );
    if (!page) {
      note.textContent = "";
      return;
    }
    const shown = page.tracks.length;
    note.textContent = page.truncated
      ? `${shown} of ${page.total}+ tracks — the library is larger than one listing.`
      : `${shown} of ${page.total} track${page.total === 1 ? "" : "s"}, newest first.`;
    fill(
      el("library"),
      page.tracks.map((track) =>
        row(
          track.title,
          [track.artist, track.album].filter(Boolean).join(" · "),
          [
            button("Play", () => enqueue(track.path, "replace", true), "primary"),
            button("Queue", () => enqueue(track.path, "append", false)),
          ]
        )
      ),
      query ? "Nothing matches that." : "The library is empty — add something first."
    );
  }

  function wireLibrary() {
    el("lib-form").addEventListener("submit", (event) => {
      event.preventDefault();
      loadLibrary();
    });
  }

  // -------------------------------------------------------------------- polling

  function startPolling() {
    stopPolling();
    tick();
    state.ticker = window.setInterval(() => {
      if (state.player && state.player.state === "play" && state.player.item_length_ms) {
        state.progressMs += 1000;
        drawProgress();
      }
    }, 1000);
  }

  function stopPolling() {
    if (state.timer) window.clearTimeout(state.timer);
    if (state.ticker) window.clearInterval(state.ticker);
    state.timer = state.ticker = null;
  }

  async function tick() {
    if (!state.token) return;
    await refreshPlayer();
    // Jobs are only worth polling while something is in flight or the ingest tab is on screen.
    if (state.jobsActive || state.tab === "add") await refreshJobs();
    if (!state.token) return;
    state.timer = window.setTimeout(tick, POLL_MS);
  }

  function wireVisibility() {
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") {
        stopPolling();
      } else if (state.token) {
        // Redraw immediately rather than after a poll interval: coming back to a stale screen and
        // watching it correct itself is worse than a blank moment.
        startPolling();
      }
    });
  }

  // ------------------------------------------------------------------ the rest

  function wireFooter() {
    const link = el("owntone-link");
    // Built here rather than on the server because the server does not know the address the phone
    // used to reach it. OwnTone normally listens on loopback, so a link to the *configured* URL
    // would point the phone at itself; the host this page came from is the right one.
    const url =
      CONST.owntoneUrl ||
      `${window.location.protocol}//${window.location.hostname}:${CONST.playerPort}/`;
    link.href = url;
    link.target = "_blank";
    el("btn-forget").addEventListener("click", () => forgetToken("Token removed from this device."));
  }

  function registerWorker() {
    if (!("serviceWorker" in navigator)) return;
    // Scope "/" is why the worker is served from the root rather than from /ui/: a worker's default
    // scope is its own directory, and one under /ui/ could not cache the page at "/".
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {
      /* An unavailable worker costs offline caching and nothing else. */
    });
  }

  function init() {
    wireTabs();
    wireTransport();
    wireSearch();
    wireLibrary();
    wireFooter();
    wireVisibility();
    registerWorker();

    el("gate-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      await enter(el("gate-token").value);
    });

    const token = tokenFromFragment() || readStoredToken();
    if (token) {
      enter(token).then((ok) => {
        // `undefined`, not "": a rejected token has already explained itself via forgetToken.
        if (!ok && !state.token) showGate(undefined);
      });
    } else {
      showGate("");
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
