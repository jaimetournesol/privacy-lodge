/**
 * Typed wrappers for every Tauri command in the shared contract,
 * plus a polled Status store.
 *
 * Commands are snake_case in Rust and invoked with camelCase arg keys
 * (Tauri v2 auto-converts). All commands return Result<_, String>, so
 * a rejected invoke gives us a plain string message.
 */
import { invoke } from "@tauri-apps/api/core";
import { writable, type Writable } from "svelte/store";

/* ── contract types ───────────────────────────────────────────── */

export type Phase = "fresh" | "setting_up" | "running" | "stopped" | "error";

export type SetupStage = "starting_services" | "minting_address" | "ready";

export type ServiceState = "starting" | "healthy" | "stopped" | "error";

export interface Service {
  name: "homeserver" | "tor" | "voice";
  state: ServiceState;
}

export interface Status {
  phase: Phase;
  onion: string | null;
  demo_mode: boolean;
  setup_stage: SetupStage | null;
  services: Service[];
  people_count: number;
  paired_count: number;
  box_name: string;
}

export interface RecoveryKit {
  phrase: string;
  onion: string | null;
  created: string;
  box_name: string;
}

export interface ConnectQr {
  /** "privacybolt://connect?hs=<onion>&user=<username>&token=<hex>" */
  payload: string;
  /** A complete inline <svg> QR code. */
  svg: string;
}

/* ── environment guard ────────────────────────────────────────── */

/** True when running inside a Tauri webview (invoke is available). */
export function hasTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/* ── command wrappers ─────────────────────────────────────────── */

export function getStatus(): Promise<Status> {
  return invoke<Status>("get_status");
}

export function suggestPassword(): Promise<string> {
  return invoke<string>("suggest_password");
}

export function beginSetup(args: {
  boxName: string;
  username: string;
  password: string;
}): Promise<null> {
  return invoke<null>("begin_setup", args);
}

export function getRecoveryKit(): Promise<RecoveryKit> {
  return invoke<RecoveryKit>("get_recovery_kit");
}

export function confirmRecoveryWord(args: {
  /** 0-based index into the phrase words. */
  index: number;
  word: string;
}): Promise<boolean> {
  return invoke<boolean>("confirm_recovery_word", args);
}

/** Writes a printable HTML kit into Downloads; returns the absolute path. */
export function saveRecoveryKitHtml(): Promise<string> {
  return invoke<string>("save_recovery_kit_html");
}

export function getConnectQr(): Promise<ConnectQr> {
  return invoke<ConnectQr>("get_connect_qr");
}

/** The loopback URL of the one-page web setup server (feature A). */
export function getSetupUrl(): Promise<string> {
  return invoke<string>("get_setup_url");
}

/** Open the web setup page in the default browser. */
export function openSetupPage(): Promise<null> {
  return invoke<null>("open_setup_page");
}

export function stopBox(): Promise<null> {
  return invoke<null>("stop_box");
}

export function startBox(): Promise<null> {
  return invoke<null>("start_box");
}

export interface LegacyInstall {
  present: boolean;
  containers: string[];
}

/** Detect a running v0.1 Docker appliance so we never silently orphan it. */
export function detectLegacyInstall(): Promise<LegacyInstall> {
  return invoke<LegacyInstall>("detect_legacy_install");
}

export interface JoinInfo {
  onion: string;
  join_token: string;
  /** QR encoding privacybolt://join?hs=…&token=… */
  svg: string;
}

/** What a new person needs to join this box (People → Add a person). */
export function getJoinInfo(): Promise<JoinInfo> {
  return invoke<JoinInfo>("get_join_info");
}

export interface AppInfo {
  version: string;
  data_dir: string;
  demo_mode: boolean;
}

export function appInfo(): Promise<AppInfo> {
  return invoke<AppInfo>("app_info");
}

/** Wipe the box and return to fresh setup. Destructive — confirm first. */
export function resetBox(): Promise<null> {
  return invoke<null>("reset_box");
}

export interface PairCodeOut {
  code: string;
  /** QR of the pair code. */
  svg: string;
}

export interface Pairing {
  onion: string;
  added_at: number;
}

/** Mint a 15-minute pair code for a friend's box to accept. */
export function pairCreate(): Promise<PairCodeOut> {
  return invoke<PairCodeOut>("pair_create");
}

/** Accept a friend's pair code → add them to the federation allowlist. */
export function pairAccept(code: string): Promise<string> {
  return invoke<string>("pair_accept", { code });
}

export function pairList(): Promise<Pairing[]> {
  return invoke<Pairing[]>("pair_list");
}

export function pairRemove(onion: string): Promise<null> {
  return invoke<null>("pair_remove", { onion });
}

/* ── status store, polled every 1.5 s ─────────────────────────── */

export const status: Writable<Status | null> = writable(null);

/**
 * Liveness of the status poll itself — separate from the box's phase.
 *
 * get_status can fail silently for a beat while the box is mid-restart; that's
 * normal on Tor. But if every poll keeps failing we must NOT keep rendering the
 * last-known (likely green) status as if it were live. After STALE_AFTER_MS of
 * unbroken failure we flip `stale` so the UI can show "lost contact" and dim
 * the now-frozen numbers. `lastOk` is the epoch-ms of the last good poll.
 */
export interface Liveness {
  /** True once we've had no successful poll for STALE_AFTER_MS. */
  stale: boolean;
  /** Consecutive failed polls since the last success. */
  failures: number;
  /** Epoch ms of the last successful poll, or null before the first. */
  lastOk: number | null;
}

/** Go stale after ~5 s of unbroken poll failure (≈3 missed 1.5 s ticks). */
export const STALE_AFTER_MS = 5000;

export const liveness: Writable<Liveness> = writable({
  stale: false,
  failures: 0,
  lastOk: null,
});

let pollTimer: ReturnType<typeof setInterval> | null = null;

export function startStatusPolling(intervalMs = 1500): void {
  if (pollTimer !== null) return;
  const tick = async () => {
    if (!hasTauri()) return; // plain-browser context: nothing to poll
    try {
      const s = await getStatus();
      status.set(s);
      // A good poll clears any staleness immediately.
      liveness.set({ stale: false, failures: 0, lastOk: Date.now() });
    } catch (e) {
      // Box may be mid-restart — keep the last known status, but COUNT the
      // failure and flag staleness once we've been dark too long.
      console.error("get_status poll failed:", e);
      liveness.update((l) => {
        const failures = l.failures + 1;
        const since = l.lastOk;
        // Never had a good poll yet, or it's been too long → stale.
        const stale =
          since === null ? failures >= 3 : Date.now() - since >= STALE_AFTER_MS;
        return { stale, failures, lastOk: since };
      });
    }
  };
  void tick();
  pollTimer = setInterval(tick, intervalMs);
}

export function stopStatusPolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

/** Force one immediate poll — wired to the dashboard's Reload button. */
export async function refreshStatus(): Promise<void> {
  if (!hasTauri()) return;
  try {
    const s = await getStatus();
    status.set(s);
    liveness.set({ stale: false, failures: 0, lastOk: Date.now() });
  } catch (e) {
    console.error("manual refreshStatus failed:", e);
    liveness.update((l) => ({ ...l, failures: l.failures + 1 }));
  }
}

/* ── small shared helper ──────────────────────────────────────── */

/** Copy text to the clipboard; returns false if the platform refused. */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch {
      return false;
    }
  }
}
