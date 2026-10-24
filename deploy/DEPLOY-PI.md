# Deploying tunebox on a Raspberry Pi

The main [README](../README.md) describes `./deploy/install.sh` as the whole of it, and on the host
you are sitting at, it is. This guide is the other case: a **headless Pi you reach over ssh**, where
the code lives on your laptop, the music lives on a USB disk, and — optionally — you want the remote
to work when you are not in the house.

Four scripts, in order. Each is idempotent and each takes `--dry-run`.

| Step | Script | Run from | Needed? |
|---|---|---|---|
| 1 | *(router)* | the router's web UI | yes — everything downstream names the Pi's address |
| 2 | `deploy/push.sh` | your laptop | yes |
| 3 | `deploy/owntone-build.sh` | the Pi | only for the house speaker |
| 4 | `deploy/owntone-setup.sh` | the Pi | only for the house speaker |
| 5 | *(router)* | the router's web UI | only for access from outside the house |

Steps 3 and 4 are skippable. Playback on the phone — **This device** in the remote's selector — is
served by tunebox itself and needs no OwnTone at all, so a Pi that only ever feeds headphones stops
after step 2.

## Before you start

- **Raspberry Pi OS, 64-bit if the hardware allows.** The installer's Linux branch targets Debian
  and derivatives.
- **Put the music on a USB SSD, not the SD card.** Ingest writes whole audio files and the nightly
  job writes a health log; an SD card is the wrong medium for both. `--music-root` is how you say so,
  and it has to be a path that is mounted at boot — put it in `/etc/fstab` with `nofail` before you
  start, because a mount point that is an ordinary empty directory makes ingest *work* while the
  library silently accumulates on the card. That failure is why `tunebox healthcheck` reports free
  space and track count together.
- **An ssh alias**, because `push.sh` defaults to one called `tunebox`:

  ```
  # ~/.ssh/config
  Host tunebox
      HostName 192.168.50.93
      User robin
      IdentityFile ~/.ssh/id_ed25519
  ```

  Keeping the Pi's username, hostname and key in the one place ssh already looks is the reason
  `push.sh` takes an alias rather than three flags. `TUNEBOX_PI_HOST` or `--host user@addr` override
  it.
- **Passwordless sudo is not required**, but the installer and the OwnTone build both need sudo, and
  `push.sh` runs the installer over `ssh -t` so a password prompt works.

## 1. Give the Pi a fixed address

Three later things name this address — `TUNEBOX_API_HOST`, the URL inside the `tunebox pair` QR
code, and the route your phone takes through the VPN — so an address that moves breaks all three at
once, silently, with "the icon on my phone stopped working" as the only symptom.

Prefer a **DHCP reservation on the router** over static configuration on the Pi. The Pi stays stock,
there is one authority for the answer, and you cannot create the duplicate-address or wrong-gateway
class of problem.

On an ASUS router: **LAN** → **DHCP Server** → **Manually Assigned IP around the DHCP list** →
*Enable Manual Assignment* = **Yes**. Pick the Pi from the client dropdown (which saves typing the
MAC), enter the address, click the **⊕** to add the row — a row typed but not added is silently
dropped — then **Apply**.

Get the MAC from the Pi, and note that wired and wireless are different ones:

```bash
cat /sys/class/net/eth0/address      # wired
cat /sys/class/net/wlan0/address     # wireless
```

While you are on that page, move **IP Pool Starting Address** up to `.100` and reserve something
below it. ASUS defaults the pool to `.2`–`.254`, so a reservation inside that range is an address
the router may also hand to a guest; narrowing the pool removes the question rather than relying on
the reservation winning.

Then confirm, because a reservation does not apply until the lease is renewed:

```bash
sudo nmcli con down "Wired connection 1" && sudo nmcli con up "Wired connection 1"
ip -4 addr show eth0
```

(Raspberry Pi OS Bookworm and later use NetworkManager. On Bullseye and older it is `dhcpcd`; check
with `systemctl is-active NetworkManager`.)

## 2. Push the checkout and install

```bash
./deploy/push.sh                                    # copy, then install and start
./deploy/push.sh --dry-run                          # print every step, change nothing
./deploy/push.sh --music-root /mnt/ssd/music \
                 --api-host 192.168.50.93           # the two flags worth passing on a first run
./deploy/push.sh --no-install                       # copy only
./deploy/push.sh -- --no-deps                       # everything after `--` goes to install.sh
```

`push.sh` tars the **working tree** — not a git revision — copies it to `~/tunebox-src` on the Pi,
replaces whatever was there, and runs `deploy/install.sh` over ssh. Three consequences worth
knowing:

- **An uncommitted change is testable on the real hardware**, which is the point of having a Pi on
  the desk at all. It also keeps git, your credentials and your ssh keys off the Pi entirely.
- **The staging directory is replaced, not extracted over.** `install.sh` pip-installs from that
  checkout and deliberately not in editable mode, so a file you deleted locally but which survived
  on the Pi would be packaged into the venv and run. Replacing makes the deployed tree equal to your
  local one rather than the union of every push you have ever done.
- **It transfers a tarball over scp rather than using rsync**, because rsync is not on every
  Raspberry Pi OS image. `install.sh` installs it — Phase 5's backup needs it — but that happens
  *after* this transfer, so depending on it here would risk failing on exactly the first run that
  matters. The tree is about 500 KB once ignored paths are dropped, so incremental transfer buys
  nothing.

What `install.sh` then creates on the Pi — the venv, the wrapper scripts, the mode-600 env file, the
systemd unit and the nightly timer — is in the main README under
[Deploying](../README.md#deploying), including the reasoning for a *system* unit rather than
`systemd --user` and the sandboxing it carries. Two Pi-specific notes:

- **`--api-host` matters here in a way it does not on a laptop.** The installer reads the LAN address
  off the default route, which is right on a machine with one interface and wrong on a Pi that has
  both Ethernet and Wi-Fi up. Passing it explicitly is how you decide. It is never `0.0.0.0` —
  PLAN.md 3.2 forbids that, and the installer refuses it.
- **Re-running is the upgrade path.** The token is never regenerated, because that would log out
  every phone in the house with a token prompt as the only clue. `--api-host` and `--music-root` are
  re-asserted only when passed explicitly, so a hand-edited env file survives a push.

Verify, then pair:

```bash
ssh -t tunebox '~/.local/share/tunebox/venv/bin/tunebox doctor'
ssh -t tunebox '~/.local/share/tunebox/venv/bin/tunebox pair'
```

`doctor` checks every dependency, including the one that matters most on a Pi: **a JavaScript
runtime**. Without `yt-dlp-ejs` and a runtime to drive it, metadata extraction keeps working
perfectly while every media download comes back HTTP 403 — and there is no official deno build for
32-bit ARM, which is why tunebox offers yt-dlp `node`/`quickjs`/`bun` as well
(`TUNEBOX_JS_RUNTIMES`). If `doctor` reports no runtime, install one before anything else.

`pair` prints a QR code. Point the phone's camera at it, then use **Add to Home Screen**. Both the
code and the link are the credential itself, so `pair` refuses to write to anything but a terminal.

Set a backup destination while you are in there — on a Pi it is the one setting whose absence you
will regret:

```bash
ssh tunebox
echo 'TUNEBOX_BACKUP_DEST=/mnt/backup/tunebox' >> ~/.config/tunebox/tunebox.env
sudo systemctl restart tunebox
```

A destination on a *different* disk from the library. The same disk is allowed and warned about: it
protects against somebody deleting an album and does nothing whatsoever about the disk dying. The
backup is also the only undo `DELETE /library` has, which is why `TUNEBOX_BACKUP_PRUNE` is off by
default.

## 3. OwnTone, from source

Skip this if you only play on the phone.

**There is no OwnTone package for Raspberry Pi OS.** Measured on this project's Pi, Raspberry Pi OS
trixie, 2026-10:

```
apt-cache policy owntone       -> (no such package)
apt-cache policy forked-daapd  -> Candidate: (none)
sources: deb.debian.org/debian, debian-security, archive.raspberrypi.com
```

So `apt install owntone` is not a route on this hardware and a source build is the only one. That
also settles a question the main README left open: since there is no Debian build, nothing can be
inferred about whether it enables Chromecast — `owntone-build.sh` passes `--enable-chromecast`
itself and then *asserts* it, because a daemon built without it fails at the only thing this project
needs it for, and the failure looks like a speaker that is simply never listed.

```bash
ssh -t tunebox
cd ~/tunebox-src
./deploy/owntone-build.sh              # build, install, register the systemd unit
./deploy/owntone-build.sh --dry-run
./deploy/owntone-build.sh --version 29.4 --no-verify
```

Expect this to take a while on a Pi — it is a C build with a lot of dependencies, and `--jobs`
defaults to `nproc`.

What it does beyond `make install`, each piece load-bearing:

- **Sets `general.uid`** to the invoking user. The shipped conf says `uid = "owntone"` and a source
  build creates no such user, so the daemon exits at startup. Running as your own user is also what
  guarantees read access to a music root under that user's home.
- **Owns the state directories.** `make install` leaves `/usr/local/var/{log,cache,run}` as root,
  but the daemon drops privileges and then has to write its log and its song database.
- **Installs the generated systemd unit**, which `make install` does not — so without this step the
  daemon dies with the terminal that started it and never comes back after a reboot. The unit is
  OwnTone's own (`owntone.service.in` configured with our prefix), carrying upstream's
  `MemoryMax=256M`, a value upstream derived from observations on an rpi3 under load. Installing it
  as `owntone.service` is also exactly what `owntone-setup.sh` looks for.

The tarball digest is **trust-on-first-use**: recorded from this script's first run and checked on
every run afterwards, because the project publishes no checksum. It still turns "a mirror served
something else" into an error rather than a build. `--sha256` replaces it; `--no-verify` skips it
when you bump `--version`.

It deliberately **does not start the daemon**, because at that point the conf still points at
OwnTone's default `/srv/music` and starting it would index the wrong directory first.

## 4. Point OwnTone at the library

```bash
./deploy/owntone-setup.sh              # configure, supervise, find the speaker
./deploy/owntone-setup.sh --dry-run    # show the diff it would apply to owntone.conf
./deploy/owntone-setup.sh --supervise  # start it and keep it started; change no config
```

Four changes to `owntone.conf`, with a timestamped backup taken first. The main README's
[Setting up OwnTone](../README.md#setting-up-owntone) has the full reasoning; the short version is
that two of the four are fixing defaults that are actively wrong for this use:

- `general.trusted_networks` → `{ "localhost" }`. **This is the one that matters.** OwnTone ships
  `{ "lan" }`, which skips its own password for every device on the home network — measured,
  `curl -X PUT http://host:3689/api/player/stop` from anywhere in the house returns `204`. tunebox
  keeps working because it talks to loopback.
- `library.clear_queue_on_stop_disable` → `true`, so the queue outlives the music. The default
  empties it when playback stops, and reaching the end of the last track *is* stopping playback.
- `library.directories` → tunebox's music root, or the daemon never sees an ingested track.
- `general.admin_password` → generated, and written into `tunebox.env` at the same time so the two
  cannot drift.

Then it waits for the speaker to appear in mDNS and writes `TUNEBOX_OUTPUT_ID` — the id rather than
the name, because an id is stable and a name is what somebody renames in the Google Home app. With
more than one candidate it lists them and stops rather than choosing.

**None of the macOS local-network palaver applies here.** On macOS 26 a launchd-started OwnTone is
denied local-network access without any dialog, finds the speaker over mDNS anyway, and then fails
to connect — which is why `deploy/start.sh` exists. Linux has no such gate, so
`sudo systemctl enable --now owntone` is genuinely the whole of it.

```bash
~/.local/share/tunebox/venv/bin/tunebox player status    # is the speaker there?
~/.local/share/tunebox/venv/bin/tunebox doctor           # is everything there?
```

## 5. Reaching it from outside the house

Optional, and the framing matters: **you do not need tunebox reachable from the internet. You need
your phone on your home network from anywhere.** A VPN gives you the second without the first —
nothing about the service changes, no port answers a stranger, and the token stays a LAN-only
secret.

### Do not port-forward 8080

PLAN.md 3.2 opens with it, and the reasons are specific to what this service is rather than general
caution:

- **There is no TLS.** `serve` is plain uvicorn; the token rides in an `X-Tunebox-Token` header in
  cleartext on every request, and the remote polls. Open the port and the shared secret for your
  whole library crosses the public internet unencrypted a few times a minute.
- **The token is all-or-nothing, with no backstop.** One `compare_digest` in
  `TokenAuthMiddleware` is the entire authorisation model. No rate limiting, no lockout, no
  per-route scope. 256 bits is not guessable, so the risk is not brute force — it is that a leaked
  token has no second line of defence and no revocation short of rotating it and re-pairing every
  phone in the house.
- **`POST /ingest` is remote-triggered subprocess execution.** It makes the Pi run yt-dlp and ffmpeg
  on demand, with your household IP and your bandwidth.
- **`DELETE /library` has no trash.** The backup is the undo.

"DMZ host" is worse rather than narrower: on consumer routers it forwards *all* unsolicited inbound
ports to that address, which on this Pi includes ssh and OwnTone's API on 3689 — a port OwnTone binds
on every interface and cannot be told not to.

### Option A: WireGuard on the router (nothing on the Pi changes)

The cleanest option for this layout, and the reason is specific: your phone gets a VPN address that
the router routes into the LAN, so packets arrive at `192.168.50.93:8080` exactly as they do from the
sofa. The existing bind works untouched, and the `tunebox pair` QR code is already correct.

**DDNS first.** If the WireGuard client config is generated before the DDNS hostname exists, the
router bakes your current WAN IP into the exported `Endpoint` line and the config goes stale the next
time your ISP changes it.

**WAN** → **DDNS**: *Enable the DDNS Client* = Yes, Server = `WWW.ASUS.COM`, pick a hostname. Choose
something that is not your name or anything inferable — `asuscomm.com` is an enumerable namespace, so
a guessable name puts your address on a list of "IPs running an ASUS router with DDNS enabled", which
is a pre-filtered target population for the next firmware CVE. It grants nobody anything on its own;
it narrows exploit selection for whoever is holding one.

Watch that page for a warning that the WAN IP is private. If you see one you are behind CGNAT or a
double NAT, and neither DDNS nor WireGuard can work — go to Option B. Cross-check by comparing the
WAN IP on the router's Network Map against `curl ifconfig.me` from inside the house.

Do **not** enable the free Let's Encrypt certificate on that page unless you need HTTPS on the name:
issuing a certificate publishes the hostname to public Certificate Transparency logs, which makes it
searchable even if it was unguessable.

**Then the server.** **VPN** → **VPN Server** → **WireGuard VPN**:

1. Enable the WireGuard server. Leave the port at `51820`. The router opens it in the firewall
   itself — do **not** add a port-forward rule.
2. **Apply.**
3. Add a client, name it, and make sure the intranet/LAN access option is on. Depending on firmware
   this is a checkbox ("Access Intranet") or a dropdown ("Client will use VPN to access" → **Both**
   or **Intranet**, not "Internet only"). This is the setting that decides whether the tunnel can
   reach the Pi at all.
4. **Apply** again. **The QR code icon only appears on the client row after Apply** — each client
   gets a QR square and a download arrow for the `.conf` at the right-hand end of its row.

**On the phone**, install the official WireGuard app, then **+** → **Create from QR code**. Before
toggling it on, open the config and check two lines:

- **`Endpoint`** should be `yourname.asuscomm.com:51820`, not a bare IP. If it is an IP, the DDNS was
  not in place when the client was generated: delete the client, re-add it, re-scan.
- **`AllowedIPs`** is the split-tunnel decision, and it is worth making deliberately:

  ```
  AllowedIPs = 192.168.50.0/24     # only LAN traffic takes the tunnel  ← recommended
  AllowedIPs = 0.0.0.0/0, ::/0     # everything goes home
  ```

  The narrow form is effectively free: WireGuard is silent when there is no traffic for it, and for
  tunebox the only destination you need is one address. The full tunnel hides your DNS and traffic
  metadata on hostile Wi-Fi, but it makes your home *upload* your phone's download cap, adds a
  detour to every packet's latency, turns a power cut at home into no connectivity at all, and
  breaks captive portals — hotel Wi-Fi needs you authenticated before any tunnel can establish.

  Duplicate the config into two tunnels rather than choosing once: a narrow `tunebox` that stays on,
  and a `home-full` you switch on for café Wi-Fi. If you keep the narrow one, delete any
  `DNS = 192.168.50.1` line, or resolution will try to use a server it has no route to.

**Verify it is really the tunnel:** turn the phone's Wi-Fi *off* so you are on mobile data, enable
the tunnel, then open `http://192.168.50.93:8080/`. The router's WireGuard page shows the client's
last handshake and transfer counters, which is the quickest confirmation the tunnel is actually up
rather than the page being served from a cached service worker.

### Option B: Tailscale on the Pi

Use this if you are behind CGNAT, or if you would rather publish no public hostname at all.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale serve --bg http://192.168.50.93:8080
tailscale serve status
```

The `serve` step is not optional, and the reason is the bind: uvicorn is listening on
`192.168.50.93` only, so a request arriving on `tailscale0` at `100.x` reaches nothing. `serve`
proxies it while tunebox stays bound to the LAN address — which is better than rebinding, because the
wildcard is what PLAN.md 3.2 forbids and the installer refuses.

It also gives you a real HTTPS MagicDNS hostname, and that is a secure context — so Chrome on Android
will finally install the page as a standalone PWA and the service worker will exist. On
`http://` LAN addresses it cannot, which the main README flags as an open shortfall.

Two follow-ons. `tunebox pair` builds its QR from `TUNEBOX_API_HOST`, so for the tailnet use
`tunebox pair --no-qr` and open `https://<magicdns-name>/#token=<token>` by hand. And behind the
proxy, the client IP that `TokenAuthMiddleware` logs on a rejected request becomes `127.0.0.1`
rather than the real peer — a small loss, given PLAN.md 3.2 treats that log as the audit trail worth
having.

The `serve` syntax has changed across Tailscale releases; check `tailscale serve --help` if that form
is rejected.

### What about Cloudflare Tunnel

It does give a public HTTPS hostname with no open port, and it works on networks that block VPN UDP.
But it makes the service genuinely internet-reachable, so the single shared token becomes the only
thing between the world and your library — see the four bullets above for why that is not enough. If
you go there, put Cloudflare Access in front so real authentication happens *ahead* of tunebox, and
know that proxying a media stream through the free tier is a grey area in their terms.

### Router settings that matter more than the VPN

The VPN is not the attack surface; the router's own management interfaces are. Roughly in order of
how often each is the real cause of a compromised consumer router:

| Setting | Where | Set to |
|---|---|---|
| Web Access from WAN | Administration → System | **No** |
| AiCloud / AiDisk | AiCloud 2.0 | **Off** |
| UPnP | WAN → Internet Connection | **Off** — it lets any IoT device punch its own hole outward, which is exactly what `tunebox exposure` audits for |
| SSH / Telnet from WAN | Administration → System | LAN only, or off |
| Admin username | Administration → System | not `admin`, long password |
| Firmware auto-update | Administration → Firmware Upgrade | **On** |

Two residual risks worth naming. The **private key on your phone is the credential** — lose the
phone and whoever unlocks it has your LAN; the fix is deleting that peer on the router, which revokes
it without touching `TUNEBOX_TOKEN` or re-pairing anything else. And **the tunnel reaches the whole
LAN**, not just tunebox, including OwnTone on 3689 — broader than the job needs, though
`trusted_networks = { "localhost" }` means the daemon still demands its password from anything that
is not the Pi itself.

Afterwards, confirm you did not accidentally open anything:

```bash
ssh -t tunebox '~/.local/share/tunebox/venv/bin/tunebox exposure'
```

A WireGuard endpoint on the router is not a forwarded port to this host, so this should still report
no mappings to the Pi. If it reports one, something forwarded 8080 and that is the thing to undo.

### Why a VPN endpoint is not the thing PLAN.md 3.2 forbids

3.2 says "no public exposure, and no port forwarding — ever", and a WireGuard endpoint is inbound
UDP, so the apparent contradiction deserves an answer rather than silence.

What 3.2 forbids is *an endpoint that answers*. WireGuard sends nothing in reply to a packet that is
not authenticated with a public key it already knows: a scan of 51820 shows filtered, with no banner,
no version and no handshake with a stranger. There is nothing to brute-force because there is nothing
that responds. Port 8080 would answer — over plaintext HTTP, with a single token, no rate limiting
and a route that runs yt-dlp.

The property 3.2 is protecting is "no packet from the internet reaches the ingest API". A VPN keeps
that property exactly: packets from the internet reach the *router's* crypto, and only an
already-authenticated peer becomes a packet on the LAN. 3.2 says as much about Tailscale —
"strictly better than opening a port" — and this is the same argument with the tunnel terminating on
the router rather than on the Pi.

## Troubleshooting

| Symptom | Cause |
|---|---|
| every download fails with HTTP 403, metadata works | no JavaScript runtime. `doctor` names it; install `node` if there is no deno for your architecture |
| library appears empty but ingest says `done` | the USB disk is not mounted and the mount point is a writable directory. `healthcheck` reports space and track count together for this reason |
| speaker never listed | OwnTone built without `--enable-chromecast`, or the speaker is on a different VLAN — Cast discovery is mDNS and will not cross a boundary |
| phone cannot reach the page on the LAN | `TUNEBOX_API_HOST` is bound to an address the phone cannot route to. Check `ss -ltnp` on the Pi |
| tunnel connects, page times out | `AllowedIPs` does not include the LAN subnet, or intranet access is off on the router's client |
| warning on every ingest about rescan | no player installed. `TUNEBOX_RESCAN_AFTER_INGEST=0` silences it |
| progress bar moves, no sound | `playing_without_output` — the daemon is playing with no speaker attached. The remote says so outright |

```bash
ssh tunebox 'journalctl -u tunebox -n 50 --no-pager'
ssh tunebox 'journalctl -u owntone -n 50 --no-pager'
```

## What is verified, and what is not

Held to the same standard as the rest of these documents.

**Measured on the Pi:** that no `owntone` or `forked-daapd` package exists in Raspberry Pi OS trixie
or the Raspberry Pi archive, which is what `deploy/owntone-build.sh` exists to answer and what makes
the source build the only route.

**Tested, not run:** the installer's Linux branch. The suite puts `uname`/`systemctl`/`sudo` shims on
`PATH` and runs it on macOS, asserting the systemd unit's hardening directives, its
`ReadWritePaths`, `WantedBy=multi-user.target`, and that the dependency on OwnTone is `Wants` rather
than `Requires`. That proves the generated unit, not the hardware.

**Not verified at all:** every router step in sections 1 and 5. They are written from knowledge of
ASUSWRT, not from a run against a router, and that web UI has been reorganised more than once across
388.x / 3006.x and Merlin builds — the sequence and the settings are right, but a label or a nesting
level may differ. Likewise `tailscale serve`'s exact syntax, and whether the WireGuard tunnel ends up
presenting the Pi with the tunnel address or the router's LAN address depends on whether your
firmware routes or NATs the VPN subnet.
