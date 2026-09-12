//! Agents — provisioning the optional agent runtime that runs beside the box.
//!
//! An "agent" is a Hermes profile with its own Matrix account on THIS box. The box owns
//! provisioning because it holds the registration token; the agent container owns running
//! the model. The two meet at a handoff file on a shared volume — credentials never ride
//! account-data, which the phone can read.
//!
//! What the phone's one-tap setup triggers, in order:
//!   1. register `@<localpart>:<onion>` against the local client API (token-gated UIA),
//!   2. write the credentials to the handoff volume for the agent container to pick up,
//!   3. create a DM room and invite the agent, so it has somewhere to talk,
//!   4. publish the roster to account-data, which is what makes the phone's Agents app
//!      show it — and, crucially, what keeps it OUT of Messaging.
//!
//! Step 4 is the security-relevant one: the phone decides "human or AI" from this roster
//! and nothing else. A username is a string a remote box can choose, so inferring it from
//! the user id would let a federated peer masquerade as (or hide) an AI.

use serde_json::{json, Value};

/// The roster the phone reads. Mirrors `ai.tournesol.privacylodge.pairings` in spirit:
/// box-published, owner-only, and the single source of truth for a client-side decision.
pub const AGENTS_ACCOUNT_DATA_TYPE: &str = "ai.tournesol.privacylodge.agents";

/// Where the box drops credentials for the agent container. A shared volume, not
/// account-data: an access token is secret material and account-data is readable by every
/// device signed into the owner's account.
const HANDOFF_PATH: &str = "/handoff/matrix.env";
/// One file per agent, `<localpart>.env`, same shape as [`HANDOFF_PATH`].
///
/// The single `matrix.env` above is still written for the FIRST agent, and is still what an
/// older agent image reads. Keeping both means a box can be rolled back to a pre-multi-agent
/// image without stranding its original agent — the extra agents simply go quiet until the
/// newer image is back, rather than the whole add-on breaking.
const HANDOFF_AGENTS_DIR: &str = "/handoff/agents";
/// The first agent's localpart. Fixed, because it is also the one that maps to Hermes's
/// *default* profile; every later agent gets a profile named after its own localpart.
const DEFAULT_LOCALPART: &str = "hermes-ai";
/// Written by the agent container (not by us) so the owner's phone can be given the WebUI
/// password. Absent on a box with no agents installed, which is exactly right.
const HANDOFF_WEBUI_PASSWORD: &str = "/handoff/webui-password";

/// What the owner chose in the Add-agent wizard.
///
/// Everything except `name` is optional: an empty [`AgentSpec::provider`] means "same as my
/// other agents", which clones the default profile — the behaviour before the wizard existed.
///
/// `api_key` is secret and deliberately short-lived in memory: it arrives on the guarded
/// command channel (the box clears the command before provisioning) and goes straight into
/// the agent's own handoff file at 0600. It is never written to account data.
#[derive(Default)]
pub struct AgentSpec {
    pub name: String,
    pub provider: String,
    pub api_key: String,
    pub base_url: String,
    pub model: String,
}

/// One provisioned agent.
pub struct Provisioned {
    pub user_id: String,
    pub display_name: String,
    pub room_id: Option<String>,
    /// An agent the box no longer tracks — its account and room outlived a hand-deletion.
    /// Listed so the owner can clear it up; never treated as a live agent.
    pub leftover: bool,
}

/// Register an agent account using the box's registration token.
///
/// tuwunel offers exactly one flow (`m.login.registration_token`) because `config.rs`
/// renders `allow_registration = true` together with a token — it is never an open
/// registration server. UIA is two calls: one to get a session id, one to satisfy the stage.
async fn register(
    client: &reqwest::Client,
    base: &str,
    localpart: &str,
    password: &str,
    join_token: &str,
) -> Result<(String, String, String), String> {
    let url = format!("{base}/_matrix/client/v3/register");

    // Stage 1 — an empty auth POST is expected to 401 WITH a session id. That is the
    // documented UIA handshake, not an error, so a 401 here is the success path.
    let r = client
        .post(&url)
        .json(&json!({}))
        .send()
        .await
        .map_err(|e| format!("couldn't reach the homeserver: {e}"))?;
    let v: Value = r
        .json()
        .await
        .map_err(|e| format!("bad registration response: {e}"))?;
    let session = v
        .get("session")
        .and_then(|s| s.as_str())
        .ok_or_else(|| "the homeserver didn't offer a registration session".to_string())?;

    // Stage 2 — satisfy the token stage and take the account.
    let r = client
        .post(&url)
        .json(&json!({
            "username": localpart,
            "password": password,
            "inhibit_login": false,
            "auth": {
                "type": "m.login.registration_token",
                "token": join_token,
                "session": session,
            }
        }))
        .send()
        .await
        .map_err(|e| format!("registration failed: {e}"))?;
    let v: Value = r
        .json()
        .await
        .map_err(|e| format!("bad registration response: {e}"))?;

    if let Some(err) = v.get("error").and_then(|e| e.as_str()) {
        // M_USER_IN_USE is worth naming plainly: it means setup already ran once, and the
        // fix is to reuse the existing agent rather than to retry.
        let code = v.get("errcode").and_then(|c| c.as_str()).unwrap_or("");
        if code == "M_USER_IN_USE" {
            return Err(format!("an agent named '{localpart}' already exists on this box"));
        }
        return Err(err.to_string());
    }

    let user_id = v
        .get("user_id")
        .and_then(|u| u.as_str())
        .ok_or("registration returned no user_id")?
        .to_string();
    let token = v
        .get("access_token")
        .and_then(|t| t.as_str())
        .ok_or("registration returned no access_token")?
        .to_string();
    let device = v
        .get("device_id")
        .and_then(|d| d.as_str())
        .unwrap_or("")
        .to_string();
    Ok((user_id, token, device))
}

/// Hand the agent its credentials on the shared volume.
///
/// Written 0600 and only to the handoff mount. If the mount isn't there the agent add-on
/// isn't installed, which is a normal state (agents are optional), so say so clearly
/// rather than failing with an io error the owner can't act on.
fn write_handoff(
    localpart: &str,
    spec: &AgentSpec,
    onion: &str,
    user_id: &str,
    token: &str,
    device: &str,
    owner: &str,
    room: Option<&str>,
) -> Result<(), String> {
    let dir = std::path::Path::new(HANDOFF_PATH)
        .parent()
        .ok_or("bad handoff path")?;
    if !dir.is_dir() {
        return Err("the agents add-on isn't installed on this box".to_string());
    }
    // The homeserver URL is the box's own loopback: the agent shares our network namespace,
    // so it must NOT take a Tor circuit to reach the box sitting next to it.
    let body = format!(
        "MATRIX_HOMESERVER=http://127.0.0.1:{port}\n\
         MATRIX_USER_ID={user_id}\n\
         MATRIX_ACCESS_TOKEN={token}\n\
         MATRIX_DEVICE_ID={device}\n\
         PL_BOX_ONION={onion}\n\
         PL_OWNER={owner}\n\
         PL_AGENT_LOCALPART={localpart}\n",
        port = crate::config::HOMESERVER_PORT + crate::config::off(),
    );
    // Only emit what the owner actually chose. An absent key is meaningfully different from
    // an empty one: absent means "inherit the default profile", empty would mean "this
    // provider needs no key", and the container branches on exactly that.
    let mut body = body;
    for (k, v) in [
        ("PL_AGENT_PROVIDER", &spec.provider),
        ("PL_AGENT_MODEL", &spec.model),
        ("PL_AGENT_BASE_URL", &spec.base_url),
        ("PL_AGENT_API_KEY", &spec.api_key),
    ] {
        if !v.is_empty() {
            body.push_str(&format!("{k}={v}\n"));
        }
    }
    // The agent's own room, so the container can set it as the Hermes home channel. Without
    // it every new agent opens with "No home channel is set for Matrix" — a question the
    // owner shouldn't have to answer, since we just created the one room it has.
    if let Some(room) = room.filter(|r| !r.is_empty()) {
        body.push_str(&format!("PL_AGENT_ROOM={room}\n"));
    }

    let agents_dir = std::path::Path::new(HANDOFF_AGENTS_DIR);
    std::fs::create_dir_all(agents_dir)
        .map_err(|e| format!("couldn't create the agent handoff directory: {e}"))?;
    write_0600(&agents_dir.join(format!("{localpart}.env")), &body)?;

    // The first agent also keeps the legacy single-file path — see HANDOFF_AGENTS_DIR.
    if localpart == DEFAULT_LOCALPART {
        write_0600(std::path::Path::new(HANDOFF_PATH), &body)?;
    }
    Ok(())
}

/// Write secret material with the mode set at creation, never after.
///
/// `File::create` then `set_permissions` would leave a window where the file exists at the
/// umask default — on a shared volume that is a real (if brief) exposure of an access token.
fn write_0600(path: &std::path::Path, body: &str) -> Result<(), String> {
    use std::io::Write;
    #[cfg(unix)]
    use std::os::unix::fs::OpenOptionsExt;

    let mut opts = std::fs::OpenOptions::new();
    opts.write(true).create(true).truncate(true);
    #[cfg(unix)]
    opts.mode(0o600);
    let mut f = opts
        .open(path)
        .map_err(|e| format!("couldn't write the agent handoff: {e}"))?;
    f.write_all(body.as_bytes())
        .map_err(|e| format!("couldn't write the agent handoff: {e}"))?;
    Ok(())
}

/// Turn a display name the owner typed into a Matrix localpart / Hermes profile name.
///
/// Both have to accept it: Matrix localparts are permissive, but a Hermes profile name must
/// match `^[a-z0-9][a-z0-9_-]{0,63}$`, so that stricter rule is the one we satisfy. Anything
/// outside `[a-z0-9-]` becomes a hyphen, runs collapse, and the result is trimmed to a
/// sensible length. Returns None when nothing usable survives (e.g. a name that was all
/// emoji), so the caller can ask for a different one rather than invent a name.
fn slugify(name: &str) -> Option<String> {
    let mut out = String::new();
    let mut last_dash = true; // leading dashes are not allowed, so start as if we just wrote one
    for ch in name.trim().chars() {
        let c = ch.to_ascii_lowercase();
        if c.is_ascii_alphanumeric() {
            out.push(c);
            last_dash = false;
        } else if !last_dash && out.len() < 32 {
            out.push('-');
            last_dash = true;
        }
        if out.len() >= 32 {
            break;
        }
    }
    let s = out.trim_matches('-').to_string();
    if s.is_empty() || !s.starts_with(|c: char| c.is_ascii_alphanumeric()) {
        return None;
    }
    Some(s)
}

/// Create the owner↔agent room and invite the agent.
///
/// `is_direct` + an explicit invite mirrors what the phone's contact exchange builds, so the
/// room reads as a normal DM to the client — the ONLY thing that marks it as an agent room
/// is the roster, which is what keeps that decision un-spoofable.
async fn create_room(
    client: &reqwest::Client,
    base: &str,
    owner_token: &str,
    agent_user: &str,
    name: &str,
) -> Option<String> {
    let r = client
        .post(format!("{base}/_matrix/client/v3/createRoom"))
        .bearer_auth(owner_token)
        .json(&json!({
            "preset": "trusted_private_chat",
            "is_direct": true,
            "name": name,
            "invite": [agent_user],
            // Encryption ON at creation, not bolted on later.
            //
            // Without this the room is plaintext, and everything downstream that promises
            // otherwise quietly fails instead: the phone refuses to send ("refusing to send
            // into a non-encrypted room" — correctly), and the agent, which we start with
            // MATRIX_E2EE_MODE=required, won't work there either. The result is a chat that
            // looks fine and silently swallows every message.
            //
            // It must be in initial_state: `m.room.encryption` can only be turned on, never
            // off, so setting it at creation is both safe and the only way to guarantee no
            // plaintext event ever exists in the room's history.
            "initial_state": [{
                "type": "m.room.encryption",
                "state_key": "",
                "content": { "algorithm": "m.megolm.v1.aes-sha2" },
            }],
        }))
        .send()
        .await
        .ok()?;
    let v: Value = r.json().await.ok()?;
    v.get("room_id").and_then(|r| r.as_str()).map(String::from)
}

/// Set the agent WebUI's password to one the owner chose.
///
/// Written to the handoff volume, which the agent container watches — it restarts its WebUI
/// on the new secret rather than requiring the container to be recreated. 0600, and the
/// caller has already cleared the command that carried it out of account data.
pub fn set_webui_password(password: &str) -> Result<(), String> {
    let path = std::path::Path::new(HANDOFF_WEBUI_PASSWORD);
    let dir = path.parent().ok_or("no handoff directory")?;
    if !dir.exists() {
        return Err("the agents add-on isn't installed on this box".into());
    }
    #[cfg(unix)]
    {
        use std::io::Write;
        use std::os::unix::fs::OpenOptionsExt;
        let mut f = std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(path)
            .map_err(|e| format!("couldn't write the WebUI password: {e}"))?;
        f.write_all(password.as_bytes())
            .map_err(|e| format!("couldn't write the WebUI password: {e}"))?;
    }
    #[cfg(not(unix))]
    std::fs::write(path, password)
        .map_err(|e| format!("couldn't write the WebUI password: {e}"))?;
    Ok(())
}

// --- Sessions within an agent ---------------------------------------------------------------
//
// A session IS a room. That isn't a workaround for the lack of a session API — it's how the
// agent runtime already works: its session key is `agent:<profile>:matrix:dm:<room_id>`, so a
// second room with the same agent is a second conversation with its own history, and the
// adapter auto-joins an invite from the owner. Nothing on the agent side had to change.
//
// The box keeps the list because it is the only party that knows which rooms it created for
// which agent. Deriving it on the phone would mean guessing from room membership, and a wrong
// guess here puts an AI in the Messaging list next to real people.
pub const SESSIONS_ACCOUNT_DATA_TYPE: &str = "ai.tournesol.privacylodge.agent_sessions";

async fn read_sessions(client: &reqwest::Client, url: &str, token: &str) -> Value {
    match client.get(url).bearer_auth(token).send().await {
        Ok(r) if r.status().is_success() => r.json().await.unwrap_or_else(|_| json!({})),
        _ => json!({}),
    }
}

/// Start a new conversation with an agent: a fresh room, which the agent joins by itself.
///
/// The agent is invited, not joined for it — `_on_invite` accepts invites from the owner, and
/// going through the invite is what makes the room a DM in the agent's own `m.direct`.
pub async fn session_new(
    client: &reqwest::Client,
    base: &str,
    sessions_url: &str,
    owner_token: &str,
    agent_user: &str,
    title: &str,
) -> Result<String, String> {
    if agent_user.is_empty() {
        return Err("no agent given".into());
    }
    let title = title.trim();
    let name = if title.is_empty() { "New conversation" } else { title };
    let room = create_room(client, base, owner_token, agent_user, name)
        .await
        .ok_or("couldn't create the conversation on your box")?;

    let mut all = read_sessions(client, sessions_url, owner_token).await;
    let list = all
        .as_object_mut()
        .ok_or("the session list is corrupt")?
        .entry(agent_user.to_string())
        .or_insert_with(|| json!([]));
    if let Some(arr) = list.as_array_mut() {
        arr.push(json!({ "room_id": room, "title": name, "created_ts": now_ms() }));
    }
    client
        .put(sessions_url)
        .bearer_auth(owner_token)
        .json(&all)
        .send()
        .await
        .map_err(|e| format!("couldn't record the conversation: {e}"))?;
    Ok(room)
}

/// Delete one conversation. The agent's OTHER sessions are untouched.
///
/// Leaves and forgets the room, then drops it from the list. Order matters: a room dropped
/// from the list but still joined would be an orphan the phone can no longer name, and it
/// would surface in Messaging as a chat with an AI — the exact thing the agent split prevents.
pub async fn session_delete(
    client: &reqwest::Client,
    base: &str,
    registry_url: &str,
    sessions_url: &str,
    owner_token: &str,
    room_id: &str,
) -> Result<String, String> {
    if room_id.is_empty() {
        return Err("no conversation given".into());
    }
    let gone = leave_and_forget(client, base, registry_url, owner_token, room_id).await;
    let mut all = read_sessions(client, sessions_url, owner_token).await;
    if let Some(map) = all.as_object_mut() {
        for (_agent, list) in map.iter_mut() {
            if let Some(arr) = list.as_array_mut() {
                arr.retain(|s| s.get("room_id").and_then(|r| r.as_str()) != Some(room_id));
            }
        }
    }
    let _ = client
        .put(sessions_url)
        .bearer_auth(owner_token)
        .json(&all)
        .send()
        .await;
    // The transcript itself lives in the agent container's session store, keyed
    // `agent:<profile>:matrix:dm:<room_id>`. It is NOT deleted here — see the note in
    // HANDOFF: the multiplexed gateway holds ONE session store for every profile, so
    // reaching into it is a bigger change than this command. Say what was actually done.
    if gone {
        Ok("Conversation deleted.".into())
    } else {
        Err("couldn't delete that conversation on your box".into())
    }
}

// --- Device-code sign-in (Codex and friends) ------------------------------------------------
//
// Some providers can't be finished with a key typed on a phone: Codex is a device-code OAuth
// flow that prints a short user code, waits for the owner to enter it at OpenAI, and only then
// yields a credential. The blocking half runs INSIDE the agent container (`pp-auth-daemon`,
// which needs a pty — see its docstring); our job here is only to ask for it and to relay what
// comes back, so the owner sees the code on the phone they're holding.
const HANDOFF_AUTH_REQUEST: &str = "/handoff/auth-request.json";
const HANDOFF_AUTH_STATUS: &str = "/handoff/auth-status.json";

/// What the agent container is currently reporting about a sign-in.
///
/// `Pending` is the interesting one: it carries the code the owner has to type, and it exists
/// precisely because the flow is NOT finished — the phone shows it while the box keeps waiting.
pub enum AuthProgress {
    /// Started, but the code hasn't been printed yet.
    Starting,
    Pending { verification_uri: String, user_code: String },
    Ok(String),
    Failed(String),
}

/// Ask the agent container to begin a device-code sign-in. Returns immediately — the flow takes
/// as long as the owner takes to open a browser, so the caller polls [`auth_progress`].
pub fn auth_start(id: &str, provider: &str) -> Result<(), String> {
    if !std::path::Path::new("/handoff").exists() {
        return Err("the agents add-on isn't installed on this box".into());
    }
    // The container spawns a process named by this field, so pin its shape here as well as
    // there. Two independent checks on the same value is the point, not duplication.
    if provider.is_empty()
        || provider.len() > 40
        || !provider
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
    {
        return Err("unknown sign-in provider".into());
    }
    // Clear any status left by a previous attempt BEFORE asking for a new one, or the first
    // poll reads the last run's verdict and reports a sign-in that hasn't happened yet.
    let _ = std::fs::remove_file(HANDOFF_AUTH_STATUS);
    write_0600(
        std::path::Path::new(HANDOFF_AUTH_REQUEST),
        &json!({ "id": id, "provider": provider, "issued_ts": now_ms() }).to_string(),
    )
}

/// Ask the container to abandon a sign-in the owner backed out of, so it stops polling OpenAI.
pub fn auth_cancel(id: &str) -> Result<(), String> {
    write_0600(
        std::path::Path::new(HANDOFF_AUTH_REQUEST),
        &json!({ "id": id, "action": "cancel" }).to_string(),
    )
}

/// Read the container's status for `id`. `None` = nothing about THIS request yet.
///
/// Matching on the id matters: the status file is a single slot, so without it a stale verdict
/// from an earlier attempt would be reported as this one's.
pub fn auth_progress(id: &str) -> Option<AuthProgress> {
    let raw = std::fs::read_to_string(HANDOFF_AUTH_STATUS).ok()?;
    let v: Value = serde_json::from_str(&raw).ok()?;
    if v.get("id").and_then(|i| i.as_str()) != Some(id) {
        return None;
    }
    match v.get("state").and_then(|s| s.as_str()).unwrap_or("") {
        "starting" => Some(AuthProgress::Starting),
        "pending" => Some(AuthProgress::Pending {
            verification_uri: v.get("verification_uri")?.as_str()?.to_string(),
            user_code: v.get("user_code")?.as_str()?.to_string(),
        }),
        "ok" => Some(AuthProgress::Ok(
            v.get("message")
                .and_then(|m| m.as_str())
                .unwrap_or("Signed in.")
                .to_string(),
        )),
        "cancelled" => Some(AuthProgress::Failed("Sign-in cancelled.".into())),
        "error" => Some(AuthProgress::Failed(
            v.get("error")
                .and_then(|m| m.as_str())
                .unwrap_or("sign-in didn't complete")
                .to_string(),
        )),
        _ => None,
    }
}

/// Publish the roster. This is what the phone keys "is this an AI?" off.
pub async fn publish_registry(
    client: &reqwest::Client,
    url: &str,
    owner_token: &str,
    agents: &[Provisioned],
) -> Result<(), String> {
    let list: Vec<Value> = agents
        .iter()
        .map(|a| {
            json!({
                "user_id": a.user_id,
                "display_name": a.display_name,
                "group": if a.leftover { "Leftovers" } else { "" },
                "description": if a.leftover {
                    "Left over from a deleted agent — remove it to clear the chat"
                } else {
                    "Runs on your box"
                },
                "room_id": a.room_id,
            })
        })
        .collect();
    // The agent WebUI's own onion, so the phone's Agent settings app knows where to tunnel.
    // Published here rather than in boxstatus because this is the agent-shaped key the
    // phone already reads, and it's empty/absent on a box with no agents.
    // In the container PRIVACY_LODGE_DATA_DIR=/data, so the agent hidden service's hostname
    // lands here. Empty until tor has minted it (first boot after this port was added).
    let webui_onion = std::fs::read_to_string(
        std::path::Path::new(&crate::envcompat::var("DATA_DIR").unwrap_or("/data".into()))
            .join("data/tor/hs-agent/hostname"),
    )
    .map(|s| s.trim().to_string())
    .unwrap_or_default();
    // The WebUI password, generated inside the agent container and mirrored to the handoff
    // volume for us. Handing it to the phone is what makes the password real rather than
    // decorative — otherwise the owner meets a login form for a secret they've never seen.
    //
    // Trade-off, stated plainly: this puts a secret in the owner's account data, which
    // tuwunel stores unencrypted. It is not a new exposure — the same box already holds the
    // password file, the homeserver, and the agent — and reading it needs the owner's own
    // access token over the onion. What it buys is that the password is a genuine second
    // gate on a shell-capable UI instead of a value only the container knows.
    let webui_password = std::fs::read_to_string(HANDOFF_WEBUI_PASSWORD)
        .map(|s| s.trim().to_string())
        .unwrap_or_default();
    // The phone's half of the agent onion's client-auth keypair (see
    // config::ensure_agent_client_auth). Without it the phone cannot fetch the service's
    // descriptor at all, so this key IS the app's access to Agent settings — it has to
    // travel with the address it unlocks.
    let webui_auth_key = std::fs::read_to_string(
        std::path::Path::new(&crate::envcompat::var("DATA_DIR").unwrap_or("/data".into()))
            .join("data/tor/agent-client-auth.key"),
    )
    .map(|s| s.trim().to_string())
    .unwrap_or_default();
    let r = client
        .put(url)
        .bearer_auth(owner_token)
        .json(&json!({
            "agents": list,
            "webui_onion": webui_onion,
            "webui_port": crate::config::AGENT_WEBUI_ONION_PORT,
            "webui_password": webui_password,
            "webui_auth_key": webui_auth_key,
            "updated_ts": crate::agent::now_ms(),
        }))
        .send()
        .await
        .map_err(|e| format!("couldn't publish the agent roster: {e}"))?;
    if r.status().is_success() {
        Ok(())
    } else {
        Err(format!("couldn't publish the agent roster ({})", r.status()))
    }
}

/// Loopback port the agent container's WebUI listens on. It shares the box's network
/// namespace, so this is reachable from here exactly when the add-on is running.
const AGENT_WEBUI_PORT: u16 = 8787;

/// Is the agent runtime actually up? See the note in [`setup`] for why this beats testing
/// for the handoff directory.
pub async fn agent_running(client: &reqwest::Client) -> bool {
    client
        .get(format!("http://127.0.0.1:{AGENT_WEBUI_PORT}/"))
        .timeout(std::time::Duration::from_secs(3))
        .send()
        .await
        .is_ok()
}

/// A throwaway password for the agent account. Nothing ever types it: the agent
/// authenticates with the access token from registration, and this only exists because
/// the register endpoint requires one. Generated, used once, never stored.
fn random_password() -> String {
    use rand::RngCore;
    let mut b = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut b);
    b.iter().map(|x| format!("{x:02x}")).collect()
}

pub fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// Read the roster the box previously published, so setup is additive rather than
/// clobbering agents provisioned earlier.
pub async fn read_registry(
    client: &reqwest::Client,
    url: &str,
    owner_token: &str,
) -> Vec<Provisioned> {
    let Ok(r) = client.get(url).bearer_auth(owner_token).send().await else {
        return Vec::new();
    };
    let Ok(v) = r.json::<Value>().await else {
        return Vec::new();
    };
    v.get("agents")
        .and_then(|a| a.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|o| {
                    Some(Provisioned {
                        user_id: o.get("user_id")?.as_str()?.to_string(),
                        display_name: o
                            .get("display_name")
                            .and_then(|d| d.as_str())
                            .unwrap_or("Agent")
                            .to_string(),
                        room_id: o
                            .get("room_id")
                            .and_then(|r| r.as_str())
                            .map(String::from),
                        leftover: o
                            .get("group")
                            .and_then(|g| g.as_str())
                            .is_some_and(|g| g == "Leftovers"),
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

/// Re-publish the roster from the handoff file, which is the box's own record of what it
/// provisioned. Called on every supervisor pass, like the status blob.
///
/// Two reasons this is not just belt-and-braces: a roster written once can be missed by a
/// client that wasn't synced at that moment (and account-data has no re-delivery), and the
/// phone's human/AI split is only correct while it holds a roster — so publishing it
/// repeatedly is what makes that split self-healing rather than dependent on one write
/// landing at the right time.
pub async fn republish_from_handoff(
    client: &reqwest::Client,
    base: &str,
    registry_url: &str,
    owner_token: &str,
    onion: &str,
    owner_user: &str,
) {
    // EVERY provisioned agent, not just the first. Republishing from one handoff file would
    // rewrite the roster down to a single entry on the next supervisor pass — silently
    // deleting every other agent from the phone's Agents app while their accounts, rooms and
    // runtimes carried on existing. The roster is the phone's only source of truth for
    // "which of these are AI", so a partial republish is worse than none.
    let mut user_ids: Vec<String> = Vec::new();
    let mut files: Vec<std::path::PathBuf> = vec![std::path::PathBuf::from(HANDOFF_PATH)];
    if let Ok(rd) = std::fs::read_dir(HANDOFF_AGENTS_DIR) {
        files.extend(
            rd.filter_map(|e| e.ok())
                .map(|e| e.path())
                .filter(|p| p.extension().is_some_and(|x| x == "env")),
        );
    }
    for path in files {
        let Ok(body) = std::fs::read_to_string(&path) else {
            continue;
        };
        for line in body.lines() {
            if let Some(v) = line.strip_prefix("MATRIX_USER_ID=") {
                let id = v.trim().to_string();
                // `matrix.env` is a copy of the first agent's file, so it duplicates.
                if !id.is_empty() && !user_ids.contains(&id) {
                    user_ids.push(id);
                }
            }
        }
    }
    if user_ids.is_empty() {
        return; // no agent provisioned (or the add-on isn't installed) — nothing to say
    }
    // Keep whatever the current roster says about each agent (room id, group, name) so a
    // republish never clobbers detail we already published.
    let existing = read_registry(client, registry_url, owner_token).await;
    let roster: Vec<Provisioned> = user_ids
        .into_iter()
        .map(|user_id| match existing.iter().find(|a| a.user_id == user_id) {
            Some(known) => Provisioned {
                user_id: known.user_id.clone(),
                display_name: known.display_name.clone(),
                room_id: known.room_id.clone(),
                // Anything with a handoff file is live, whatever a stale roster said.
                leftover: false,
            },
            // Only reachable for an agent whose roster entry was lost; the localpart is the
            // best name we have left, and it beats showing nothing.
            None => {
                let name = user_id
                    .trim_start_matches('@')
                    .split(':')
                    .next()
                    .unwrap_or("Agent")
                    .to_string();
                Provisioned {
                    display_name: if name == DEFAULT_LOCALPART {
                        "Hermes".to_string()
                    } else {
                        name
                    },
                    user_id,
                    room_id: None,
                    leftover: false,
                }
            }
        })
        .collect();
    let mut roster = roster;
    roster.extend(discover_orphans(client, base, owner_token, onion, owner_user, &roster).await);
    let _ = publish_registry(client, registry_url, owner_token, &roster).await;
}

/// Leave and forget a room, and prune it out of `m.direct`.
///
/// The prune is not optional: a left-and-forgotten DM lingers in that map and resurfaces as a
/// ghost chat, which is the exact symptom this cleanup exists to remove.
async fn leave_and_forget(
    client: &reqwest::Client,
    base: &str,
    registry_url: &str,
    owner_token: &str,
    room: &str,
) -> bool {
    let enc = enc_path(room);
    let left = client
        .post(format!("{base}/_matrix/client/v3/rooms/{enc}/leave"))
        .bearer_auth(owner_token)
        .json(&json!({}))
        .send()
        .await
        .map(|r| r.status().is_success())
        .unwrap_or(false);
    let forgot = client
        .post(format!("{base}/_matrix/client/v3/rooms/{enc}/forget"))
        .bearer_auth(owner_token)
        .json(&json!({}))
        .send()
        .await
        .map(|r| r.status().is_success())
        .unwrap_or(false);
    if let Some((ad_base, _)) = registry_url.rsplit_once("/account_data/") {
        let direct_url = format!("{ad_base}/account_data/m.direct");
        if let Ok(resp) = client.get(&direct_url).bearer_auth(owner_token).send().await {
            if let Ok(map) = resp.json::<Value>().await {
                if let Some(obj) = map.as_object() {
                    let mut kept = serde_json::Map::new();
                    let mut changed = false;
                    for (uid, rooms) in obj {
                        let pruned: Vec<Value> = rooms
                            .as_array()
                            .map(|a| a.iter().filter(|r| r.as_str() != Some(room)).cloned().collect())
                            .unwrap_or_default();
                        if rooms.as_array().is_some_and(|a| a.len() != pruned.len()) {
                            changed = true;
                        }
                        if !pruned.is_empty() {
                            kept.insert(uid.clone(), Value::Array(pruned));
                        } else if rooms.as_array().is_none() {
                            kept.insert(uid.clone(), rooms.clone());
                        }
                    }
                    if changed {
                        let _ = client
                            .put(&direct_url)
                            .bearer_auth(owner_token)
                            .json(&Value::Object(kept))
                            .send()
                            .await;
                    }
                }
            }
        }
    }
    left && forgot
}

/// Clear rooms that cannot be a live conversation with anyone.
///
/// Deliberately narrow, because this runs unattended against the owner's real chat list and
/// leaving a room federates a visible "left" event to whoever is in it. Exactly two shapes
/// qualify, and neither can be a contact:
///
///   1. **Nobody else is in it.** The owner is the only member — every other party has left.
///      There is no one to talk to and no one to notify.
///   2. **A one-to-one with a LOCAL account that is not a known agent.** Local accounts only
///      exist because this box registered them (registration is token-gated and the box is
///      the only thing that registers), so a local id that is not the owner and not in the
///      roster is a deleted agent. A federated peer — a real contact — is never local.
///
/// Everything else, including every room on another onion, is left strictly alone.
pub async fn cleanup_dead_rooms(
    client: &reqwest::Client,
    base: &str,
    registry_url: &str,
    owner_token: &str,
    onion: &str,
    owner_user: &str,
) -> usize {
    let live: Vec<String> = read_registry(client, registry_url, owner_token)
        .await
        .into_iter()
        .filter(|a| !a.leftover)
        .map(|a| a.user_id)
        .collect();
    let suffix = format!(":{onion}");
    let Ok(resp) = client
        .get(format!("{base}/_matrix/client/v3/joined_rooms"))
        .bearer_auth(owner_token)
        .send()
        .await
    else {
        return 0;
    };
    let Ok(v) = resp.json::<Value>().await else {
        return 0;
    };
    let Some(rooms) = v.get("joined_rooms").and_then(|r| r.as_array()) else {
        return 0;
    };
    let mut cleared = 0usize;
    for room in rooms {
        let Some(room) = room.as_str() else { continue };
        let enc = enc_path(room);
        let Ok(resp) = client
            .get(format!("{base}/_matrix/client/v3/rooms/{enc}/members"))
            .bearer_auth(owner_token)
            .send()
            .await
        else {
            continue;
        };
        let Ok(members) = resp.json::<Value>().await else {
            continue;
        };
        let Some(chunk) = members.get("chunk").and_then(|c| c.as_array()) else {
            continue;
        };
        let mut ids: Vec<String> = Vec::new();
        for ev in chunk {
            if let Some(uid) = ev.get("state_key").and_then(|k| k.as_str()) {
                if !ids.iter().any(|u| u == uid) {
                    ids.push(uid.to_string());
                }
            }
        }
        let others: Vec<&String> = ids.iter().filter(|u| *u != owner_user).collect();
        let reason = if others.is_empty() {
            Some("nobody else is in it")
        } else if others.len() == 1
            && others[0].ends_with(&suffix)
            && !is_reserved_local(others[0])
            && !live.iter().any(|l| l == others[0])
        {
            Some("a deleted agent's room")
        } else {
            None
        };
        if let Some(why) = reason {
            if leave_and_forget(client, base, registry_url, owner_token, room).await {
                cleared += 1;
                eprintln!("[privacy-lodge] agents: cleared {room} — {why}");
            } else {
                eprintln!("[privacy-lodge] agents: could NOT clear {room} ({why})");
            }
        }
    }
    cleared
}

/// Local accounts that are NOT agents and must never be offered for removal.
///
/// tuwunel/conduwuit runs a server admin bot (`@conduit:`) and the owner shares an admin room
/// with it. It is local, it is not the owner, and it is in no roster — so every heuristic for
/// "a leftover agent" matches it exactly. Removing it would throw away the box's own admin
/// channel. Caught in testing: the Agents app listed `conduit` under Leftovers.
fn is_reserved_local(user_id: &str) -> bool {
    let lp = user_id.trim_start_matches('@').split(':').next().unwrap_or("");
    matches!(lp, "conduit" | "conduwuit" | "tuwunel" | "server" | "admin" | "notices")
}

/// Find agent accounts the box no longer tracks, so the owner can clear them up.
///
/// Agents deleted before `remove()` existed left their Matrix account and their room behind,
/// in no roster and no handoff file. Because the phone's human/AI split is roster-driven, the
/// moment the box republished without them they were reclassified as PEOPLE and surfaced in
/// Messaging next to real contacts — with no way to get rid of them, since the Agents screen
/// only lists what the roster names.
///
/// So: list them, and let the owner delete them with the control that already exists. This
/// only ever ADDS rows to the roster — nothing is removed automatically. That matters,
/// because the identification below is a heuristic and a wrong guess that merely shows an
/// extra row is recoverable, while a wrong guess that deletes a chat is not.
///
/// The heuristic is exact on a Privacy Lodge box today: registration is token-gated and the box
/// is the only thing that ever registers, so a LOCAL account that is neither the owner nor a
/// known agent can only be an agent the box created earlier. If a box ever gains a second
/// human account, this needs a real marker instead.
async fn discover_orphans(
    client: &reqwest::Client,
    base: &str,
    owner_token: &str,
    onion: &str,
    owner_user: &str,
    known: &[Provisioned],
) -> Vec<Provisioned> {
    // Scanning every room's membership is several round trips, and this runs on the
    // supervisor's regular pass. Orphans appear only when an agent is deleted, so a slow
    // cadence costs the owner nothing and keeps the pass cheap.
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::Mutex;
    static LAST_SCAN: AtomicU64 = AtomicU64::new(0);
    // The throttle must return the PREVIOUS result, not nothing. Returning an empty vec
    // between scans republished a roster without the leftovers, so every pass in the 5-minute
    // gap silently un-listed what the scan had just found — the roster oscillated and the
    // last write (the empty one) is what the phone saw.
    static CACHED: Mutex<Vec<(String, String, String)>> = Mutex::new(Vec::new());
    let replay = || -> Vec<Provisioned> {
        CACHED
            .lock()
            .map(|c| {
                c.iter()
                    .filter(|(uid, _, _)| !known.iter().any(|a| &a.user_id == uid))
                    .map(|(uid, name, room)| Provisioned {
                        user_id: uid.clone(),
                        display_name: name.clone(),
                        room_id: Some(room.clone()),
                        leftover: true,
                    })
                    .collect()
            })
            .unwrap_or_default()
    };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let last = LAST_SCAN.load(Ordering::Relaxed);
    if last != 0 && now.saturating_sub(last) < 300 {
        return replay();
    }
    LAST_SCAN.store(now, Ordering::Relaxed);

    let suffix = format!(":{onion}");
    let Ok(resp) = client
        .get(format!("{base}/_matrix/client/v3/joined_rooms"))
        .bearer_auth(owner_token)
        .send()
        .await
    else {
        return Vec::new();
    };
    let Ok(v) = resp.json::<Value>().await else {
        return Vec::new();
    };
    let Some(rooms) = v.get("joined_rooms").and_then(|r| r.as_array()) else {
        return Vec::new();
    };

    let mut out: Vec<Provisioned> = Vec::new();
    for room in rooms {
        let Some(room) = room.as_str() else { continue };
        let enc = enc_path(room);
        // FULL member state, not joined_members. A dead agent may have been invited and never
        // accepted, or have left — both still leave the owner staring at a dead chat, and
        // both are invisible to joined_members. Measured: scanning only joined members found
        // one of the three leftovers on this box.
        let Ok(resp) = client
            .get(format!("{base}/_matrix/client/v3/rooms/{enc}/members"))
            .bearer_auth(owner_token)
            .send()
            .await
        else {
            continue;
        };
        let Ok(members) = resp.json::<Value>().await else {
            continue;
        };
        let Some(chunk) = members.get("chunk").and_then(|c| c.as_array()) else {
            continue;
        };
        let mut people: Vec<(String, String)> = Vec::new();
        for ev in chunk {
            let Some(uid) = ev.get("state_key").and_then(|k| k.as_str()) else {
                continue;
            };
            let content = ev.get("content");
            // `leave` for the OWNER would mean they already left; that room is not in
            // joined_rooms anyway. Any other membership means this id belongs to the room.
            let name = content
                .and_then(|c| c.get("displayname"))
                .and_then(|d| d.as_str())
                .unwrap_or("")
                .trim()
                .to_string();
            if !people.iter().any(|(u, _)| u == uid) {
                people.push((uid.to_string(), name));
            }
        }
        let locals: Vec<&str> = people
            .iter()
            .map(|(u, _)| u.as_str())
            .filter(|u| *u != owner_user && u.ends_with(&suffix))
            .collect();
        if !locals.is_empty() || people.len() <= 2 {
            eprintln!(
                "[privacy-lodge] agents: room {} — {} member(s), {} local non-owner",
                room,
                people.len(),
                locals.len()
            );
        }
        // A one-to-one room. A bigger room is not an agent DM, and leaving it would be
        // someone else's conversation.
        if people.len() > 2 {
            continue;
        }
        for (uid, dn) in &people {
            let uid = uid.as_str();
            if uid == owner_user || !uid.ends_with(&suffix) || is_reserved_local(uid) {
                continue; // the owner, a federated peer, or the server's own admin bot
            }
            if known.iter().any(|a| a.user_id == uid) || out.iter().any(|a| a.user_id == uid) {
                continue;
            }
            let name = if dn.is_empty() {
                uid.trim_start_matches('@').split(':').next().unwrap_or("agent").to_string()
            } else {
                dn.clone()
            };
            out.push(Provisioned {
                user_id: uid.to_string(),
                display_name: name,
                room_id: Some(room.to_string()),
                leftover: true,
            });
        }
    }
    if let Ok(mut c) = CACHED.lock() {
        *c = out
            .iter()
            .map(|a| {
                (
                    a.user_id.clone(),
                    a.display_name.clone(),
                    a.room_id.clone().unwrap_or_default(),
                )
            })
            .collect();
    }
    eprintln!(
        "[privacy-lodge] agents: scanned {} room(s), {} leftover agent room(s) found",
        rooms.len(),
        out.len()
    );
    out
}

/// The whole one-tap flow. Returns the message the phone shows.
pub async fn setup(
    client: &reqwest::Client,
    base: &str,
    registry_url: &str,
    owner_token: &str,
    onion: &str,
    join_token: &str,
    owner: &str,
    spec: &AgentSpec,
) -> Result<String, String> {
    if join_token.is_empty() {
        return Err("this box has no registration token, so it can't create an agent".into());
    }
    // Refuse early when the add-on isn't running — otherwise we'd mint a Matrix account for
    // a runtime that will never come up, and the owner would have to clean it up.
    //
    // Probe the agent's own port rather than testing for the handoff directory: the volume
    // is mounted on the box unconditionally (the box is its writer), so its existence says
    // nothing about whether the agent is actually there. A reply on the WebUI port does —
    // it can only come from the agent container sharing our network namespace. Any HTTP
    // status counts, including 302/401: we're proving something is listening, not
    // authenticating.
    if !agent_running(client).await {
        return Err("the agents add-on isn't running on this box — enable it with \
                    './pl-box agents on', then try again"
            .to_string());
    }

    let mut existing = read_registry(client, registry_url, owner_token).await;

    // No name = the first-run, one-tap path: a fixed first agent keeps that idempotent and
    // predictable, and it is the one that maps to Hermes's *default* profile. `-ai` is a
    // readable convention, NOT the thing that makes it an agent (the roster is).
    //
    // A name = the owner adding another agent. It gets its own account, its own room, and
    // its own Hermes profile, so it can run a different model (or a different subscription)
    // from the first one.
    let name = spec.name.as_str();
    let (localpart, display) = if name.trim().is_empty() {
        (DEFAULT_LOCALPART.to_string(), "Hermes".to_string())
    } else {
        let slug = slugify(name).ok_or_else(|| {
            "that name has no letters or numbers in it — pick something like \"Codex\"".to_string()
        })?;
        // Reserving the default localpart matters: it is the only one wired to the default
        // profile, so letting a second agent claim it would put two Matrix accounts on one
        // profile and Hermes would refuse the duplicate credential at gateway startup.
        if slug == DEFAULT_LOCALPART {
            return Err("that name is reserved for the first agent — pick another".to_string());
        }
        (slug, name.trim().to_string())
    };

    let agent_user = format!("@{localpart}:{onion}");
    if existing.iter().any(|a| a.user_id == agent_user) {
        return if localpart == DEFAULT_LOCALPART {
            Ok("agents are already set up on this box".to_string())
        } else {
            Err(format!("you already have an agent called {display}"))
        };
    }

    let password = random_password();
    let (user_id, token, device) = register(client, base, &localpart, &password, join_token).await?;
    // Room BEFORE handoff: the handoff carries the room id so the container can set it as
    // the agent's home channel. The container starts provisioning the moment the handoff
    // file lands, so writing it first would race — and the room is one API call away.
    let room_id = create_room(client, base, owner_token, &user_id, &display).await;
    if room_id.is_none() {
        // Not fatal: the account and runtime are live, and a room can be created later.
        // Better to report a working-but-incomplete setup than to fail the whole thing.
        eprintln!("[privacy-lodge] agent: account created but the room wasn't — will retry later");
    }

    write_handoff(
        &localpart,
        spec,
        onion,
        &user_id,
        &token,
        &device,
        owner,
        room_id.as_deref(),
    )?;

    existing.push(Provisioned {
        user_id: user_id.clone(),
        display_name: display.clone(),
        room_id,
        leftover: false,
    });
    publish_registry(client, registry_url, owner_token, &existing).await?;

    Ok(format!("{display} is ready — say hello in the Agents app"))
}

/// Remove an agent: clear the chat, retire the account, forget the profile.
///
/// This is the inverse of [`setup`], and it exists because not having it was a real defect —
/// deleting an agent by hand (`rm` the handoff file and the profile) removed the *agent* and
/// left the *Matrix identity* behind. The account and its room outlived it, and because the
/// phone's human/AI split is roster-driven and deliberately fail-closed, the moment the box
/// republished a roster without that id the phone reclassified it as a PERSON — so a deleted
/// agent reappeared as a fake human in Messaging, next to real contacts. It also burned the
/// name forever (`M_USER_IN_USE`).
///
/// `target` is the agent's full Matrix user id. Deliberately not a display name: this is
/// destructive, near-irreversible, and the phone always knows the exact id of the row the
/// owner tapped. Resolving a human-typed name here would be one fuzzy match away from
/// deleting the wrong agent.
///
/// Ordering is chosen so a partial failure leaves the box in the safest state:
///
///   1. leave + forget the room       — what the owner actually asked for (the chat goes away)
///   2. drop the handoff entry        — stops the container serving it, frees `/handoff`
///   3. republish the roster          — the phone stops listing it
///   4. deactivate the account        — LAST, and best-effort: it is the only irreversible
///                                      step, and failing it costs a reusable name, nothing
///                                      more. Never let it fail the whole removal.
pub async fn remove(
    client: &reqwest::Client,
    base: &str,
    registry_url: &str,
    owner_token: &str,
    owner_user: &str,
    target: &str,
) -> Result<String, String> {
    let target = target.trim();
    if target.is_empty() || !target.starts_with('@') || !target.contains(':') {
        return Err("that doesn't look like an agent id".to_string());
    }
    let localpart = target
        .trim_start_matches('@')
        .split(':')
        .next()
        .unwrap_or("")
        .to_string();
    if localpart.is_empty() {
        return Err("that doesn't look like an agent id".to_string());
    }
    // The first agent IS Hermes's default profile, and the box's own legacy handoff path
    // points at it. Removing it would leave a running profile with no credentials rather
    // than a clean box, so refuse rather than half-do it.
    if localpart == DEFAULT_LOCALPART {
        return Err("the first agent can't be removed — remove the agents add-on instead".into());
    }

    let known = read_registry(client, registry_url, owner_token).await;
    let entry = known.iter().find(|a| a.user_id == target);
    let display = entry
        .map(|a| a.display_name.clone())
        .unwrap_or_else(|| localpart.clone());

    // 1. The chat. Room id from the roster when we have it; otherwise ask the homeserver for
    //    the owner's rooms and pick the one this agent is in — which is what makes ORPHANS
    //    (agents deleted by hand before this existed) cleanable at all, since they are in no
    //    roster and no handoff file.
    let mut room_id = entry.and_then(|a| a.room_id.clone());
    if room_id.is_none() {
        room_id = find_room_with(client, base, owner_token, target).await;
    }
    let mut chat_cleared = false;
    if let Some(room) = room_id.as_deref() {
        let enc = enc_path(room);
        // Leave first: forgetting a room you are still in is rejected.
        let left = client
            .post(format!("{base}/_matrix/client/v3/rooms/{enc}/leave"))
            .bearer_auth(owner_token)
            .json(&json!({}))
            .send()
            .await
            .map(|r| r.status().is_success())
            .unwrap_or(false);
        // Forget is what removes it from the owner's room list rather than just marking them
        // as departed — without it the dead chat keeps showing.
        let forgot = client
            .post(format!("{base}/_matrix/client/v3/rooms/{enc}/forget"))
            .bearer_auth(owner_token)
            .json(&json!({}))
            .send()
            .await
            .map(|r| r.status().is_success())
            .unwrap_or(false);
        chat_cleared = left && forgot;
        if !chat_cleared {
            eprintln!("[privacy-lodge] agent remove: leave={left} forget={forgot} for {room}");
        }
    }

    // 1b. Prune `m.direct`. A left-and-forgotten DM can linger in that account-data map and
    //     resurface later as a ghost chat — the box's own peer-removal path already learned
    //     this and prunes there for the same reason. Skipping it here would leave exactly the
    //     symptom this whole feature exists to fix.
    if let Some((ad_base, _)) = registry_url.rsplit_once("/account_data/") {
        let direct_url = format!("{ad_base}/account_data/m.direct");
        if let Ok(resp) = client.get(&direct_url).bearer_auth(owner_token).send().await {
            if let Ok(map) = resp.json::<Value>().await {
                if let Some(obj) = map.as_object() {
                    let mut kept = serde_json::Map::new();
                    let mut changed = false;
                    for (uid, rooms) in obj {
                        if uid == target {
                            changed = true;
                            continue;
                        }
                        // Also drop the room itself from any other user's list — an agent DM
                        // should only be under its own id, but a stale entry elsewhere would
                        // resurrect the same room.
                        if let (Some(arr), Some(room)) = (rooms.as_array(), room_id.as_deref()) {
                            let pruned: Vec<Value> = arr
                                .iter()
                                .filter(|r| r.as_str() != Some(room))
                                .cloned()
                                .collect();
                            if pruned.len() != arr.len() {
                                changed = true;
                            }
                            if !pruned.is_empty() {
                                kept.insert(uid.clone(), Value::Array(pruned));
                            }
                            continue;
                        }
                        kept.insert(uid.clone(), rooms.clone());
                    }
                    if changed {
                        let _ = client
                            .put(&direct_url)
                            .bearer_auth(owner_token)
                            .json(&Value::Object(kept))
                            .send()
                            .await;
                    }
                }
            }
        }
    }

    // 2. The handoff entry. Its absence is what tells the container to stop serving the
    //    profile, so this must happen before we report success.
    let handoff = std::path::Path::new(HANDOFF_AGENTS_DIR).join(format!("{localpart}.env"));
    let agent_token = std::fs::read_to_string(&handoff)
        .ok()
        .and_then(|body| {
            body.lines()
                .find_map(|l| l.strip_prefix("MATRIX_ACCESS_TOKEN=").map(str::to_string))
        });
    let _ = std::fs::remove_file(&handoff);

    // 3. The roster, rebuilt from what's left on disk.
    // The onion is whatever the agent's own id says — no need to plumb it in separately.
    let onion = target.split(':').nth(1).unwrap_or("");
    republish_from_handoff(client, base, registry_url, owner_token, onion, owner_user).await;

    // 4. Retire the account, so the name can be used again. Uses the AGENT's own token from
    //    the handoff file — the box never kept the password, and tuwunel has no HTTP admin
    //    API, so this is the only route that doesn't need the owner to drive an admin room by
    //    hand. Orphans have no handoff file and therefore no token: their chat still goes
    //    away, their name stays taken, and we say so rather than pretending.
    let mut name_freed = false;
    if let Some(tok) = agent_token {
        let ok = client
            .post(format!("{base}/_matrix/client/v3/account/deactivate"))
            .bearer_auth(&tok)
            .json(&json!({ "erase": true }))
            .send()
            .await
            .map(|r| r.status().is_success())
            .unwrap_or(false);
        name_freed = ok;
        if !ok {
            eprintln!("[privacy-lodge] agent remove: couldn't deactivate {target} (name stays taken)");
        }
    }

    Ok(match (chat_cleared, name_freed) {
        (true, true) => format!("{display} is gone. The name is free to use again."),
        (true, false) => format!("{display} is gone. The name stays taken on this box."),
        (false, true) => format!(
            "{display} is removed, but its chat may linger until your phone re-syncs."
        ),
        (false, false) => format!("{display} is removed from your agents."),
    })
}

/// Find the owner's room shared with `user_id`, for agents that predate the roster carrying
/// room ids (and for orphans that are in no roster at all).
async fn find_room_with(
    client: &reqwest::Client,
    base: &str,
    owner_token: &str,
    user_id: &str,
) -> Option<String> {
    let rooms: Value = client
        .get(format!("{base}/_matrix/client/v3/joined_rooms"))
        .bearer_auth(owner_token)
        .send()
        .await
        .ok()?
        .json()
        .await
        .ok()?;
    for room in rooms.get("joined_rooms")?.as_array()? {
        let room = room.as_str()?;
        let enc = enc_path(room);
        let members: Value = match client
            .get(format!("{base}/_matrix/client/v3/rooms/{enc}/joined_members"))
            .bearer_auth(owner_token)
            .send()
            .await
        {
            Ok(r) => match r.json().await {
                Ok(v) => v,
                Err(_) => continue,
            },
            Err(_) => continue,
        };
        if let Some(joined) = members.get("joined").and_then(|j| j.as_object()) {
            // A DM with exactly the owner and this agent. The membership check keeps a
            // group room the agent merely sits in from being mistaken for its own chat.
            if joined.contains_key(user_id) && joined.len() <= 2 {
                return Some(room.to_string());
            }
        }
    }
    None
}

/// Percent-encode a Matrix id for use as a URL path segment.
///
/// Room ids start with `!` and always contain `:` — both of which change the meaning of a
/// path if passed through raw. Written here rather than pulling in a crate: this is the only
/// place in the box that needs it, and the rule (encode everything outside RFC 3986
/// unreserved) is short enough to be obviously correct.
fn enc_path(s: &str) -> String {
    let mut out = String::with_capacity(s.len() * 3);
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}
