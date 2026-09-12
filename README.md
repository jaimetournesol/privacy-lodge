# Privacy Lodge Desktop — your box

> **Take your data back.** Run your own private server — your *box* — that hosts
> your apps, over Tor, end-to-end encrypted, with no corporation in the middle.

Your messages, your friends, your files, your feed — right now they live on
corporate servers, harvested for profit and surveillance. **Privacy Lodge is how you
take them back.** You run your own private server, and it hosts your apps. Your data
lives with you. Everything is end-to-end encrypted and travels **only over Tor** —
no central platform, no clearnet, no ads, no algorithm, no data broker in the path.

**This repo *is* the box.** It runs quietly on your computer (or a Raspberry Pi) and
keeps your personal server alive — reachable only at its own `.onion`, and only by the
people you've paired with. Run it with **Docker on any OS** (Windows, macOS, Linux,
NAS, VPS), or as a native desktop app on Linux — [see below](#run-your-box).

## A platform, not just a messenger

Your box is your always-on private cloud. What it hosts today, and where it's going:

- **✅ Messaging + calls (working today)** — end-to-end encrypted chat and
  voice/video, federated box-to-box over Tor, from the
  [Privacy Bolt app](../privacy-bolt).
- **🚧 Social (planned)** — a Tor-only, self-hosted, federated alternative to the
  corporate timeline: post from your box, follow other boxes, and content flows
  box-to-box over Tor — no company, no algorithm, no clearnet.
- **✅ Agents** — AI agents that run on your own box, reached over Tor like
  everything else. Each gets its own account and its own end-to-end encrypted
  chat, and a box can run several. Their control panel rides a *second* onion
  behind tor v3 client authorisation, because it can run shell commands.
  Optional add-on: `./pl-box agents on`, or choose it at install.
- **🚧 Files (planned)** — the box is built to host apps; the phone becomes the
  launcher for all of them.

## Run your box

### 🐳 Docker is the universal install — it works on every operating system

**Windows, macOS, Linux, and any server / NAS / Raspberry Pi / VPS.** One image, one command,
the same box everywhere. If you're not sure what to use, use Docker.

On **Linux** you can *also* install a native desktop app if you prefer one — that's the only
extra option, and it's optional.

> | Your computer | How to run your box |
> |---|---|
> | **Windows** | 🐳 **Docker** |
> | **macOS** | 🐳 **Docker** |
> | **Linux** | 🐳 **Docker** — or the native [Linux installer](option-2-linux-installer-optional-native-desktop-app) if you'd rather have a desktop app |
> | **server / NAS / Pi / VPS** | 🐳 **Docker** |
>
> **Why is Docker the universal one?** A box is the app *plus* the services it runs on, and
> the Matrix homeserver it uses (tuwunel) publishes **Linux builds only**. Docker carries a
> complete Linux box wherever it runs, so it behaves identically on every OS — which is why
> there's no `.exe` or `.dmg`: an installer there could only ship the window, not the box.

The box is identical whichever route you take — same services, same `.onion`, same phone app.
Set-up is the same one-page flow both ways: **choose a username + password on a local web
page → scan the QR with the phone app → the page closes and everything is managed from your
phone.**

> Get the **phone app** from the [privacy-bolt releases](https://github.com/jaimetournesol/privacy-bolt/releases/latest) (latest APK) — it's the same app wherever your box runs.

### Option 1 · 🐳 Docker — works on every OS (recommended)

Runs anywhere Docker does — **Windows, macOS, Linux, servers, NAS, Raspberry Pi, VPS**. No
desktop needed: the box is reached only over its `.onion`, set up from a browser and managed
from the phone.

**Pull the published image** ([`jaimemelon/privacy-lodge-box`](https://hub.docker.com/r/jaimemelon/privacy-lodge-box)):

```bash
docker pull jaimemelon/privacy-lodge-box:latest
MYVOL=pp-data-$(openssl rand -hex 4)   # your box's data volume — note it down and always reuse it
docker run -d --name privacy-lodge-box --restart unless-stopped -v "$MYVOL":/data \
  -p 127.0.0.1:8470:8470 -e PRIVACY_LODGE_SETUP_BIND=0.0.0.0 jaimemelon/privacy-lodge-box:latest
# open http://127.0.0.1:8470/ → username + password → scan the QR in the phone app
```

That volume holds your box's identity (the onion key) — **keep the name and back it up**; a
different name means a different, empty box.

Or build + manage it with the **`pl-box`** helper (`cd docker && ./pl-box build && ./pl-box init && ./pl-box up`).

Then `status` / `logs` / `backup` / `restore` / `update` as needed. **Full guide:
[docker/README.md](docker/README.md).**

> **Windows / macOS:** install [Docker Desktop](https://www.docker.com/products/docker-desktop/)
> first, then run exactly the commands above — nothing else differs.

### Option 2 · Linux installer — optional native desktop app

**Linux only, and entirely optional** — Docker (Option 1) runs the same box on Linux too.
Choose this if you want a normal desktop application rather than a container.

On first launch the box **opens a one-page setup in your default browser** (username +
password → QR). Once your phone connects, the box runs in the background and is managed from
the phone; the desktop window is just a status shell.

**Install the package** from the [latest release](https://github.com/jaimetournesol/privacy-lodge/releases/latest)
— it bundles everything the box runs on (Tor, the homeserver, the call services), so there is
nothing else to fetch:

```bash
sudo apt install ./Privacy Lodge_*_amd64.deb     # or: sudo dnf install ./Privacy Lodge-*.rpm
privacy-lodge                                     # launch — setup opens in your browser
```
(Or run the `.AppImage` directly, no install needed.)

Once it's running, keep it current from **PP Config → Software update** on your phone: your
box checks for a signed update over Tor and installs it only when you approve.

<details><summary>Build from source instead (developers)</summary>

```bash
source ~/.nvm/nvm.sh
pnpm install
./scripts/fetch-sidecars.sh                 # fetch tuwunel + tor + call sidecars
pnpm tauri build --no-bundle                # → src-tauri/target/release/privacy-lodge
./src-tauri/target/release/privacy-lodge      # launch — setup opens in your browser
```

For hot-reload development use `pnpm tauri dev`. To build a *bundled* installer (sidecars
included) run `./scripts/stage-sidecars.sh` first, then `pnpm tauri build`.
</details>

## What the desktop app does

It's a [Tauri 2](https://tauri.app) + Svelte shell that owns the lifecycle (spawn,
health-check, restart, shutdown) of the local services that make up your box:

- **tuwunel** — an embedded Matrix homeserver (your personal server)
- **C-tor** — the Tor client providing the onion transport between boxes
- **Caddy** — TLS-terminating fed-proxy + onion sites (the paired-peer federation
  allowlist, plus the lk-jwt / client-API / wss-SFU sites the phone's calls need)
- **coturn + LiveKit SFU + lk-jwt** — the call backend (voice/video over Tor)

Nothing binds to the open internet: every service listens on loopback, and Tor maps
your box's `.onion` ports onto those listeners. You are reachable only over Tor, and
only by boxes you've paired with.

## Updating an existing box

Already running a box and want the latest version **without losing your onion,
account, pairings, or chats**? See **[UPDATING.md](UPDATING.md)** — the short version
is: quit the app cleanly, replace the binary, restart. Never delete the data dir.

## Dev quickstart

```bash
source ~/.nvm/nvm.sh            # Node v24 via nvm
pnpm install
./scripts/fetch-sidecars.sh     # fetch tuwunel + tor into the runtime bin dir
pnpm tauri dev
```

`fetch-sidecars.sh` extracts `tuwunel` from the upstream OCI image (requires Docker)
and fetches a **pinned, current `tor`** — the Tor Expert Bundle (`TOR_EB_VER`, currently
15.0.18 → tor 0.4.9.11), *not* whatever tor the build machine happens to have. It enforces
a floor (`TOR_MIN`) and refuses to ship an end-of-life tor: EOL tor (0.4.8.x and older) is
dropped from the network and silently breaks federation, so pinning avoids a box that "looks
fine" but can't reach peers. It also fetches `caddy`, `coturn`, `lk-jwt-service`, and
`livekit-server` (bundled **v1.13.1**, from `livekit/livekit-server`) for federation +
Element Call. A missing `livekit-server` just means group calls are off until it's installed.
Binaries land in `$HOME/.local/share/ai.tournesol.privacylodge/bin` by default — override with
`PRIVACY_LODGE_BIN_DIR`. Run with `--uninstall` to remove them.

## Demo mode

The app runs **without sidecars**. If the supervisor can't find (or fails to start)
`tuwunel`/`tor`, it falls back to demo mode: the full UI is browsable with stubbed
data, and an **amber banner** is shown so it's unmistakable that nothing real is
running. This keeps UI work unblocked and makes first-clone DX painless.

## Runtime layout

Everything lives under the platform app-data dir (`ai.tournesol.privacylodge`):

```
<app_data_dir>/
├── config/        # generated tuwunel + tor configuration
├── tor/           # tor data dir (onion keys, state)
├── tuwunel-db/    # homeserver database
└── bin/           # sidecar binaries (dev: populated by fetch-sidecars.sh)
```

## Ports

Everything binds **loopback only**; Tor maps the box's `.onion` ports onto these
loopback listeners (federation, calls, and client API are reached only via the onion).

| Service                    | Loopback bind    | Onion port | Why                                                 |
| -------------------------- | ---------------- | ---------- | --------------------------------------------------- |
| tuwunel homeserver         | `127.0.0.1:8118` | `8008`, `80` | client API; never exposed off-box. `80` mirrors `8008` so SDK clients that derive calls (e.g. account data) from the bare server_name reach tuwunel |
| tor SOCKS proxy            | `127.0.0.1:9150` | —          | outbound federation; Tor Browser's port, avoids system tor 9050 |
| Caddy fed-proxy            | `127.0.0.1:8449` | `8448`     | TLS-terminates inbound federation + enforces the paired-peer allowlist (tuwunel has none of its own) |
| Caddy lk-jwt (TLS)         | `127.0.0.1:8445` | `8443`     | lk-jwt over **TLS on the onion** — the phone's Element Call reaches it via Tor's HTTP-CONNECT tunnel, which only carries TLS |
| Caddy client API (TLS)     | `127.0.0.1:8455` | `8009`     | client API over **TLS on the onion**, same reason — so EC can discover the call focus (`rtc_foci`) |
| Caddy wss SFU              | `127.0.0.1:7444` | `7443`     | LiveKit signaling over wss (EC refuses ws://) |
| LiveKit SFU / TCP media    | `127.0.0.1:7880` / `7881` | (via 7443) | TCP-only (Tor carries no UDP) |
| coturn                     | `127.0.0.1:3479` | `3478`     | TURN relay; media rides Tor (TCP) and is forwarded locally to the SFU |
| lk-jwt-service             | `127.0.0.1:8082` | (via 8443) | mints a LiveKit JWT from a Matrix OpenID token |

> The plain-http onion ports (`8082` lk-jwt, `8008` client API) aren't reachable
> over Tor's HTTP-CONNECT tunnel (TLS-only), so Caddy **also** serves lk-jwt and the
> client API over TLS on dedicated onion ports `8443` / `8009`. The phone app's call
> code targets exactly these — that's how Element Call in the WebView discovers the
> call focus and connects over Tor. (`PRIVACY_LODGE_PORT_OFFSET` shifts the loopback
> binds so two boxes can share one host; onion ports stay standard.)

## QR pairing folds peers into the federation allowlist

Privacy Lodge only federates with boxes you've paired with. The phone's QR contact
exchange drives this: when the owner scans a peer's code, the phone records the
peer's box onion in the owner's Matrix account data
(`ai.tournesol.privacylodge.pairings`). The box **watches** that account data
(`supervisor.rs`), folds any new peer onion into the fed-proxy allowlist, re-renders
the Caddyfile, and hot-reloads Caddy — so the two boxes start federating with no
manual step. (Pair codes can also be exchanged box-to-box directly; both paths land
in `pairings.json` → `render_caddyfile`.)

## Licence

Privacy Lodge is **AGPL-3.0-or-later** — see [`LICENSE`](LICENSE). The programs a box bundles and
runs (Element Call, tuwunel, Tor, LiveKit, Caddy, coturn, lk-jwt-service) keep their own
licences, with a written source offer for the AGPL ones, in
[`THIRD-PARTY-LICENSES.md`](THIRD-PARTY-LICENSES.md).

## Docs

- UX design: [`../docs/redesign/2026-06-ux-design.md`](../docs/redesign/2026-06-ux-design.md)
- Plan: [`../docs/redesign/2026-06-portability-plan.md`](../docs/redesign/2026-06-portability-plan.md)
  (design: [`2026-06-portability-design.md`](../docs/redesign/2026-06-portability-design.md))

## Status

| Area                                    | Status     |
| --------------------------------------- | ---------- |
| Onboarding flow (D1–D5)                 | ✅ done    |
| Dashboard (D6)                          | ✅ done    |
| Sidecar supervisor + demo mode          | ✅ done    |
| Federation allowlist + QR-pair watch    | ✅ done    |
| Element Call backend over Tor (lk-jwt + LiveKit SFU + coturn, TLS on onion) | ✅ done |
| People / Boxes / Agent / Settings pages | ❌ not yet |
| `externalBin` packaging + signing       | ❌ not yet |
| Auto-updater                            | ❌ not yet |

### Apps on the box (the ecosystem)

| App                                     | Status     |
| --------------------------------------- | ---------- |
| Messaging + voice/video calls           | ✅ working |
| Social — Tor-only federated timeline (ActivityPub over onion) | 🚧 planned |
| File storage / sync                     | 🚧 planned |
| Agents (several per box, own account + E2EE room each) | ✅ working |
| Cross-box agent mesh                    | 🚧 planned |
