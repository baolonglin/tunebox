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
    /** What the queue was last drawn from, so an unchanged poll leaves its nodes (and focus) alone. */
    queueKey: null,
    /** Locally ticked between polls so the progress bar moves at one frame a second, not one per poll. */
    progressMs: 0,
    /** True while a volume drag is in flight, so a poll cannot yank the slider out from under a thumb. */
    holdingVolume: false,
    jobsActive: false,
    /** Ids of ingests already seen as finished. ``null`` until the first poll — see noticeFinishedIngests. */
    finishedJobs: null,
    /**
     * The playlist names last read from GET /playlists.
     *
     * Cached because the Library panel needs them: a row's `⋯` offers "add to playlist", and a
     * dropdown that fetched its options when it opened would be a request per tap and an empty
     * select for the first moment of each one. Refreshed whenever the Playlists panel is drawn and
     * whenever one is created or deleted, which is every moment the set can have changed from here.
     */
    playlists: [],
    /**
     * Which playlist row is expanded, by name, or `null`.
     *
     * The one piece of state on this page the server has no opinion about — and it has to be held
     * here rather than in the DOM because every edit redraws the list: without it, removing a track
     * from a playlist collapsed the playlist you were editing.
     */
    openPlaylist: null,
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

  /**
   * One list row: a title, a subtitle, and some buttons. Every list on this page is this shape.
   *
   * ``activate`` makes the title itself the control, and when it is given the row's main area is a
   * real ``<button>`` rather than a ``<div>`` with a click handler on it. That is not a detail: a
   * handler on a div is unreachable by keyboard, invisible to a screen reader and does not respond
   * to Enter or Space, which is the standard way this exact pattern is got wrong. The caller is
   * expected to label it — see `queueRow`, where the label differs for the track already playing.
   */
  function row(title, subtitle, actions, className, activate) {
    const li = make("li", className);
    const main = activate ? make("button", "row-main") : make("div", "row-main");
    if (activate) {
      main.type = "button";
      main.addEventListener("click", activate);
    }
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

  /**
   * Replace a list's rows, keeping keyboard focus on the row it was on.
   *
   * Rebuilding destroys the focused node, and a browser sends focus to ``<body>`` when that
   * happens — so without this, a keyboard or screen-reader user on a queue row is thrown back to
   * the top of the page by the next poll. A row opts in by carrying ``data-item-id``; if the same
   * id is drawn again, its new node gets the focus. The id is compared rather than put into a
   * selector because it came from the daemon (rule 1).
   */
  function fill(list, items, emptyText) {
    const active = document.activeElement;
    const focusedId = active && list.contains(active) ? active.dataset.itemId : undefined;
    clear(list);
    if (!items.length) {
      const li = make("li", "empty", emptyText);
      list.appendChild(li);
      return;
    }
    items.forEach((item) => list.appendChild(item));
    if (focusedId) {
      const again = Array.from(list.querySelectorAll("[data-item-id]")).find(
        (node) => node.dataset.itemId === focusedId
      );
      if (again) again.focus();
    }
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
    // Up front rather than on the tab switch, because the *library* panel needs the names too: a
    // row's "add to playlist" must have something to offer the first time somebody opens one.
    loadPlaylists();
    return true;
  }

  // --------------------------------------------------------------------- tabs

  const TABS = ["now", "add", "library", "playlists"];

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
    // Unconditionally, *not* "only if the list is empty": the library changes behind this page's
    // back on every finished ingest, and a cached-once listing is how a track that says "done" in
    // the Ingests list ends up missing from the Library panel. One directory walk per tab switch is
    // what the panel is for, and it is capped by TUNEBOX_LIBRARY_MAX_FILES.
    if (name === "library") refreshLibrary();
    if (name === "add") refreshJobs();
    // Same reasoning as the library: a playlist can have been created from a terminal, or by another
    // phone, since this panel was last drawn. Reading it is one small directory.
    if (name === "playlists") loadPlaylists();
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

  /**
   * The repeat cycle and its labels.
   *
   * `off → all → single → off` is the order the modes are wanted in, and the label is the *current*
   * mode rather than the next one: a button that reads "Repeat one song" and turns repeat off when
   * pressed is confusing, while one that reads the state and advances it is how every music player
   * on the phone this runs on behaves.
   *
   * Anything the daemon reports that is not one of the three — including the `null` of a daemon that
   * did not say — is drawn as "off" and advances to "all", so an unexpected value costs one tap and
   * not a dead button.
   */
  const REPEAT_NEXT = { off: "all", all: "single", single: "off" };
  const REPEAT_LABEL = { off: "Repeat off", all: "Repeat the queue", single: "Repeat one song" };

  function repeatMode(player) {
    const mode = player && player.repeat;
    return Object.prototype.hasOwnProperty.call(REPEAT_NEXT, mode) ? mode : "off";
  }

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

    const mode = repeatMode(player);
    const repeat = el("btn-repeat");
    repeat.textContent = REPEAT_LABEL[mode];
    // The stylesheet highlights `[data-mode]:not([data-mode="off"])`, so the border follows the text
    // rather than replacing it as the state signal.
    repeat.dataset.mode = mode;

    const shuffle = el("btn-shuffle");
    shuffle.textContent = player.shuffle ? "Shuffle on" : "Shuffle off";
    shuffle.setAttribute("aria-pressed", player.shuffle ? "true" : "false");

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

    // Playing with nothing attached. Everything above this line — the title, the moving progress
    // bar, the ⏸ glyph — is true of the daemon's queue and a lie about the room, so the fact that
    // no audio exists anywhere has to be said outright rather than left to be inferred from a
    // "— off" at the end of a muted line. Measured: OwnTone keeps `state: play` and keeps counting
    // after `All selected speakers failed to start`.
    const alert = el("speaker-alert");
    if (player.playing_without_output) {
      alert.textContent = "Playing, but no speaker is attached — nothing is coming out.";
      alert.hidden = false;
    } else {
      alert.textContent = "";
      alert.hidden = true;
    }

    renderQueue(player);
  }

  /**
   * Redraw the queue — but only when something in it changed.
   *
   * Every poll lands here, and most polls bring the same queue back. Rebuilding it anyway would be
   * harmless to look at and bad to use: even with `fill` putting focus back on the same row, moving
   * focus to a new node makes a screen reader announce it, so VoiceOver would re-read "Play …"
   * every two seconds. Skipping an unchanged queue keeps the very same nodes, so nothing is
   * announced and nothing moves.
   */
  function renderQueue(player) {
    const queue = player.queue || [];
    const key = JSON.stringify([
      player.item_id,
      queue.map((item) => [item.id, item.title, item.artist, item.album, item.path]),
    ]);
    if (key === state.queueKey) return;
    state.queueKey = key;

    el("queue-count").textContent = queue.length ? `(${queue.length})` : "";
    el("btn-clear").disabled = queue.length === 0;
    fill(
      el("queue"),
      queue.map((item) => queueRow(item, player.item_id)),
      "The queue is empty."
    );
  }

  /**
   * One queue row, where the row *is* the play button.
   *
   * Tapping a track in a queue is the gesture everybody arrives already knowing, and it used to do
   * nothing at all here: the rows were drawn with no actions, so reaching track seven meant ⏭ six
   * times. The title is the control rather than a "Play" button beside it, for two reasons — a queue
   * is a list of things to play, so a verb repeated on every row is noise, and the row is a
   * comfortable thumb target where a small button in a corner is not.
   *
   * What it sends is the item's **id**. Not its position: two phones and OwnTone's own web UI share
   * this queue (PLAN.md 2), so a position is only true of the poll it came from, while an id either
   * names the track that was tapped or names nothing and is answered `404`.
   *
   * The row already loaded is not exempt — a row that does nothing is the bug being fixed — but its
   * label says what the tap will actually do, because the daemon restarts that track rather than
   * resuming it.
   */
  function queueRow(item, currentId) {
    const title = item.title || item.path || "(untitled)";
    const current = Boolean(item.id) && item.id === currentId;
    const li = row(
      title,
      [item.artist, item.album].filter(Boolean).join(" · "),
      [],
      current ? "current" : "",
      // An item with no id is not playable and must not look it: nothing can be sent for it.
      item.id ? () => playQueueItem(item.id) : undefined
    );
    const main = li.querySelector(".row-main");
    if (item.id) {
      main.setAttribute("aria-label", current ? `Restart “${title}”` : `Play “${title}”`);
      // What `fill` follows focus by across a redraw.
      main.dataset.itemId = item.id;
    }
    if (current) main.setAttribute("aria-current", "true");
    return li;
  }

  /**
   * Jump to a queue item.
   *
   * No optimistic redraw, and no local guess at what is now playing — `command` redraws from the
   * response, which is rule 2 of this file. Worth knowing when reading the screen afterwards: for
   * about two seconds the daemon answers `state: "pause"` with no progress while the Cast output
   * starts (measured on the real speaker), so the transport button shows ▶ until the next poll. That
   * is the daemon being honest, not a command that failed.
   *
   * A `404` means the row went away between the poll and the tap — someone else's phone cleared the
   * queue. `attempt` puts that in the banner and the next poll removes the row.
   */
  function playQueueItem(itemId) {
    return command(`/player/queue/${encodeURIComponent(itemId)}/play`);
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

  /**
   * Turn the speaker on, and say so while waiting.
   *
   * This is the one control whose request is *slow on purpose*: a sleeping Cast speaker refuses the
   * first connection, so the server retries for up to about four seconds rather than handing back a
   * failure that has already stopped being true. Four seconds of a button that looks untouched reads
   * as a button that does nothing, which is the bug this whole change is about — so the label says
   * what is happening and the button refuses a second tap while it is happening.
   */
  async function waking() {
    const button = el("btn-select");
    const label = button.textContent;
    button.disabled = true;
    button.textContent = "Waking the speaker…";
    try {
      await command("/player/output/select");
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  }

  function wireTransport() {
    el("btn-toggle").addEventListener("click", () => command("/player/toggle"));
    el("btn-prev").addEventListener("click", () => command("/player/previous"));
    el("btn-next").addEventListener("click", () => command("/player/next"));
    el("btn-stop").addEventListener("click", () => command("/player/stop"));
    el("btn-select").addEventListener("click", waking);

    // The *absolute* next mode, not "advance": the session is shared, so a relative command from a
    // phone holding a two-second-old poll could land somewhere neither phone asked for. Sending the
    // mode this button's own label implied is idempotent, and the response redraws the label.
    el("btn-repeat").addEventListener("click", () =>
      command("/player/repeat", { mode: REPEAT_NEXT[repeatMode(state.player)] })
    );
    el("btn-shuffle").addEventListener("click", () =>
      command("/player/shuffle", { enabled: !(state.player && state.player.shuffle) })
    );

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
    const form = el("search-form");
    const go = form.querySelector('button[type="submit"]');
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const query = el("q").value.trim();
      if (!query) return;
      // Greyed out for the round trip, for the same reason as addButton below: when the field holds
      // a pasted link this button *is* the add button, and either branch is a yt-dlp subprocess on
      // the host. Re-enabled in `finally` rather than left disabled, because unlike a queued track
      // there is nothing here that stays done — the field is still there to search again with.
      go.disabled = true;
      try {
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
              [addButton(item.url)]
            )
          ),
          "No results."
        );
      } finally {
        go.disabled = false;
      }
    });
  }

  /**
   * The Add button on a search result: greyed out the moment it is tapped.
   *
   * Two reasons, and the second is the real one. A tap on a phone is easily a double-tap, and the
   * round trip includes a yt-dlp metadata request — long enough to look like nothing happened. And
   * once the job is queued a second tap cannot achieve anything: POST /ingest dedupes an identical
   * request back to the same job, so the button would be offering work that will not be done.
   *
   * It stays disabled on success and comes back on failure, because a 401 or a dead worker is
   * exactly when somebody wants to try again. The label carries the state as well as the colour —
   * a disabled button that still says "Add" reads as broken rather than done, and this stylesheet's
   * own rule is that nothing conveys state by colour alone.
   */
  function addButton(url) {
    const node = button("Add", async () => {
      node.disabled = true;
      node.textContent = "Adding…";
      const job = await ingest(url);
      if (job) {
        node.textContent = "Added";
        return;
      }
      node.disabled = false;
      node.textContent = "Add";
    }, "primary");
    return node;
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
    noticeFinishedIngests(jobs);
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

  /**
   * Reload the library when an ingest has just finished.
   *
   * Without this, the Library panel is only as fresh as the last time it was drawn: a track can say
   * "done" in the list above and be absent from the one below, which is the same library described
   * twice and disagreeing. It is a *transition* rather than a count, because the first poll after
   * unlocking sees every past ingest as done — news, then, would mean reloading the library on every
   * page load for no reason.
   */
  function noticeFinishedIngests(jobs) {
    const finished = new Set(
      jobs.filter((job) => job.state === "done" && job.path).map((job) => job.id)
    );
    const previous = state.finishedJobs;
    state.finishedJobs = finished;
    if (previous === null) return; // the first poll is history, not news
    for (const id of finished) {
      if (!previous.has(id)) {
        refreshLibrary();
        return;
      }
    }
  }

  /**
   * Redraw the library — unless a delete confirmation is open in it.
   *
   * Rebuilding the rows underneath an open confirmation would be worse than showing a stale list for
   * another moment: the listing is newest-first, so a freshly ingested track pushes every row down by
   * one, and the tap that was about to confirm a delete would land on the row that took its place.
   * A deferred refresh costs a tab switch; a mis-aimed delete costs a file.
   */
  function refreshLibrary() {
    if (el("library").querySelector(".row-confirm:not([hidden])")) return;
    loadLibrary();
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
      page.tracks.map(libraryRow),
      query ? "Nothing matches that." : "The library is empty — add something first."
    );
  }

  /** Unique ids for the disclosure wiring below; the rows themselves are rebuilt on every load. */
  let rowSeq = 0;

  /**
   * One library row: **Play**, **Queue**, and — two taps away — **Delete**.
   *
   * Delete is not a third button beside Play, and that is the whole design of this function. It is
   * the only irreversible thing the remote can do, what it destroys is the only irreplaceable state
   * the system has, and Play sits where a thumb lands on a phone. So it is behind the row's `⋯`,
   * and then behind a button that names the track it is about to remove.
   *
   * The confirmation is here rather than in the API on purpose: an endpoint that asked twice would
   * be a protocol every client eventually skips, while `window.confirm` is a modal a browser is
   * free to suppress — and in an installed web app it looks like the page has crashed.
   */
  function libraryRow(track) {
    const subtitle = [track.artist, track.album].filter(Boolean).join(" · ");
    const li = row(track.title, subtitle, [
      button("Play", () => enqueue(track.path, "replace", true), "primary"),
      button("Queue", () => enqueue(track.path, "append", false)),
    ]);

    const panel = make("div", "row-confirm");
    panel.id = `row-confirm-${++rowSeq}`;
    panel.hidden = true;

    const more = button("⋯", () => reveal(panel.hidden));
    more.setAttribute("aria-label", `More actions for ${track.title}`);
    more.setAttribute("aria-expanded", "false");
    more.setAttribute("aria-controls", panel.id);
    li.querySelector(".row-actions").appendChild(more);

    function reveal(show) {
      panel.hidden = !show;
      more.setAttribute("aria-expanded", String(show));
      // Rebuilt on every open rather than cached, which is not laziness about the DOM: the set of
      // playlists changes behind this row's back, so a strip first opened before any playlist existed
      // would go on saying "No playlists yet" for as long as the listing stood. One `<select>` per
      // open is cheap; a hundred rows each holding a stale one is what building it eagerly would cost.
      const stale = panel.querySelector(".row-add");
      if (stale) panel.removeChild(stale);
      if (show) panel.insertBefore(addToPlaylistControl(track), panel.firstChild);
    }

    panel.appendChild(
      make(
        "p",
        "row-warn",
        `Delete “${track.title}”? The file is removed from the disk, and the remote cannot put it back.`
      )
    );
    const actions = make("div", "row-actions");
    const remove = button("Delete", async () => {
      remove.disabled = true;
      remove.textContent = "Deleting…";
      const result = await deleteTrack(track);
      if (result) return; // the list is being rebuilt, so this row is about to disappear
      remove.disabled = false;
      remove.textContent = "Delete";
    }, "danger");
    actions.appendChild(remove);
    actions.appendChild(button("Cancel", () => reveal(false)));
    panel.appendChild(actions);
    li.appendChild(panel);
    return li;
  }

  /**
   * Delete one track, then redraw both halves of what it was in.
   *
   * The library listing *and* the player: the queue may have been holding this file, and a 409 is
   * the answer when it is the track currently loaded — which `attempt` puts in the banner verbatim,
   * because "stop playback or skip past it first" is the whole of what somebody needs to know.
   */
  async function deleteTrack(track) {
    const result = await attempt(
      () => api("/library", { method: "DELETE", body: { paths: [track.path] } }),
      "delete"
    );
    if (!result) return null;
    await loadLibrary();
    await refreshPlayer();
    const pruned = (result.removed_directories || []).length;
    const lists = (result.playlists_updated || []).length;
    el("lib-note").textContent =
      `Deleted ${track.title}.` +
      (pruned ? ` ${pruned} empty folder${pruned === 1 ? "" : "s"} removed.` : "") +
      // The part nobody asked a delete to do: entries naming this track were taken out of the
      // playlists that held them. Saying so is the difference between that and a list that silently
      // got shorter, which is what OwnTone on its own would have left behind.
      (lists ? ` Removed from ${lists} playlist${lists === 1 ? "" : "s"}.` : "");
    if (lists) await loadPlaylists();
    // A rescan the daemon refused is worth saying out loud: the file is gone, but OwnTone's own
    // browse view will keep offering it until it re-indexes.
    if (result.warnings && result.warnings.length) banner(result.warnings[0]);
    return result;
  }

  function wireLibrary() {
    el("lib-form").addEventListener("submit", (event) => {
      event.preventDefault();
      loadLibrary();
    });
  }

  // ------------------------------------------------------------------ playlists

  /**
   * The Playlists panel: see what is in one, change it, play it.
   *
   * A playlist here is an `.m3u` file in the music root that tunebox wrote, so everything on this
   * panel is one request and there is no local state to keep in sync — except which row is open,
   * because that is the one thing the server has no opinion about.
   *
   * The first version of this panel could only *create* (from the queue) and *play*, and that was the
   * wrong half of the feature to ship alone: a playlist was a name with a track count beside it, so
   * there was no way to see what was in one and no way to fix one song in forty without rebuilding
   * the whole list. Expanding a row and editing in place is what the rest of this section is.
   */
  async function loadPlaylists() {
    const note = el("pl-note");
    note.textContent = "Reading the playlists…";
    const found = await attempt(() => api("/playlists"), "playlists");
    if (!found) {
      note.textContent = "";
      return;
    }
    state.playlists = found.map((playlist) => playlist.name);
    note.textContent = found.length
      ? `${found.length} playlist${found.length === 1 ? "" : "s"}.`
      : "";
    fill(
      el("playlists"),
      found.map(playlistRow),
      "No playlists yet — save the queue above, or start an empty one and add tracks from the Library tab."
    );
  }

  /** "4 tracks · 1 missing" — said out loud because OwnTone would skip the missing one silently. */
  function playlistSubtitle(playlist) {
    const missing = playlist.missing_count || 0;
    return (
      `${playlist.count} track${playlist.count === 1 ? "" : "s"}` +
      (missing ? ` · ${missing} missing` : "")
    );
  }

  /**
   * One playlist row: tap the name to see inside, **Play**, **Queue**, and `⋯` for rename and delete.
   *
   * The name is the control, the same decision the queue's rows made: a row that only carried a count
   * was a row with nothing to tap, and "what is actually in this?" is the first question anybody has.
   * Play and Queue stay where a thumb lands; the two destructive-ish verbs stay behind `⋯`.
   */
  function playlistRow(playlist) {
    const li = row(
      playlist.name,
      playlistSubtitle(playlist),
      [
        button("Play", () => playPlaylist(playlist, "replace", true), "primary"),
        button("Queue", () => playPlaylist(playlist, "append", false)),
      ],
      undefined,
      () => toggle()
    );
    const main = li.querySelector(".row-main");
    // So a redraw can put keyboard focus back on the row it was on — see `fill`.
    main.dataset.itemId = playlist.name;
    main.setAttribute("aria-label", `${playlist.name}, ${playlistSubtitle(playlist)}. Show its tracks`);

    const tracks = make("div", "row-tracks");
    tracks.id = `pl-tracks-${++rowSeq}`;
    tracks.hidden = true;
    main.setAttribute("aria-expanded", "false");
    main.setAttribute("aria-controls", tracks.id);

    const panel = make("div", "row-confirm");
    panel.id = `row-confirm-${++rowSeq}`;
    panel.hidden = true;

    const more = button("⋯", () => reveal(panel.hidden));
    more.setAttribute("aria-label", `More actions for ${playlist.name}`);
    more.setAttribute("aria-expanded", "false");
    more.setAttribute("aria-controls", panel.id);
    li.querySelector(".row-actions").appendChild(more);

    function reveal(show) {
      panel.hidden = !show;
      more.setAttribute("aria-expanded", String(show));
    }

    async function toggle() {
      const showing = !tracks.hidden;
      state.openPlaylist = showing ? null : playlist.name;
      main.setAttribute("aria-expanded", String(!showing));
      tracks.hidden = showing;
      if (!showing) await drawEntries(playlist.name, li, tracks);
    }

    // Rename, which exists here because nothing else can do it: OwnTone's own web UI cannot edit
    // these files either (the same config flag that stops it writing one stops it renaming one).
    const rename = make("form", "row-rename");
    const field = document.createElement("input");
    field.type = "text";
    field.maxLength = 120;
    field.value = playlist.name;
    field.setAttribute("aria-label", `New name for ${playlist.name}`);
    rename.appendChild(field);
    const saveName = make("button", null, "Rename");
    saveName.type = "submit";
    rename.appendChild(saveName);
    rename.addEventListener("submit", async (event) => {
      event.preventDefault();
      const wanted = field.value.trim();
      if (!wanted || wanted === playlist.name) return;
      saveName.disabled = true;
      const renamed = await attempt(
        () =>
          api(`/playlists/${encodeURIComponent(playlist.name)}`, {
            method: "PATCH",
            body: { name: wanted },
          }),
        "rename"
      );
      saveName.disabled = false;
      if (!renamed) return;
      // The server sanitises the name, so it may not be the one that was typed — and whatever it is,
      // it is the one the next request has to use. Keep the row open under the new name.
      if (state.openPlaylist === playlist.name) state.openPlaylist = renamed.name;
      el("pl-note").textContent = `Renamed to ${renamed.name}.`;
      await loadPlaylists();
    });
    panel.appendChild(rename);

    panel.appendChild(
      make("p", "row-warn", `Delete the playlist “${playlist.name}”? No music is removed.`)
    );
    const actions = make("div", "row-actions");
    const remove = button("Delete", async () => {
      remove.disabled = true;
      remove.textContent = "Deleting…";
      const result = await attempt(
        () => api(`/playlists/${encodeURIComponent(playlist.name)}`, { method: "DELETE" }),
        "delete"
      );
      if (result) {
        if (state.openPlaylist === playlist.name) state.openPlaylist = null;
        el("pl-note").textContent = `Deleted ${playlist.name}.`;
        await loadPlaylists();
        return; // the list is being rebuilt, so this row is about to disappear
      }
      remove.disabled = false;
      remove.textContent = "Delete";
    }, "danger");
    actions.appendChild(remove);
    actions.appendChild(button("Cancel", () => reveal(false)));
    panel.appendChild(actions);

    li.appendChild(tracks);
    li.appendChild(panel);
    // Reopened after a redraw, so an edit does not collapse the thing being edited.
    if (state.openPlaylist === playlist.name) toggle();
    return li;
  }

  /** Fetch one playlist and draw its entries into an open row. */
  async function drawEntries(name, li, tracks) {
    clear(tracks);
    tracks.appendChild(make("p", "muted small", "Reading…"));
    const playlist = await attempt(
      () => api(`/playlists/${encodeURIComponent(name)}`),
      "playlist"
    );
    if (!playlist) {
      clear(tracks);
      return;
    }
    renderEntries(name, li, tracks, playlist);
  }

  /**
   * The entries of one playlist, numbered, each with ↑ ↓ and ✕.
   *
   * Numbered from one on screen and sent zero-based, which is the queue's convention and the API's.
   * The row shows the **entry** rather than only the track, so a line that resolves to nothing is
   * visible and removable instead of being a number in a subtitle: that is the state OwnTone will not
   * mention and the one somebody actually has to fix.
   */
  function renderEntries(name, li, tracks, playlist) {
    clear(tracks);
    const subtitle = li.querySelector(".row-sub");
    if (subtitle) subtitle.textContent = playlistSubtitle(playlist);
    if (!playlist.entries.length) {
      tracks.appendChild(
        make("p", "muted small", "Nothing in it yet — add tracks from the Library tab’s ⋯ menu.")
      );
      return;
    }
    const list = make("ol", "entries");
    playlist.entries.forEach((entry, index) => {
      list.appendChild(entryRow(name, li, tracks, playlist, entry, index));
    });
    tracks.appendChild(list);
  }

  function entryRow(name, li, tracks, playlist, entry, index) {
    const last = index === playlist.entries.length - 1;
    const title = entry.track ? entry.track.title : entry.entry;
    const subtitle = entry.track
      ? [entry.track.artist, entry.track.album].filter(Boolean).join(" · ")
      : "This file is not there — remove it, or put the track back.";

    /** Every edit answers with the whole playlist, so the list is redrawn from the response. */
    const after = async (work, label) => {
      const updated = await attempt(work, label);
      if (!updated) return;
      renderEntries(name, li, tracks, updated);
    };

    const up = button("↑", () =>
      after(
        () =>
          api(`/playlists/${encodeURIComponent(name)}/tracks/${entry.position}`, {
            method: "PUT",
            body: { to: entry.position - 1 },
          }),
        "move"
      )
    );
    up.disabled = index === 0;
    up.setAttribute("aria-label", `Move ${title} up`);

    const down = button("↓", () =>
      after(
        () =>
          api(`/playlists/${encodeURIComponent(name)}/tracks/${entry.position}`, {
            method: "PUT",
            body: { to: entry.position + 1 },
          }),
        "move"
      )
    );
    down.disabled = last;
    down.setAttribute("aria-label", `Move ${title} down`);

    // No confirmation strip for this one, unlike the Library panel's delete: it removes a line of
    // text and the track stays on disk, so asking twice would be ceremony for nothing. `positions`
    // and not the path, so that removing the second of two copies removes the second of two copies.
    const drop = button("✕", () =>
      after(
        () =>
          api(`/playlists/${encodeURIComponent(name)}/tracks`, {
            method: "DELETE",
            body: { positions: [entry.position] },
          }),
        "remove"
      ),
      "danger"
    );
    drop.setAttribute("aria-label", `Remove ${title} from ${name}`);

    const node = row(
      `${index + 1}. ${title}`,
      subtitle,
      [up, down, drop],
      entry.missing ? "entry missing" : "entry"
    );
    node.querySelector(".row-main").dataset.itemId = `${name}:${entry.position}`;
    return node;
  }

  /**
   * Play a playlist, or append it to the queue.
   *
   * One request: the server reads the file, resolves every entry under the music root and hands the
   * lot to the player at once — so a playlist cannot leave the queue half-replaced with playback
   * already running. The answer *is* the new player state, which is why this redraws from it rather
   * than polling for it.
   */
  function playPlaylist(playlist, mode, start) {
    return attempt(async () => {
      const player = await api(`/playlists/${encodeURIComponent(playlist.name)}/play`, {
        method: "PUT",
        body: { mode, start },
      });
      renderPlayer(player);
      selectTab("now");
    }, "playlist");
  }

  /**
   * The two ways a playlist starts life, both from the one name field.
   *
   * **Save the queue** is the quick one and was the only one at first, which turned out to be the
   * wrong place to stop: it means a playlist can only be built by first making it the thing that is
   * playing. **Start empty** is the other half — name it now, then file tracks into it from the
   * Library panel's `⋯` whenever you come across them.
   */
  function wirePlaylists() {
    const form = el("pl-form");
    const field = el("pl-name");
    const fromQueue = form.querySelector('button[type="submit"]');
    const empty = el("btn-pl-empty");

    async function create(body, describe) {
      const name = field.value.trim();
      if (!name) {
        field.focus();
        return;
      }
      fromQueue.disabled = empty.disabled = true;
      try {
        const playlist = await attempt(
          () => api("/playlists", { method: "POST", body: { name, ...body } }),
          "playlist"
        );
        if (!playlist) return;
        field.value = "";
        // Open the new one: with "start empty" especially, the next thing to happen is adding to it,
        // and a row that opened itself says where the tracks will appear.
        state.openPlaylist = playlist.name;
        await loadPlaylists();
        el("pl-note").textContent = describe(playlist);
      } finally {
        fromQueue.disabled = empty.disabled = false;
      }
    }

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      create({ from_queue: true }, (playlist) => {
        const skipped = (playlist.skipped || []).length;
        return (
          `Saved ${playlist.name} — ${playlist.count} track${playlist.count === 1 ? "" : "s"}.` +
          // Queue items that are not library tracks are left out rather than failing the save, so the
          // count can legitimately differ from the queue length. Saying which is cheap.
          (skipped ? ` ${skipped} queue item${skipped === 1 ? "" : "s"} skipped.` : "")
        );
      });
    });

    empty.addEventListener("click", () =>
      create({}, (playlist) => `Created ${playlist.name}. Add tracks from the Library tab.`)
    );
  }

  /**
   * "Add to playlist" for one library track, inside the row's `⋯` strip.
   *
   * A `<select>` of existing playlists rather than a free-text field, because creating one lives on
   * the Playlists panel and two ways to create would be two names for the same thing. Built when the
   * strip is opened rather than for every row of the listing: a hundred rows would otherwise be a
   * hundred dropdowns, each holding a copy of the same options that goes stale the moment somebody
   * saves a queue.
   */
  function addToPlaylistControl(track) {
    const box = make("div", "row-add");
    if (!state.playlists.length) {
      box.appendChild(
        make("p", "muted small", "No playlists yet — make one on the Playlists tab first.")
      );
      return box;
    }
    const picker = document.createElement("select");
    picker.setAttribute("aria-label", `Playlist to add ${track.title} to`);
    state.playlists.forEach((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      picker.appendChild(option);
    });
    box.appendChild(picker);
    const add = button("Add to playlist", async () => {
      add.disabled = true;
      add.textContent = "Adding…";
      const playlist = await attempt(
        () =>
          api(`/playlists/${encodeURIComponent(picker.value)}/tracks`, {
            method: "POST",
            body: { paths: [track.path] },
          }),
        "playlist"
      );
      if (playlist) {
        // Stays disabled, like the Add button on a search result: the track is in the playlist, and a
        // second tap would only put it there twice.
        add.textContent = "Added";
        el("lib-note").textContent = `Added ${track.title} to ${playlist.name}.`;
        return;
      }
      add.disabled = false;
      add.textContent = "Add to playlist";
    });
    box.appendChild(add);
    return box;
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
    wireTokenReveal();
  }

  /**
   * "Show token" — the one-time bridge to a home-screen icon.
   *
   * Adding this page to the home screen cannot bring the token with it: `tokenFromFragment` strips
   * it from the address bar the moment it reads it, so the captured URL has none, and iOS gives an
   * installed web app its own storage container, so `readStoredToken` finds nothing either. The
   * alternative to revealing it here is telling somebody to go and read tunebox.env on the machine
   * in the cupboard.
   *
   * Hidden by default and cleared out of the DOM again on hide, so it is on screen only while
   * somebody is deliberately looking at it.
   */
  function wireTokenReveal() {
    const button = el("btn-show-token");
    const panel = el("token-reveal");
    const text = el("token-text");
    button.addEventListener("click", () => {
      const showing = !panel.hidden;
      if (showing) {
        panel.hidden = true;
        // Emptied rather than merely hidden: a token sitting in the DOM is a token in the next
        // screenshot of an inspector, and nothing needs it there.
        text.textContent = "";
      } else {
        text.textContent = state.token || "";
        panel.hidden = false;
      }
      button.textContent = showing ? "Show token" : "Hide token";
      button.setAttribute("aria-expanded", String(!showing));
    });
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
    wirePlaylists();
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
