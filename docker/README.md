# Privacy Lodge box in Docker

Run your box as a container — on a Linux server, a NAS, a Raspberry Pi, a cloud VPS, or
Windows/macOS via Docker Desktop. One image, everywhere Docker runs.

**No ports to publish.** The box is reached only through its `.onion` (outbound Tor
rendezvous), so there's nothing to expose or forward. Its whole identity — the onion key,
the admin account, `secrets.json`, and `pairings.json` — lives in **one named volume**.
Lose that volume and the box is gone for good, so **back it up**.

> **Your volume name is unique to your install.** `pl-box init` generates one (e.g.
> `privacy-lodge-data-a1b2c3d4`) and records it in `.env` as `PL_VOLUME`, so two boxes on the
> same host never collide. **It must never change** — pointing the box at a different name
> gives you a new, empty box. Keep `.env` safe alongside your backups.

### Upgrading, and old leftover volumes

- **Already running a box?** Just update — your `.env` has no `PL_VOLUME`, so the box keeps
  using the original `privacy-lodge-data` volume: same onion, same account, nothing to do.
- **Fresh install on a host that still has an old volume?** `pl-box init` generates a new
  volume name, so the leftover `privacy-lodge-data` is **ignored and left untouched** — you get a
  clean box, not a resurrected old one. Delete it yourself (`docker volume rm privacy-lodge-data`)
  once you're sure you don't need it.
- ⚠️ **Don't `init --force` on a machine whose live box uses `privacy-lodge-data`** — that writes a
  *new* volume name and your real box will look like it vanished (it hasn't; the volume is still
  there, just unused). `init` warns you if it spots one. To go back, set
  `PL_VOLUME=privacy-lodge-data` in `.env`.

## Easiest: pull the published image

No build needed — pull it straight from Docker Hub and finish setup in your browser:

```bash
docker pull jaimemelon/privacy-lodge-box:latest
MYVOL=pp-data-$(openssl rand -hex 4)      # your box's data volume — note it down, keep it forever
docker run -d --name privacy-lodge-box --restart unless-stopped -v "$MYVOL":/data \
  -p 127.0.0.1:8470:8470 -e PRIVACY_LODGE_SETUP_BIND=0.0.0.0 \
  jaimemelon/privacy-lodge-box:latest
# then open http://127.0.0.1:8470/ in your browser
```

(Or with compose: set `PL_IMAGE=jaimemelon/privacy-lodge-box:latest` and `docker compose up -d`.)
The rest of this guide covers building the image yourself and the `pl-box` helper.

## Quick start (build it yourself)

Everything goes through the **`pl-box`** helper in this directory. Set-up is a **one-page
web form** — no need to bake a password into config:

```bash
./pl-box build     # once — build the container image from the host-built binary + sidecars
./pl-box init      # box name + a fresh secrets key → .env (leave the password blank)
./pl-box up        # start the box; it prints your setup URL
```

Then open the URL it prints — **http://127.0.0.1:8470/** — in any browser on this machine:

1. Choose a **username + password** (this is what your phone signs in with — keep it safe).
2. The box provisions and shows a **QR code**.
3. **Scan it in the Privacy Bolt app** → you're signed in, all over Tor.

The setup page is loopback-only (host `127.0.0.1` only, never the LAN) and **shuts itself
down the moment your phone connects** — setup is one-time.

> Prefer a scripted/non-interactive setup (CI, headless)? Give `init` a password instead and
> the box provisions straight from it; then `./pl-box qr` prints the connect code to the
> terminal (the pre-web-setup behaviour, still supported).

## All commands

| Command | What it does |
|---|---|
| `./pl-box init` | Create `.env` — box name, a fresh `PL_SECRETS_KEY`, and an optional password (blank ⇒ set it in the browser). |
| `./pl-box build` | Build the `privacy-lodge-box:dev` image (stages the binary + sidecars). |
| `./pl-box up` | Start the box. First run prints the **web-setup URL** (`http://127.0.0.1:8470/`); a provisioned box just resumes. |
| `./pl-box qr` | Print the phone-connect QR in the terminal (for the scripted/password-in-`.env` path). |
| `./pl-box status` | Running? Shows the onion, uptime, and the volume name. |
| `./pl-box logs` | Follow the logs (watch it mint the onion + boot the sidecars). |
| `./pl-box restart` | Restart the box. |
| `./pl-box down` | Stop the box — identity is kept in the volume. |
| `./pl-box update [<version>]` | Update, keeping identity. Docker-Hub install: pull `<version>` (or refresh the current tag), pin it in `.env`, recreate — the box's own update check hands you this command with the version filled in. Source install: rebuild the image + recreate. |
| `./pl-box backup [dir] [--encrypt]` | Bundle the box **and** the agents add-on (onion key, secrets, pairings, agent profiles + keys) → `backups/`. **Do this.** `--encrypt` seals it with a passphrase (AES-256-GCM) — without the passphrase the file is noise, to you too. |
| `./pl-box restore <file>` | Restore into fresh volumes and select them after validation (stop the box first); previous volumes are kept. Old bare-tar backups and `.enc` bundles both work. |
| `./pl-box shell` | Open a shell inside the container. |
| `./pl-box destroy` | Remove the box **and** its volume (asks you to type the box name). |

## Windows

Runs on **Docker Desktop for Windows** — pick either front end (same commands, same box):

- **PowerShell (native):** use `pl-box.ps1`, e.g. `./pl-box.ps1 init`, `./pl-box.ps1 up`,
  `./pl-box.ps1 qr`. Same subcommands as the table above.
- **WSL2 / Git Bash:** use the bash `./pl-box` exactly as on Linux. WSL2 is Docker Desktop's
  default backend (real Linux), so this is the most battle-tested path; Git Bash works too
  (the script disables MSYS path-mangling for container mounts).

**One caveat — the image.** `build` bundles a **Linux** box binary + sidecars that are staged
on a Linux host, so it **can't build on native Windows**. Get the image once, then `up`/`qr`
work natively from PowerShell:

```powershell
# on a Linux box (or in WSL2):  cd docker && ./pl-box build && docker save privacy-lodge-box:dev -o pl-box.tar
docker load -i pl-box.tar      # ← on Windows
.\pl-box.ps1 init ; .\pl-box.ps1 up      # then open http://127.0.0.1:8470/ in your browser
```

(Or build it directly inside WSL2 and run from there.) A self-contained image you can
`docker build` / `docker pull` on any OS is the Stage-2 follow-up.

## Agents (optional add-on)

AI agents that run on your box, reached over Tor like everything else. Off unless you ask
for it — `./pl-box agents on`, or choose it when the installer offers.

```
./pl-box agents on        # install (pulls jaimemelon/privacy-lodge-agent)
./pl-box agents status
./pl-box agents ui        # open the control panel in a browser on THIS machine
./pl-box agents off       # remove the container; the data volume is kept
```

The add-on runs a Codex Conductor and a separate Docker worker. Each has its own
private state volume and Codex device authorization. Open Agents in Privacy Bolt to
sign in, chat with Conductor beside its presentation, manage machine stages, or
connect another Agentnode machine. Stage close buttons close that local view;
they do not stop an agent's work.

The browser control panel uses a separate onion with Tor client authorization and
an authenticated browser session. The worker has no host Docker socket. The host
owns container installation, and agent processes run unprivileged inside them.
The runtime provides Python, Node and Codex; create project virtual environments
inside the allocated workspace rather than changing `/opt/venv`.

`pl-box backup` includes Conductor, worker, peer and handoff volumes, including
provider sign-in state. Protect or encrypt backups accordingly. Restore validates
all archives before extracting into fresh volumes, then selects the restored
identity atomically. It retains the previous volumes and configuration.

For an upgrade to 0.2.0, preserve `.env`, update these helper and Compose files, and
run `./pl-box update 0.2.0`. Legacy agent data is retained, but legacy sessions are
not converted into Agentnode sessions. Both public images use the `0.2.0` tag.
The Agentnode repository remains private: only the reviewed runtime subset is
included in Lodge's public source and runtime image.

## Back up your box — it's the whole identity

An `.onion` address is derived from a secret key that exists **only** in your box's data
volume (the `PL_VOLUME` name in `.env`). If that volume is deleted — or you point the box at a
different name — the address can never come back and your phone is orphaned on a dead box. So
keep a backup (of the volume **and** `.env`):

```bash
./pl-box backup                     # → docker/backups/pl-box-<onion>-N.tgz
```

Recovering onto a new machine (or after an accidental wipe) is the reverse — and it brings
back the **same onion**, so your phone reconnects with no re-pairing:

```bash
./pl-box restore backups/pl-box-….tgz
./pl-box up
```

`PL_SECRETS_KEY` (in `.env`) must also stay the same across restarts — it decrypts
`secrets.json`. `init` generates it once; keep `.env` private (it's `chmod 600` and
git-ignored) and store a copy alongside your backup.

## Verified

- Boots, provisions, mints its onion + admin account inside the container.
- Identity persists in the `privacy-lodge-data` volume; a fresh container **resumes with the
  same onion**, and survives `docker restart` / a host reboot.
- **Reachable over Tor via its `.onion` with no published ports** (proven box-to-box).
- **Full feature parity — voice + video calls included.** All six sidecars run (tor,
  tuwunel, caddy, coturn, livekit-server, lk-jwt-service); coturn comes from apt so it gets
  its correct libs.
- **`backup` → wipe → `restore` round-trips the identity** (same onion returns).

## Without the CLI (plain docker / compose)

The CLI just wraps these. `docker compose` reads the `.env` that `init` wrote (all vars are
optional — leave `PL_PASS` unset for the web-setup flow):

```bash
docker compose up -d           # start; the setup page is published to host 127.0.0.1:8470 only
docker compose logs -f box     # watch it come up (and, in the scripted path, print the QR)
docker compose down            # stop
```

Then open **http://127.0.0.1:8470/** and finish setup in your browser (unless you set
`PL_PASS`, in which case it provisions from that and prints the QR to the logs).

Or one plain `docker run` (identity in the `privacy-lodge-data` volume; publish the setup port
to host loopback only):

```bash
MYVOL=pp-data-$(openssl rand -hex 4)     # pick a name and KEEP it — it holds your box identity
docker volume create "$MYVOL"
docker run -d --name privacy-lodge-box --restart unless-stopped -v "$MYVOL":/data \
  -p 127.0.0.1:8470:8470 \
  -e PRIVACY_LODGE_SETUP_BIND=0.0.0.0 \
  privacy-lodge-box:dev
docker logs -f privacy-lodge-box     # then open http://127.0.0.1:8470/ in your browser
```

⚠️ Write that volume name down. Every later `docker run`, backup, or restore must use the
**same** one — a different name is a different (empty) box, and the onion key is unrecoverable.

*(Prefer the non-interactive path? Drop the two setup lines and add
`-e PL_USER=yourname -e PL_PASS='a-strong-password' -e PL_SECRETS_KEY="$(openssl rand -base64 32)"`.
`PL_USER` has no default — it's required whenever `PL_PASS` is set.)*

## Notes & known limits (Stage 1)

- Build is **Stage 1**: it reuses the prebuilt Tauri binary + sidecars from your machine
  (`build.sh`) — fast to iterate. A shipping image would compile both in a multi-stage
  build.
- **Image is ~1.2 GB / amd64.** The binary is the Tauri GUI (links webkit2gtk), run headless
  under Xvfb, so the image ships webkit/gtk/xvfb just to satisfy it. All the *sidecars* have
  arm64 builds, so the only blocker to a multi-arch (arm64 for a Pi / Apple Silicon) image is
  cross-compiling this webkit-linked binary — cleanest to do alongside **Stage 2** (a headless
  box runner with no Tauri), which also shrinks the image.
- **Setup page:** the one-page web setup is the only local surface, bound to host `127.0.0.1`
  only and live **only until your phone signs in**. Everything else is Tor-only over the `.onion`.
