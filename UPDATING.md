# Updating your box without losing your settings

Your Privacy Lodge box keeps **all** of its identity and history in one place: its
**data directory**. Updating means replacing the **app** — *never* the data directory.
As long as you don't touch the data dir, an update keeps your **.onion address, your
account, your paired contacts, and all your chats**.

## Where your settings live

Everything that matters is under the app-data directory (do **not** delete it):

| OS | Path |
| --- | --- |
| Linux | `~/.local/share/ai.tournesol.privacylodge/` |
| macOS | `~/Library/Application Support/ai.tournesol.privacylodge/` |
| Windows | `%APPDATA%\ai.tournesol.privacylodge\` |

The parts you must preserve:
- **`tor/hs/`** — your `.onion` **identity** (private key). Lose it and your address
  changes and paired peers can no longer reach you.
- **`tuwunel-db/`** (the homeserver database) — your **account, rooms, and message
  history**.
- **`pairings.json` + `secrets.json`** — your **paired peers** and encrypted secrets.
- `config/` is regenerated on every start — safe to ignore.

> **Golden rule:** update the app, keep the data dir. Never run first-time
> setup / a "fresh"/"reset" deploy on an existing box — that mints a NEW onion and
> wipes your pairings.

## How to update

1. **Quit the running box cleanly.** Use the app's own **Quit** (close the window /
   tray → Quit). This stops all the box's services (tor, tuwunel, Caddy, and the call
   sidecars) in order. **Do not `kill -9` it** — a hard kill can leave the sidecars
   running (orphans); they keep holding the ports, so the new copy fails to bind and
   the dashboard shows *"Could not connect to localhost: Connection refused"* with
   services flapping. (If that happens, see Troubleshooting.)

2. **Get the new version.**
   - From source:
     ```bash
     cd privacy-lodge
     git pull
     pnpm install
     pnpm tauri build          # add --no-bundle for just the binary
     ```
   - Or drop the new prebuilt binary / installer in over the old one.

3. **Start the box again.** It reuses the same data dir → **same onion, same account,
   same chats.**

4. **Verify:** the dashboard shows your **same `.onion` address** and your existing
   conversations. Your phone reconnects to the same box over Tor automatically (no
   re-login) — give it a few seconds.

## What NOT to do

- ❌ Don't delete or move the data directory.
- ❌ Don't re-run first-time setup / a "fresh deploy" on an existing box.
- ❌ Don't `kill -9` the box to stop it — quit it cleanly so its sidecars shut down.

## Troubleshooting: "Could not connect to localhost" / port in use

If a previous stop was a hard kill, old sidecars can survive and hold the ports so the
new box can't start. On a machine running **one** box, clear any leftovers before
starting — **stop the supervisor first so it can't respawn them**, then the sidecars:

```bash
pkill -x privacy-lodge          # the supervisor — kill FIRST
sleep 3
for p in tor tuwunel caddy turnserver livekit-server lk-jwt-service; do pkill -x "$p"; done
sleep 2
# confirm none remain, then launch the app again — same data dir, same onion
pgrep -x -l 'privacy-lodge|tor|tuwunel|caddy|turnserver'
```

Your data dir is untouched by any of this, so nothing is lost — you're only clearing
stuck processes.
