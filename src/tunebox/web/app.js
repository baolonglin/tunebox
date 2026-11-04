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
 *    **The one exception is the phone-as-output mode**, and it is an exception because the premise
 *    is reversed: when "This phone" is the output, this page *is* the player, so there is no server
 *    state to be the truth. The queue, the transport and the modes are held in `state.local` and
 *    the only thing asked of the server is a URL for one file. The two halves never share a queue —
 *    see the "the phone as an output" section for why mirroring them would be worse than not.
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

  /**
   * Whether this host offers the phone itself as an output (TUNEBOX_LOCAL_PLAYBACK).
   *
   * Read from the server-templated attribute rather than probed, so a host that has the feature off
   * never draws a picker. See the `data-local-playback` comment in index.html.
   */
  const LOCAL_PLAYBACK = CONST.localPlayback === "1";

  /** Which device this remote last chose. Per device, which is the only scope that makes sense. */
  const OUTPUT_KEY = "tunebox.output";
  /** This device's own queue, so closing the app and reopening it does not lose the list. */
  const LOCAL_QUEUE_KEY = "tunebox.local";
  /** The value of `state.device` that means "play here, in this browser". */
  const LOCAL_DEVICE = "local";
  /**
   * How many tracks ahead this device holds a streaming URL for. See `localPrefetchNext`: the
   * number is what decides how many track boundaries a phone in a pocket can cross without the
   * network, because a throttled page cannot complete a fetch to replace one it has spent.
   */
  const AHEAD_DEPTH = 3;

  /** Elements are looked up once: this page's DOM is static, only its contents change. */
  const el = (id) => document.getElementById(id);

  const state = {
    token: null,
    tab: "now",
    player: null,
    /**
     * Where this remote is sending audio: `"local"` (this browser is the audio path) or the id of
     * one of the daemon's outputs.
     *
     * Per device and never sent to the server. That asymmetry is the heart of the design: a speaker
     * is a shared thing, so its queue is the daemon's and anybody in the house may change it, while
     * "I am listening on headphones" is nobody else's business and must not reach into what another
     * phone is playing. One page, one selector, and a queue per device.
     */
    device: LOCAL_DEVICE,
    /**
     * The daemon's outputs, as last seen. Cached rather than read on demand because the selector
     * needs them in *both* modes, and in this device's own mode nothing else polls `GET /player` —
     * on a host with no OwnTone at all that poll would be a 503 in the banner every two seconds,
     * underneath a screen that is playing music perfectly well.
     */
    outputs: [],
    /**
     * This device's own playback. See the "this device as an output" section.
     *
     * `queue` is the list as drawn; `order` is the order it is *played* in (identity, or a shuffle)
     * and `cursor` indexes into `order`. Two arrays rather than shuffling `queue` itself, so turning
     * shuffle off restores the order somebody built rather than leaving it scrambled.
     */
    local: {
      queue: [],
      order: [],
      cursor: -1,
      repeat: "off",
      shuffle: false,
      /** Set when a track will not load — a deleted file, or an expired ticket. */
      error: null,
      /**
       * Streaming URLs already minted for the tracks coming next, as `[{ path, url }]`, up to
       * `AHEAD_DEPTH` of them. The whole point of it is that a track boundary contains no network
       * request: see `localPrefetchNext` and `localAdvanceOnEnded`.
       */
      ahead: [],
      /**
       * `{ path, ms }` read out of storage at startup — where this device was in the track when it
       * was last closed. Consumed by the first `localLoad`, and only if it is still the same track.
       */
      resume: null,
    },
    /** What the queue was last drawn from, so an unchanged poll leaves its nodes (and focus) alone. */
    queueKey: null,
    /**
     * What the device selector was last drawn from. Same reason as `queueKey` and one more: a poll
     * that rebuilt the `<option>` list would close the OS picker while somebody had it open.
     */
    deviceKey: null,
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
    // The selector's contents, and the one chance to notice that the device this page was last
    // pointed at is no longer there. Deliberately not awaited: a daemon that is down must not hold
    // up a screen that can play perfectly well without it.
    refreshDevices();
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
    // Volume is the one control that is not simply "the same thing, elsewhere": iOS refuses to let
    // a page set an audio element's volume, silently, so in phone mode on an iPhone the slider has
    // to be turned off and explained rather than left to move and do nothing. See `canSetVolume`.
    const volumeUsable = !player.local || canSetVolume();
    ["vol", "btn-volup", "btn-voldown"].forEach((id) => {
      el(id).disabled = !volumeUsable;
    });
    const volumeNote = el("vol-note");
    volumeNote.textContent = volumeUsable
      ? ""
      : "This browser will not let a page set the volume — use the phone's own volume buttons.";
    volumeNote.hidden = volumeUsable;

    const mode = repeatMode(player);
    const repeat = el("btn-repeat");
    repeat.textContent = REPEAT_LABEL[mode];
    // The stylesheet highlights `[data-mode]:not([data-mode="off"])`, so the border follows the text
    // rather than replacing it as the state signal.
    repeat.dataset.mode = mode;

    const shuffle = el("btn-shuffle");
    shuffle.textContent = player.shuffle ? "Shuffle on" : "Shuffle off";
    shuffle.setAttribute("aria-pressed", player.shuffle ? "true" : "false");

    // The daemon's outputs are the selector's contents, so a house-mode poll refreshes them for
    // free. In this device's own mode `localView` hands back the cached list instead.
    if (!player.local && player.outputs) state.outputs = player.outputs;
    renderDevices(player);
    renderDeviceNote(player);
    renderHandoff(player);

    // "Turn the speaker on" — the one case the output keeper deliberately will not handle (a
    // speaker somebody switched off on purpose). Only meaningful for the speaker this page is
    // pointed at, so it is hidden in this device's own mode and when the speaker is already on.
    const output = player.local ? null : currentOutput() || player.output;
    el("btn-select").hidden = !output || output.selected;

    // Playing with nothing attached. Everything above this line — the title, the moving progress
    // bar, the ⏸ glyph — is true of the daemon's queue and a lie about the room, so the fact that
    // no audio exists anywhere has to be said outright rather than left to be inferred from a
    // "— off" at the end of a muted line. Measured: OwnTone keeps `state: play` and keeps counting
    // after `All selected speakers failed to start`.
    //
    // In this device's own mode the equivalent failure is a track that will not load, which
    // `player.error` carries — the same place on screen, because it is the same question.
    const alert = el("speaker-alert");
    const problem = player.playing_without_output
      ? "Playing, but no speaker is attached — nothing is coming out."
      : player.error || "";
    alert.textContent = problem;
    alert.hidden = !problem;

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
    if (localMode()) return localPlayItem(itemId);
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
      // The *selected* device when that is a speaker, falling back to the configured one — which is
      // what this button meant before there was a selector, and still means on a fresh page whose
      // cached output list has not arrived yet.
      const output = currentOutput();
      if (output) await command("/player/output", { id: output.id });
      else await command("/player/output/select");
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  }

  function wireTransport() {
    // One handler per button, dispatched on the chosen output. The *button* is the same in both
    // modes and the thing it commands is not, so the branch belongs here rather than inside twelve
    // handlers — and a control that exists in only one mode (`btn-select`) is simply not wired to
    // the other, rather than being wired to something that quietly does nothing.
    el("btn-toggle").addEventListener("click", () =>
      localMode() ? localToggle() : command("/player/toggle")
    );
    el("btn-prev").addEventListener("click", () =>
      localMode() ? localPrev() : command("/player/previous")
    );
    el("btn-next").addEventListener("click", () =>
      localMode() ? localNext(true) : command("/player/next")
    );
    el("btn-stop").addEventListener("click", () =>
      localMode() ? localStop() : command("/player/stop")
    );
    el("btn-select").addEventListener("click", waking);

    // The *absolute* next mode, not "advance": the session is shared, so a relative command from a
    // phone holding a two-second-old poll could land somewhere neither phone asked for. Sending the
    // mode this button's own label implied is idempotent, and the response redraws the label.
    el("btn-repeat").addEventListener("click", () => {
      const mode = REPEAT_NEXT[repeatMode(state.player)];
      return localMode() ? localSetRepeat(mode) : command("/player/repeat", { mode });
    });
    el("btn-shuffle").addEventListener("click", () => {
      const enabled = !(state.player && state.player.shuffle);
      return localMode() ? localSetShuffle(enabled) : command("/player/shuffle", { enabled });
    });

    el("btn-volup").addEventListener("click", () =>
      localMode() ? localSetVolume({ step: 5 }) : command("/player/volume", { step: 5 })
    );
    el("btn-voldown").addEventListener("click", () =>
      localMode() ? localSetVolume({ step: -5 }) : command("/player/volume", { step: -5 })
    );

    const slider = el("vol");
    slider.addEventListener("input", () => {
      state.holdingVolume = true;
      el("vol-out").textContent = slider.value;
    });
    // On `change`, not `input`: a drag across the slider fires input for every pixel, and each one
    // would be a request to the daemon. The number beside it updates live regardless.
    slider.addEventListener("change", async () => {
      const volume = Number(slider.value);
      if (localMode()) await localSetVolume({ volume });
      else await command("/player/volume", { volume });
      state.holdingVolume = false;
    });

    el("btn-clear").addEventListener("click", () => {
      if (localMode()) return localClear();
      return attempt(async () => {
        await api("/player/queue", { method: "DELETE" });
        await refreshPlayer();
      }, "queue");
    });
  }

  /** Redraw the transport from whichever player owns it. */
  async function refreshTransport() {
    if (localMode()) {
      localRender();
      return;
    }
    await refreshPlayer();
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

  // ----------------------------------------------------- this device as an output

  /**
   * Playing on the device you are holding — a phone, a tablet, the laptop this page is open on.
   *
   * The rest of this project is built on the host owning the session so that music survives the
   * phone leaving (PLAN.md 2), and that is still the default. This mode is the deliberate opposite,
   * for the cases a shared speaker does not cover: headphones, a room with no speaker in it, somebody
   * else using the living room, or a household that never set OwnTone up at all.
   *
   * Three decisions worth knowing before reading the code.
   *
   * **There is no server-side session for it, and no sharing between devices.** A mirrored queue
   * would be state that outlived the
   * only thing able to play it — a device that walked out of the house would leave a queue behind
   * that nothing was the output for, and a second phone would then be able to "skip" a track it
   * could not hear. That last one is the decisive argument: two people on headphones are two
   * independent listeners, and one list behind both would let either of them clear the other's. So
   * the queue lives in `state.local`, persisted to this device's storage and nowhere else. The
   * honest consequence is on screen: in this mode the music stops when this device does.
   *
   * **The only thing the server provides is a URL.** `POST /library/tickets` signs a short-lived
   * capability URL per track, because `<audio src>` is a request the browser makes and a page
   * cannot put the shared token on it. See tunebox/core/tickets.py for why that is a capability URL
   * rather than a cookie or `?token=`.
   *
   * **A queue item's id is its index**, which would be wrong for a shared speaker's queue and is
   * right here: that queue has many writers, so a position is only true of the poll it came from
   * (see `queueRow`), while this one has exactly one writer — this page.
   */

  /** The element that actually makes the sound. */
  const audio = () => el("local-audio");

  /**
   * The looping silence that keeps iOS's audio session open, and the page scheduled. See the
   * `local-silence` comment in index.html for the measurements that put it there.
   */
  const silence = () => el("local-silence");

  /**
   * Hold or release iOS's audio session, in the three states this device can be in.
   *
   * `"playing"` and `"idle"` are obvious. `"paused"` is the one that took three measurements to get
   * right, and it is a genuine conflict rather than a missing trick:
   *
   * * **Let the channel go on pause** and the system state is honest — nothing shows as playing,
   *   nothing competes for the channel, no battery is spent. But a backgrounded page with no audio
   *   is a page iOS stops scheduling, so the `play` command from the lockscreen is not answered
   *   *at all*. Measured: pause from the pull-down while in another app, and playback cannot be
   *   resumed from there.
   * * **Keep it** and the lockscreen works both ways. But the system's Now Playing state follows
   *   whether audio is really running rather than `mediaSession.playbackState`, so it reports
   *   "playing" for a track nobody is listening to, within a second of the pause.
   *
   * Being unable to resume is worse than a button that looks wrong, so the channel is kept — with a
   * deadline. `SESSION_GRACE_MS` after a pause it is released, which covers the case a pause is
   * actually for (a word with somebody, then carry on) and caps what the wrong state costs. Come
   * back after that and the app is a tap away, which is where you already are.
   */
  const SESSION_GRACE_MS = 5 * 60 * 1000;
  let sessionRelease = null;
  function audioSession(state) {
    const keeper = silence();
    if (!keeper) return;
    if (sessionRelease !== null) {
      window.clearTimeout(sessionRelease);
      sessionRelease = null;
    }
    if (state !== "idle" && !localMode()) return;
    if (state === "idle") {
      if (!keeper.paused) keeper.pause();
      return;
    }
    if (keeper.paused) {
      const started = keeper.play();
      if (started && typeof started.catch === "function") started.catch(() => {});
    }
    if (state === "paused") {
      sessionRelease = window.setTimeout(() => {
        sessionRelease = null;
        // Only if it is still paused: a resume inside the window cancels this, but a second pause
        // re-arms it, and the two must not race into releasing the channel under live playback.
        if (audio().paused) audioSession("idle");
      }, SESSION_GRACE_MS);
    }
  }

  /**
   * Can this browser be told what volume to play at?
   *
   * iOS says no, and says it by *ignoring the assignment* rather than by throwing — so the only way
   * to find out is to set it and read it back. Without this check the slider would move under a
   * thumb and change nothing, which is the most expensive kind of broken control: it looks like it
   * worked. Memoised, because the answer cannot change while the page is open.
   */
  let volumeWritable = null;
  function canSetVolume() {
    if (volumeWritable === null) {
      const probe = document.createElement("audio");
      try {
        probe.volume = 0.5;
        volumeWritable = probe.volume === 0.5;
      } catch (error) {
        volumeWritable = false;
      }
    }
    return volumeWritable;
  }

  /** The track at the play cursor, or `null`. */
  function localTrack() {
    const { queue, order, cursor } = state.local;
    if (cursor < 0 || cursor >= order.length) return null;
    return queue[order[cursor]] || null;
  }

  /**
   * Rebuild the play order from the queue and the shuffle setting.
   *
   * `keep` is the queue index that must stay under the cursor, so that turning shuffle on does not
   * also skip the song that is playing. Fisher–Yates over the rest, which gives every remaining
   * track an equal chance of being next — "random next track" picked independently each time would
   * instead replay songs while others never came up at all.
   */
  function localReorder(keep) {
    const local = state.local;
    const indices = local.queue.map((_, index) => index);
    if (!local.shuffle) {
      local.order = indices;
      local.cursor = keep === null || keep === undefined ? local.cursor : keep;
      if (local.cursor >= local.order.length) local.cursor = local.order.length - 1;
      return;
    }
    const rest = indices.filter((index) => index !== keep);
    for (let i = rest.length - 1; i > 0; i -= 1) {
      const j = Math.floor(Math.random() * (i + 1));
      [rest[i], rest[j]] = [rest[j], rest[i]];
    }
    local.order = keep === null || keep === undefined ? rest : [keep, ...rest];
    local.cursor = local.order.length ? 0 : -1;
  }

  /**
   * The local state, shaped exactly like `GET /player`.
   *
   * One renderer for both modes is the point: `renderPlayer` draws the title, the progress bar, the
   * transport glyph, the modes and the queue, and a second copy of it for this mode would be a
   * second place for the screen to be subtly different. The one extra field is `local`, which is
   * how `renderPlayer` knows to describe the output as a phone rather than a speaker.
   */
  function localView() {
    const local = state.local;
    const element = audio();
    const track = localTrack();
    const loaded = Boolean(track);
    const length = Number.isFinite(element.duration) ? element.duration * 1000 : null;
    return {
      local: true,
      state: !loaded ? "stop" : element.paused ? "pause" : "play",
      volume: canSetVolume() ? Math.round(element.volume * 100) : null,
      repeat: local.repeat,
      shuffle: local.shuffle,
      consume: false,
      item_id: loaded ? `local-${local.order[local.cursor]}` : null,
      item_length_ms: length !== null ? length : track && track.length_ms ? track.length_ms : null,
      item_progress_ms: element.currentTime * 1000,
      now_playing: track
        ? { id: null, title: track.title, artist: track.artist, album: track.album, path: track.path }
        : null,
      outputs: state.outputs,
      queue: local.queue.map((item, index) => ({
        id: `local-${index}`,
        title: item.title,
        artist: item.artist,
        album: item.album,
        path: item.path,
      })),
      output: null,
      output_selected: false,
      // Impossible in this mode by construction: the output is the device running the page.
      playing_without_output: false,
      error: local.error,
    };
  }

  /** Draw the screen from `state.local`. The counterpart of `refreshPlayer` for this mode. */
  function localRender() {
    renderPlayer(localView());
    mediaSession();
  }

  /**
   * Mint a capability URL for one track.
   *
   * One request per track rather than one for the whole queue, deliberately: a 40-track queue would
   * be 40 signatures the phone would mostly never spend, and the URL has a lifetime, so minting at
   * the moment of use is also what keeps the ticket fresh for the ninth song of an evening.
   */
  async function localTicket(path) {
    const answer = await api("/library/tickets", { method: "POST", body: { paths: [path] } });
    const ticket = answer && answer.tickets && answer.tickets[0];
    if (!ticket) throw new ApiError(0, "no streaming URL came back");
    return ticket.url;
  }

  /**
   * Point the element at the track under the cursor, and optionally start it.
   *
   * `play()` is only called when asked, because a browser will reject it outside a user gesture —
   * so the gesture has to be the tap that got here. Once the element has played once the browser
   * stops asking, which is what lets `ended` advance to the next track on its own.
   */
  async function localLoad({ autoplay }) {
    const element = audio();
    const track = localTrack();
    // Before the await, deliberately: this is still inside the tap that got here, which is the only
    // kind of event that may start audio on iOS.
    audioSession(track && autoplay ? "playing" : "idle");
    if (!track) {
      element.removeAttribute("src");
      element.load();
      localRender();
      localPrefetchNext();
      return;
    }
    state.local.error = null;
    let url;
    try {
      url = await localTicket(track.path);
    } catch (error) {
      if (error.status === 401) return;
      state.local.error = `Cannot play ${track.title || track.path}: ${error.message}`;
      localRender();
      return;
    }
    element.src = url;
    localResumeInto(track);
    // **No `await` between assigning the `src` and the prefetch below.** Measured on this
    // household's phone: on a backgrounded page the promise `play()` returns does not settle, so an
    // `await` on it is the end of the function — and with the prefetch behind that await, a pocket
    // crossed exactly one track boundary and then stalled, because the thing that makes the *next*
    // boundary free never ran. The audio itself is unaffected either way: assigning `src` is what
    // starts the fetch and the decode.
    localPersist();
    localRender();
    if (autoplay) {
      let started;
      try {
        started = element.play();
      } catch (error) {
        started = null;
        state.local.error = `Could not start playback: ${error.message || error.name}`;
        localRender();
      }
      if (started && typeof started.catch === "function") {
        started.catch((error) => {
          // Autoplay refused, or the file will not decode. Either way the screen must not claim to
          // be playing — and the message differs, so it is the browser's own rather than ours.
          state.local.error = `Could not start playback: ${error.message || error.name}`;
          localRender();
        });
      }
    }
    // Last, and deliberately *after* playback has been asked for rather than before it: this issues
    // three requests, and nothing that merely makes the next track cheap may sit between a tap and
    // the sound. There is no `await` above, so a backgrounded page still reaches this line.
    localPrefetchNext();
  }

  /**
   * Put the element back where this device was when it was last closed.
   *
   * Only ever for the track that was playing then, and consumed on the first load either way: this
   * is "carry on where I was", not a bookmark per track. The seek waits for `loadedmetadata`
   * because `currentTime` on an element that has not read the container's header yet is ignored.
   */
  function localResumeInto(track) {
    const resume = state.local.resume;
    state.local.resume = null;
    if (!resume || resume.path !== track.path || resume.ms < 2000) return;
    const element = audio();
    const seconds = resume.ms / 1000;
    element.addEventListener(
      "loadedmetadata",
      () => {
        // Never to the very end: a track saved two seconds before it finished would otherwise
        // resume into its own `ended`.
        if (Number.isFinite(element.duration) && seconds > element.duration - 2) return;
        try {
          element.currentTime = seconds;
        } catch (error) {
          /* the browser would not seek; the track plays from the beginning, which is not a failure */
        }
      },
      { once: true }
    );
  }

  /**
   * The track `steps` boundaries ahead, or `null` when that is not knowable yet.
   *
   * Pure, and separate from `localNext` for one reason: it is what can be prefetched. Three of the
   * cases are deliberately `null` rather than a guess — repeat-one never fires `ended` at all
   * (`element.loop` does the repeating), the end of the queue with repeat off is a stop, and a
   * shuffled queue that is about to wrap gets re-shuffled *by* the wrap, so which track comes next
   * genuinely does not exist yet. The last of those is the one boundary in an evening that still
   * takes the slow path.
   */
  function localAheadTrack(steps) {
    const local = state.local;
    const wanted = steps === undefined ? 1 : steps;
    if (local.cursor < 0 || !local.order.length) return null;
    if (local.repeat === "single") return null;
    let at = local.cursor;
    for (let i = 0; i < wanted; i += 1) {
      if (at < local.order.length - 1) {
        at += 1;
      } else if (local.repeat === "all" && !local.shuffle) {
        at = 0;
      } else {
        return null;
      }
    }
    return local.queue[local.order[at]] || null;
  }

  /**
   * Mint the next few tracks' capability URLs *now*, while this one is still playing.
   *
   * This exists because of one measured iOS behaviour: a backgrounded page is kept alive while its
   * audio element is playing and throttled the moment it is not — and `ended` is exactly that
   * moment. The original code had to `await localTicket()` before it could assign a `src`, so on a
   * phone in a pocket the fetch was suspended mid-flight and the music simply stopped at the end of
   * the track.
   *
   * **`AHEAD_DEPTH` is 3 rather than 1, and that is the second half of the same lesson.** Holding
   * exactly one URL buys exactly one boundary: crossing it empties the store, and refilling it is a
   * fetch — which is the thing that does not come back while the page is throttled. So a pocket got
   * one good track change and then stalled on the next. Three means three boundaries with no
   * network at all, by which time the phone has usually been looked at. It is three signatures, not
   * forty: minting the whole queue up front was rejected (README) because most of them are never
   * spent, and that argument is about a queue, not about the next few minutes.
   *
   * Idempotent, and keyed by **path** rather than by position: a held URL names a file, so it stays
   * good when the queue is reordered around it and is simply dropped when the track is no longer
   * coming. Failure is silent on purpose — `localAdvanceOnEnded` falls back to the slow path that
   * was always there, and a toast about a track that has not started yet would be noise.
   */
  function localPrefetchNext() {
    const local = state.local;
    const wanted = [];
    for (let step = 1; step <= AHEAD_DEPTH; step += 1) {
      const track = localAheadTrack(step);
      if (!track) break;
      // A queue shorter than the depth, on repeat: one URL per file is all there is to hold.
      if (wanted.indexOf(track.path) >= 0) break;
      wanted.push(track.path);
    }
    const held = (path) => local.ahead.some((ticket) => ticket.path === path);
    // Synchronous, so it is still correct on a page that cannot complete a fetch: a track that is no
    // longer coming loses its URL whether or not the replacements arrive.
    local.ahead = local.ahead.filter((ticket) => wanted.indexOf(ticket.path) >= 0);
    const missing = wanted.filter((path) => !held(path));
    if (!missing.length) return Promise.resolve();
    return Promise.all(
      missing.map((path) =>
        localTicket(path).then(
          (url) => {
            // The queue may have moved on while the request was in flight. Checked against what is
            // wanted *now*, not against what was wanted when it was sent.
            for (let step = 1; step <= AHEAD_DEPTH; step += 1) {
              const track = localAheadTrack(step);
              if (track && track.path === path && !held(path)) {
                local.ahead.push({ path, url });
                return;
              }
            }
          },
          () => {
            /* no prefetch for that one; the end of the track will mint it the slow way */
          }
        )
      )
    );
  }

  /**
   * A track finished on its own.
   *
   * **Synchronous from `ended` to `play()`**, and that is the whole value of this function: no
   * `await`, no fetch, no promise between the event and the assignment, because on a backgrounded
   * iOS page there may be no later turn of the event loop to come back to. The cursor arithmetic is
   * the same as `localNext(false)`'s, over the one case `localAheadTrack` already proved is a plain
   * step: either one forward, or round to the top of a queue on repeat.
   *
   * Anything else — no held URL for the track that is next, the end of the queue, repeat-one — is
   * handed to `localNext`, which is still correct and merely slower.
   */
  function localAdvanceOnEnded() {
    const local = state.local;
    if (!localMode()) return Promise.resolve();
    const next = localAheadTrack(1);
    const ticket = next && local.ahead.filter((candidate) => candidate.path === next.path)[0];
    if (!ticket) return localNext(false);
    const element = audio();
    local.cursor = local.cursor < local.order.length - 1 ? local.cursor + 1 : 0;
    local.error = null;
    element.src = ticket.url;
    let started;
    try {
      started = element.play();
    } catch (error) {
      started = null;
    }
    if (started && typeof started.catch === "function") {
      // A rejected `play()` here means the ticket expired or the file has gone, so the slow path is
      // worth taking: it mints a fresh URL and reports a real error if that fails too.
      started.catch(() => localLoad({ autoplay: true }));
    }
    localPersist();
    localRender();
    // Tops the store back up to the depth, and prunes the track that has just started. Its fetch may
    // never come back on a page in a pocket, which is exactly why the depth is more than one.
    localPrefetchNext();
    return Promise.resolve();
  }

  /**
   * Add tracks to the phone's queue. The local half of `enqueue`.
   *
   * `tracks` carry their own title and artist because nothing on this side can read a file's tags:
   * the Library panel, the Ingests list and a playlist all already have them, so they are passed
   * along rather than looked up again.
   */
  async function localEnqueue(tracks, mode, start) {
    const local = state.local;
    const usable = tracks.filter((track) => track && track.path);
    if (!usable.length) return;
    const first = mode === "replace" ? 0 : local.queue.length;
    if (mode === "replace") {
      local.queue = usable.slice();
      local.cursor = -1;
    } else {
      local.queue = local.queue.concat(usable);
    }
    const playing = localTrack();
    localReorder(playing ? local.queue.indexOf(playing) : mode === "replace" ? first : undefined);
    if (mode === "replace" || start) {
      local.cursor = local.order.indexOf(first);
      if (local.cursor < 0) local.cursor = 0;
    }
    // Appending to a queue that had nothing loaded still has to leave something *selected*, or the
    // play button is drawn next to a list of songs and does nothing when pressed — "nothing loaded"
    // with a full queue is a state the house speaker can be in and this one must not.
    if (local.cursor < 0 && local.order.length) local.cursor = 0;
    localPersist();
    if (start) {
      await localLoad({ autoplay: true });
    } else {
      localRender();
      localPrefetchNext();
    }
  }

  /** Jump to a row in the phone's queue. `id` is `local-<index>` — see the section comment. */
  function localPlayItem(id) {
    const index = Number(String(id).replace("local-", ""));
    if (!Number.isInteger(index)) return Promise.resolve();
    const at = state.local.order.indexOf(index);
    if (at < 0) return Promise.resolve();
    state.local.cursor = at;
    return localLoad({ autoplay: true });
  }

  function localToggle() {
    const element = audio();
    if (!localTrack()) return Promise.resolve();
    if (!element.getAttribute("src")) {
      audioSession("playing");
      return localLoad({ autoplay: true });
    }
    if (element.paused) {
      audioSession("playing");
      return element.play().then(localRender, localRender);
    }
    element.pause();
    // Kept, with a deadline — see audioSession. A lockscreen pause has to be undoable there.
    audioSession("paused");
    localRender();
    return Promise.resolve();
  }

  /**
   * Next track. `manual` separates a tap on ⏭ from a track ending, and the difference is repeat:
   * "repeat one song" must replay on its own and must *not* trap the skip button on one track.
   */
  function localNext(manual) {
    const local = state.local;
    if (!local.order.length) return Promise.resolve();
    if (!manual && local.repeat === "single") return localLoad({ autoplay: true });
    const last = local.cursor >= local.order.length - 1;
    if (last) {
      if (local.repeat === "off" && !manual) return localStop();
      // A shuffled queue is re-shuffled when it wraps, so "repeat the queue" is not the same twenty
      // songs in the same order forever.
      if (local.shuffle) localReorder(undefined);
      local.cursor = 0;
    } else {
      local.cursor += 1;
    }
    localPersist();
    return localLoad({ autoplay: true });
  }

  /**
   * Previous track — or the start of this one, which is what every music player does and what makes
   * the button usable: three seconds into a song, ⏮ means "I missed the beginning".
   */
  function localPrev() {
    const local = state.local;
    const element = audio();
    if (!local.order.length) return Promise.resolve();
    if (element.currentTime > 3 || local.cursor <= 0) {
      element.currentTime = 0;
      if (element.paused) return localToggle();
      localRender();
      return Promise.resolve();
    }
    local.cursor -= 1;
    localPersist();
    return localLoad({ autoplay: true });
  }

  /** Stop: unload the track, keep the queue. The same meaning as `PUT /player/stop`. */
  function localStop() {
    const element = audio();
    element.pause();
    element.removeAttribute("src");
    element.load();
    // The cursor stays where it was, so ⏯ plays that track again from the beginning rather than
    // jumping back to the top of the list — a stop is "that is enough", not "start over".
    if (!state.local.order.length) state.local.cursor = -1;
    state.local.ahead = [];
    audioSession("idle");
    localPersist();
    localRender();
    return Promise.resolve();
  }

  function localClear() {
    state.local.queue = [];
    state.local.order = [];
    state.local.cursor = -1;
    return localStop();
  }

  function localSetRepeat(mode) {
    state.local.repeat = mode;
    // `loop` as well as the handler: the element looping itself is gapless, where waiting for
    // `ended` and re-assigning `src` is a re-fetch and an audible seam.
    audio().loop = mode === "single";
    localPersist();
    localRender();
    localPrefetchNext();
    return Promise.resolve();
  }

  function localSetShuffle(enabled) {
    state.local.shuffle = enabled;
    const playing = localTrack();
    localReorder(playing ? state.local.queue.indexOf(playing) : undefined);
    localPersist();
    localRender();
    localPrefetchNext();
    return Promise.resolve();
  }

  function localSetVolume({ volume, step }) {
    if (!canSetVolume()) return Promise.resolve();
    const element = audio();
    const current = Math.round(element.volume * 100);
    const wanted = volume !== undefined ? volume : current + step;
    element.volume = Math.max(0, Math.min(100, wanted)) / 100;
    localPersist();
    localRender();
    return Promise.resolve();
  }

  /** Drop every copy of a path from the phone's queue — what a library delete has to do here. */
  function localForget(path) {
    const local = state.local;
    if (!local.queue.some((track) => track.path === path)) return;
    const playing = localTrack();
    local.queue = local.queue.filter((track) => track.path !== path);
    const keep = playing && playing.path !== path ? local.queue.indexOf(playing) : undefined;
    localReorder(keep);
    if (playing && playing.path === path) localStop();
    else {
      localPersist();
      localRender();
      localPrefetchNext();
    }
  }

  // ---------------------------------------------- the phone's queue, across reloads

  /**
   * Keep the phone's queue on the phone.
   *
   * The house queue survives a restart because OwnTone holds it; this one would not survive a tab
   * being reopened, and a list somebody built disappearing because they switched apps is the sort of
   * loss a remote should not inflict.
   *
   * The playback **position** is stored too, which an earlier version of this called a promise it
   * could not keep. It can: the element is gone when the app is reopened and so is the ticket, but
   * `localLoad` mints a fresh one anyway, so the only thing that was ever missing was the number of
   * seconds. It matters most in the case this mode is worst at — the phone locking, or walking out
   * of Wi-Fi range, halfway through a long track — where without it the one tap that gets the music
   * back also starts the song again.
   */
  function localPersist() {
    try {
      const local = state.local;
      const element = audio();
      const track = localTrack();
      window.localStorage.setItem(
        LOCAL_QUEUE_KEY,
        JSON.stringify({
          queue: local.queue,
          cursor: local.cursor,
          order: local.order,
          repeat: local.repeat,
          shuffle: local.shuffle,
          volume: canSetVolume() ? element.volume : undefined,
          // `readyState` is the guard that makes this safe to call from anywhere, and it is not
          // defensive padding: assigning a `src` resets the element to HAVE_NOTHING, so between the
          // boundary swap and the new track's first frame `currentTime` is the *old* track's
          // position. Written without this, the next reopen would resume a fresh song three minutes
          // in. Zero means "nothing is loaded", so there is no position to record.
          position:
            track && element.readyState > 0 && element.currentTime > 0
              ? { path: track.path, ms: Math.round(element.currentTime * 1000) }
              : undefined,
        })
      );
    } catch (error) {
      /* storage disabled; the queue is then good for this session only */
    }
  }

  /**
   * Write the position down while a track plays, not only when something changes.
   *
   * Every five seconds rather than on every `timeupdate`, which fires four times a second: this is
   * `localStorage` on a phone, and the worst case the throttle costs is resuming five seconds early.
   * It has to be a timer of this shape because the event that would be the obvious hook does not
   * exist — a tab iOS discards, or a phone whose battery dies, fires no `pagehide` and no
   * `beforeunload`. The last write before the lights go out is simply the last one on the clock.
   */
  let positionWrittenAt = 0;
  function localRecordPosition() {
    const now = Date.now();
    if (now - positionWrittenAt < 5000) return;
    positionWrittenAt = now;
    localPersist();
  }

  function localRestore() {
    let saved;
    try {
      saved = JSON.parse(window.localStorage.getItem(LOCAL_QUEUE_KEY) || "null");
    } catch (error) {
      saved = null;
    }
    if (!saved || !Array.isArray(saved.queue)) return;
    const local = state.local;
    // Validated rather than trusted: this came out of storage, which a previous version of this
    // script wrote and a future one has to survive reading.
    local.queue = saved.queue
      .filter((track) => track && typeof track.path === "string")
      .map((track) => ({
        path: track.path,
        title: track.title || track.path,
        artist: track.artist || null,
        album: track.album || null,
      }));
    local.repeat = Object.prototype.hasOwnProperty.call(REPEAT_NEXT, saved.repeat)
      ? saved.repeat
      : "off";
    local.shuffle = saved.shuffle === true;
    const order = Array.isArray(saved.order)
      ? saved.order.filter((index) => Number.isInteger(index) && index < local.queue.length)
      : [];
    local.order = order.length === local.queue.length ? order : local.queue.map((_, i) => i);
    local.cursor = Number.isInteger(saved.cursor) && saved.cursor < local.order.length ? saved.cursor : -1;
    const position = saved.position;
    local.resume =
      position && typeof position.path === "string" && Number.isFinite(position.ms) && position.ms > 0
        ? { path: position.path, ms: position.ms }
        : null;
    if (canSetVolume() && typeof saved.volume === "number") {
      audio().volume = Math.max(0, Math.min(1, saved.volume));
    }
    audio().loop = local.repeat === "single";
  }

  // ------------------------------------------------------- lockscreen and events

  /**
   * Hand the track to the OS, so the lockscreen and the headphone buttons work.
   *
   * This is the one thing local playback can do that a shared speaker cannot: the Media
   * Session API needs the audio to be coming out of *this* browser, so on the OwnTone path there is
   * nothing to give it (README: "what the remote does not give you is lockscreen control — the
   * accepted cost of the phone not being the audio path"). Here it costs twenty lines.
   *
   * No artwork, for the same reason nothing else on this page loads an image: tunebox does not
   * extract embedded cover art, and the alternative is fetching one from the internet.
   */
  function mediaSession() {
    const session = navigator.mediaSession;
    if (!session || !window.MediaMetadata) return;
    const track = localMode() ? localTrack() : null;
    if (!track) {
      session.metadata = null;
      session.playbackState = "none";
      return;
    }
    session.metadata = new window.MediaMetadata({
      title: track.title || track.path,
      artist: track.artist || "",
      album: track.album || "",
    });
    session.playbackState = audio().paused ? "paused" : "playing";
  }

  function wireMediaSession() {
    const session = navigator.mediaSession;
    if (!session || typeof session.setActionHandler !== "function") return;
    const handlers = {
      // `play` and `pause` both go through `localToggle`, which is also what the page's own button
      // calls — so the lockscreen and the page cannot drift apart, and the audio session is picked
      // up and let go in exactly the same places. Measured: the system's own Now Playing state
      // follows whether audio is *actually* running, not `session.playbackState`, so a pause that
      // left a silent stream playing was reported back as "playing" within a second.
      play: () => localToggle(),
      pause: () => localToggle(),
      stop: () => localStop(),
      previoustrack: () => localPrev(),
      nexttrack: () => localNext(true),
      seekto: (details) => {
        if (details && typeof details.seekTime === "number") audio().currentTime = details.seekTime;
      },
    };
    Object.entries(handlers).forEach(([action, handler]) => {
      try {
        session.setActionHandler(action, handler);
      } catch (error) {
        /* an action this browser does not know about; the rest still work */
      }
    });
  }

  /**
   * Let the silence be startable later, from the first touch anywhere on the page.
   *
   * An audio element on iOS may not be started by script until it has played once inside a user
   * gesture. The taps that start music are several frames and sometimes a fetch away from
   * `audioSession`, so rather than thread the gesture through every one of them, the first
   * touch on the page unlocks the element with a play/pause pair it will never hear. After that a
   * programmatic `play()` is permitted, which is all this needs — the session itself is live at
   * that point anyway, because the track the tap started is playing.
   */
  function unlockSilenceOnFirstTouch() {
    const keeper = silence();
    if (!keeper) return;
    const unlock = () => {
      const started = keeper.play();
      const settle = () => {
        // Unlocking is not playing: unless music is already going, it goes straight back to paused.
        if (!localMode() || audio().paused) keeper.pause();
      };
      if (started && typeof started.then === "function") started.then(settle, () => {});
      else settle();
    };
    // `once`, and on both events, because a desktop browser with no touch still has to get here for
    // the test harness and for anybody debugging this on a laptop.
    ["pointerdown", "keydown"].forEach((name) =>
      document.addEventListener(name, unlock, { once: true, passive: true })
    );
  }

  function wireAudio() {
    const element = audio();
    // Four frames a second from the element rather than a one-second interval of our own: this is
    // the only mode where the true position is known locally, so there is no reason to guess at it.
    element.addEventListener("timeupdate", () => {
      if (!localMode()) return;
      state.progressMs = element.currentTime * 1000;
      drawProgress();
      localRecordPosition();
    });
    // The length is not known until the browser has read the container's header, and the progress
    // bar has no scale until it is.
    element.addEventListener("loadedmetadata", localRender);
    element.addEventListener("play", localRender);
    element.addEventListener("pause", localRender);
    // Not `localNext(false)`: see `localAdvanceOnEnded`, which is the same decision reached without
    // a network request in the way, because on a backgrounded phone there may be no second chance.
    element.addEventListener("ended", () => localAdvanceOnEnded());
    element.addEventListener("error", () => {
      if (!localMode()) return;
      // `load()` on an element whose src has just been removed fires this too, so an unloaded
      // element is not an error — `localStop` is a thing somebody asked for.
      if (!element.getAttribute("src")) return;
      const track = localTrack();
      // The element's own error says almost nothing useful ("MEDIA_ELEMENT_ERROR"), so this names
      // the track instead — which is the half somebody can act on, since the usual causes are a
      // file deleted since the queue was built and a format this browser cannot decode.
      state.local.error = track
        ? `Could not play ${track.title || track.path} — the file may have gone, or this browser cannot decode it.`
        : "Could not play that track.";
      localRender();
    });
  }

  // ----------------------------------------------------------- the device selector

  /**
   * One selector, every device this remote can drive.
   *
   * The thing it encodes, which took two attempts to get right: **a queue belongs to an output, not
   * to a page.** A shared speaker is one speaker in one room, so its queue is the daemon's and
   * anybody holding the token may skip a track — that is the feature. This device is one pair of
   * headphones, so its queue is its own and nobody else can reach it. An earlier draft had the two
   * sharing one list, which would have let somebody pressing Clear in the kitchen empty what another
   * person was listening to on headphones. There is no sense in which that is "changing the music in
   * the room".
   *
   * So switching the selector changes *which session this page is showing and controlling*, and
   * nothing else. Moving the music is a separate, deliberate button — see `handoff`.
   */

  function localMode() {
    return LOCAL_PLAYBACK && state.device === LOCAL_DEVICE;
  }

  /** The output `state.device` names, from the cached list, or `null` in this device's own mode. */
  function currentOutput() {
    if (localMode()) return null;
    return state.outputs.find((output) => output.id === state.device) || null;
  }

  /** The speaker a handoff would go to: the one selected, else the first one we may use. */
  function handoffTarget() {
    const usable = (output) => output.selectable && !output.needs_pairing;
    return (
      state.outputs.find((output) => output.id === state.device && usable(output)) ||
      state.outputs.find((output) => output.selected && usable(output)) ||
      state.outputs.find(usable) ||
      null
    );
  }

  function readStoredDevice() {
    try {
      return window.localStorage.getItem(OUTPUT_KEY) || LOCAL_DEVICE;
    } catch (error) {
      return LOCAL_DEVICE;
    }
  }

  function storeDevice(device) {
    try {
      window.localStorage.setItem(OUTPUT_KEY, device);
    } catch (error) {
      /* see readStoredToken */
    }
  }

  /**
   * Point this page at a device.
   *
   * Switching *to* a speaker enables it, exclusively — `PUT /player/output` turns the others off,
   * because that is what a single-choice selector means. Switching away from this device pauses it
   * for the one reason worth coding for: two things playing at once is the only outcome nobody
   * wants, and a phone singing in a pocket while its owner wonders why the room sounds doubled is
   * how that presents.
   */
  async function selectDevice(device) {
    const previous = state.device;
    if (device === previous) return;

    if (device !== LOCAL_DEVICE) {
      const node = el("device");
      const label = node.disabled;
      node.disabled = true;
      // Slow on purpose: a Cast speaker in standby refuses its first connection, so the server
      // retries for a few seconds. The selector saying nothing for that long reads as a selector
      // that did nothing.
      el("device-note").textContent = "Waking the speaker…";
      const player = await attempt(
        () => api("/player/output", { method: "PUT", body: { id: device } }),
        "speaker"
      );
      node.disabled = label;
      if (!player) {
        // 403 (not ours to use), 404 (it left the network) or 503 (it will not wake). The banner
        // has the sentence; the selector goes back to where it was rather than lying about what is
        // playing where.
        renderDevices(state.player);
        return;
      }
      if (previous === LOCAL_DEVICE) {
        audio().pause();
        // The session is held for *this* device's playback. Pointing the page at a speaker means
        // the room is the audio path, so there is nothing here left to keep alive.
        audioSession("idle");
      }
      state.device = device;
      storeDevice(device);
      renderPlayer(player);
      return;
    }

    state.device = LOCAL_DEVICE;
    storeDevice(LOCAL_DEVICE);
    // The speaker is left exactly as it is — still selected, still holding its queue. Taking this
    // page's attention away from a shared speaker must not stop a room somebody else is sitting in.
    localRender();
  }

  /**
   * Draw the selector from the cached output list. Rebuilt only when that list changes.
   *
   * `player` is whichever document was just rendered, so this works for both modes: in this
   * device's own mode `localView` carries the same cached outputs, which is what lets one renderer
   * draw one selector.
   */
  function renderDevices(player) {
    const outputs = (player && player.outputs) || state.outputs;
    const key = JSON.stringify([
      state.device,
      LOCAL_PLAYBACK,
      outputs.map((o) => [o.id, o.name, o.type, o.selected, o.selectable, o.needs_pairing]),
    ]);
    if (key === state.deviceKey) return;
    state.deviceKey = key;

    const node = el("device");
    const speakers = el("device-speakers");
    clear(speakers);
    outputs.forEach((output) => {
      const option = make("option", null, `${output.name}${output.type ? ` · ${output.type}` : ""}`);
      option.value = output.id;
      // Two different reasons a speaker cannot be chosen, and they are shown rather than hidden
      // because they are different statements with different fixes. Disabled rather than omitted:
      // "that speaker exists and you cannot use it" is not "there is no such speaker".
      if (!output.selectable) {
        option.disabled = true;
        option.textContent += " (not available to this remote)";
      } else if (output.needs_pairing) {
        // The daemon has already established this one wants a PIN, so a tap could only ever be a
        // 409. Nothing tunebox can do: the pairing is OwnTone's own, and the footer links to it.
        option.disabled = true;
        option.textContent += " (needs pairing in OwnTone)";
      }
      speakers.appendChild(option);
    });
    speakers.hidden = outputs.length === 0;
    // Hidden when the host does not offer it at all (TUNEBOX_LOCAL_PLAYBACK=0), because an option
    // that cannot be chosen is worse than an option that is not there.
    el("device-self").hidden = !LOCAL_PLAYBACK;
    node.value = state.device;
    // A `state.device` naming a speaker that has since left the network leaves the select with no
    // matching option, and a <select> with no match shows its first entry — which would quietly
    // claim the music is playing somewhere it is not.
    if (node.selectedIndex < 0) node.value = LOCAL_PLAYBACK ? LOCAL_DEVICE : "";
  }

  /** The line under the selector: where the sound is, and what that costs. */
  function renderDeviceNote(player) {
    const note = el("device-note");
    if (player && player.local) {
      note.textContent =
        "Playing here, on this device only. Its queue is private, and the music stops if this " +
        "device sleeps, locks for long enough, or leaves.";
      return;
    }
    const output = currentOutput();
    if (!output) {
      note.textContent = state.outputs.length
        ? "That speaker is not visible on the network."
        : "No speakers visible — is OwnTone running?";
      return;
    }
    note.textContent =
      `${output.name} — ${output.selected ? "on" : "off"}. ` +
      "This queue is shared, so anyone in the house can change it.";
  }

  /**
   * Move the music, rather than just the page's attention.
   *
   * Copies the queue *and the position* to the other side, starts it there, and pauses the side you
   * left. Symmetric, which is why it is one button with two labels: "continue on the speaker" and
   * "continue here" are the same operation in opposite directions.
   *
   * A copy, never a shared list. That is the whole reason this is a button somebody presses rather
   * than one queue behind both devices: a copy cannot reach into what another person is listening
   * to, and the implicit version could.
   */
  function renderHandoff(player) {
    const button = el("btn-handoff");
    const local = Boolean(player && player.local);
    const target = handoffTarget();
    const houseQueue = !local && player ? (player.queue || []).length : null;
    if (local) {
      const have = state.local.queue.length;
      button.hidden = !have || !target;
      if (!button.hidden) button.textContent = `Continue on ${target.name}`;
    } else {
      button.hidden = !LOCAL_PLAYBACK || !houseQueue;
      if (!button.hidden) button.textContent = "Continue on this device";
    }
  }

  async function handoff() {
    const button = el("btn-handoff");
    const label = button.textContent;
    button.disabled = true;
    button.textContent = "Moving the music…";
    try {
      if (localMode()) await handoffToSpeaker();
      else await handoffToHere();
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  }

  /** This device → a speaker: copy the list, select it, start at the same track and second. */
  async function handoffToSpeaker() {
    const target = handoffTarget();
    const track = localTrack();
    if (!target || !state.local.queue.length) return;
    const paths = state.local.queue.map((item) => item.path);
    const index = track ? state.local.queue.indexOf(track) : 0;
    const positionMs = Math.floor(audio().currentTime * 1000);

    await attempt(async () => {
      // The queue first and the output second, so a speaker that refuses to wake does not leave a
      // half-moved handoff: the list is in the daemon either way, and the selector simply stays here.
      const added = await api("/player/queue", {
        method: "POST",
        body: { paths, mode: "replace", start: false },
      });
      const player = await api("/player/output", { method: "PUT", body: { id: target.id } });
      audio().pause();
      state.device = target.id;
      storeDevice(target.id);

      const item = (added && added.queue && added.queue[index]) || null;
      if (item && item.id) {
        await api(`/player/queue/${encodeURIComponent(item.id)}/play`, { method: "PUT" });
        if (positionMs > 0) {
          await api("/player/seek", { method: "PUT", body: { position_ms: positionMs } });
        }
      }
      await refreshPlayer();
      if (!item) renderPlayer(player);
    }, "handoff");
  }

  /** A speaker → this device: copy the list, start at the same track and second, pause the room. */
  async function handoffToHere() {
    await attempt(async () => {
      const player = await api("/player");
      const items = (player.queue || []).filter((item) => item.path);
      if (!items.length) {
        banner("That speaker's queue has nothing this device can play.");
        return;
      }
      const index = Math.max(
        0,
        items.findIndex((item) => item.id && item.id === player.item_id)
      );
      const positionMs = player.item_progress_ms || 0;

      // Paused rather than stopped, and that is a deliberate kindness to the room: pause keeps the
      // daemon's own position, so whoever was listening there can press play and carry on from
      // where the music was when somebody took it away.
      if (player.state === "play") await api("/player/pause", { method: "PUT" });

      state.device = LOCAL_DEVICE;
      storeDevice(LOCAL_DEVICE);
      state.local.queue = items.map((item) => ({
        path: item.path,
        title: item.title || item.path,
        artist: item.artist || null,
        album: item.album || null,
      }));
      state.local.shuffle = false;
      localReorder(index);
      state.local.cursor = state.local.order.indexOf(index);
      if (state.local.cursor < 0) state.local.cursor = 0;
      localPersist();
      await localLoad({ autoplay: true });
      if (positionMs > 0) audio().currentTime = positionMs / 1000;
      localRender();
    }, "handoff");
  }

  /**
   * Read the daemon's output list without making it a poll.
   *
   * Called on unlock and whenever the selector is opened, and **silent on failure**: in this
   * device's own mode a host with no OwnTone is a perfectly good configuration, so "no speakers
   * visible" is a sentence under the selector rather than an error in the banner.
   */
  async function refreshDevices() {
    try {
      const player = await api("/player");
      state.outputs = player.outputs || [];
      reconcileDevice();
      const view = localMode() ? localView() : player;
      renderDevices(view);
      renderDeviceNote(view);
      renderHandoff(view);
    } catch (error) {
      if (error.status === 401) return;
      state.outputs = [];
      state.deviceKey = null;
      renderDevices(null);
    }
  }

  /**
   * Make sure `state.device` names something that exists.
   *
   * Three ways it can stop doing so: the host turned local playback off, the speaker this device
   * was pointed at left the network, or storage carries an id from an installation ago. All three
   * end the same way — pointed at whatever is actually playing, or at this device. The one outcome
   * ruled out is a selector showing a name while the transport underneath it belongs to something
   * else.
   */
  function reconcileDevice() {
    if (localMode()) return;
    if (state.outputs.some((output) => output.id === state.device)) return;
    const fallback =
      state.outputs.find((output) => output.selected && output.selectable) ||
      state.outputs.find((output) => output.selectable);
    if (LOCAL_PLAYBACK && (!fallback || state.device === LOCAL_DEVICE)) {
      state.device = LOCAL_DEVICE;
    } else if (fallback) {
      state.device = fallback.id;
    } else if (LOCAL_PLAYBACK) {
      state.device = LOCAL_DEVICE;
    }
    state.deviceKey = null;
  }

  function wireDevices() {
    const node = el("device");
    node.addEventListener("change", () => selectDevice(node.value));
    // Opening the picker is the moment the list has to be right: a speaker that was switched on
    // since the last poll should be in it, and in this device's own mode nothing else is asking.
    node.addEventListener("focus", () => {
      if (localMode()) refreshDevices();
    });
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
          //
          // The tags go with the path, not only the path: on the phone output there is nothing that
          // can read a file's metadata, and the job already knows what it just wrote.
          const finished = {
            path: job.path,
            title: (job.tags && job.tags.title) || title,
            artist: (job.tags && job.tags.artist) || null,
            album: (job.tags && job.tags.album) || null,
          };
          li.appendChild(
            (() => {
              const box = make("div", "row-actions");
              box.appendChild(
                button("Play", () => enqueue(finished, "replace", true), "primary")
              );
              box.appendChild(button("Queue", () => enqueue(finished, "append", false)));
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
   * Queue one track, on whichever output is chosen.
   *
   * Takes a *track* rather than a path because the two modes need different things from it. The
   * house speaker needs only the path — `GET /library` answers with paths relative to the music
   * root, `GET /jobs` with the absolute path the ingest wrote, both are accepted and both are
   * re-checked against the music root server-side, so traversal is not filtered here but is
   * impossible there. The phone needs the title and artist too, because on that side there is
   * nothing that can read a file's tags: every caller already has them, so they come along.
   */
  function enqueue(track, mode, start) {
    if (localMode()) {
      selectTab("now");
      return localEnqueue([track], mode, start);
    }
    return attempt(async () => {
      await api("/player/queue", {
        method: "POST",
        body: { paths: [track.path], mode, start },
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
      button("Play", () => enqueue(track, "replace", true), "primary"),
      button("Queue", () => enqueue(track, "append", false)),
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
    // Both outputs: the house queue may have been holding this file (and a 409 is the answer when it
    // is the track currently loaded), and the phone's queue would otherwise keep a row that can only
    // ever fail to load — the one place where "the server is the truth" needs a local counterpart.
    localForget(track.path);
    await refreshTransport();
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
   * On the house speaker this is one request: the server reads the file, resolves every entry under
   * the music root and hands the lot to the player at once — so a playlist cannot leave the queue
   * half-replaced with playback already running. The answer *is* the new player state, which is why
   * that branch redraws from it rather than polling for it.
   *
   * On the phone it is two: read the playlist, then enqueue the tracks here. There is no third
   * option — `PUT /playlists/{name}/play` means "tell the daemon to play this", and the daemon is
   * not the output. Entries that resolve to nothing are left out with a count, which is the same
   * thing the server-side path does and the one failure a playlist is most likely to have (see
   * README, Playlists).
   */
  function playPlaylist(playlist, mode, start) {
    if (localMode()) {
      return attempt(async () => {
        const full = await api(`/playlists/${encodeURIComponent(playlist.name)}`);
        const entries = (full && full.entries) || [];
        const tracks = entries
          .filter((entry) => !entry.missing && entry.track)
          .map((entry) => ({
            path: entry.track.path,
            title: entry.track.title,
            artist: entry.track.artist,
            album: entry.track.album,
          }));
        if (!tracks.length) {
          banner(`${playlist.name} has nothing playable in it.`);
          return;
        }
        selectTab("now");
        await localEnqueue(tracks, mode, start);
        const missing = entries.length - tracks.length;
        if (missing) {
          banner(
            `${missing} track${missing === 1 ? "" : "s"} in ${playlist.name} ` +
              `${missing === 1 ? "is" : "are"} missing and ${missing === 1 ? "was" : "were"} skipped.`
          );
        }
      }, "playlist");
    }
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
      // In phone mode the element's own `timeupdate` is the clock — it knows the real position four
      // times a second, so adding a second per second here would double-count it.
      if (localMode()) return;
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
    // `refreshTransport`, so phone mode does not ask OwnTone about a session it is not using: on a
    // host with no player at all that would be a 503 in the banner every two seconds, under a
    // screen that is playing music perfectly well.
    await refreshTransport();
    // Jobs are only worth polling while something is in flight or the ingest tab is on screen.
    if (state.jobsActive || state.tab === "add") await refreshJobs();
    if (!state.token) return;
    state.timer = window.setTimeout(tick, POLL_MS);
  }

  function wireVisibility() {
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") {
        // Note what is *not* stopped: the audio element. In phone mode the whole point is that the
        // music keeps going with the screen off — only the polling and the progress redraw stop,
        // because nothing is looking at them.
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
    wireDevices();
    wireAudio();
    unlockSilenceOnFirstTouch();
    wireMediaSession();
    registerWorker();

    el("btn-handoff").addEventListener("click", handoff);

    // Restored before the gate, so unlocking lands on the device this page was last pointed at
    // rather than flicking over to it a moment later.
    state.device = LOCAL_PLAYBACK ? readStoredDevice() : "";
    localRestore();
    renderDevices(null);

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
