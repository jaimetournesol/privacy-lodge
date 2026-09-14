/**
 * Translate raw backend failures into calm, plain, actionable copy.
 *
 * Every desktop catch block sees a plain string (all Tauri commands return
 * Result<_, String>), but that string is often a raw Rust / reqwest / Tor
 * message that surfaces at the highest-stakes moments. mapError() folds the
 * known failure classes into human sentences and falls back to one gentle,
 * "it's often just slow Tor" message for everything else.
 *
 * Always log the raw error with console.error — only the mapped string should
 * reach the UI.
 */

/** One gentle catch-all — most transient Tor hiccups land here. */
const FALLBACK =
  "Something didn't go through — it's often just slow Tor. Try once more.";

/**
 * Map a caught error (string or Error) to friendly UI copy.
 * Keep the raw value in console.error at the call site.
 */
export function mapError(e: unknown): string {
  const raw = typeof e === "string" ? e : e instanceof Error ? e.message : String(e);
  const s = raw.toLowerCase();

  // A network port the box needs is already taken by another program.
  if (
    s.includes("address already in use") ||
    s.includes("addrinuse") ||
    s.includes("address in use") ||
    s.includes("port") && (s.includes("in use") || s.includes("already"))
  ) {
    return "Another program is using a network port Lodge needs.";
  }

  // The box engine (sidecar binaries) isn't installed / running on this machine.
  if (
    s.includes("demo mode") ||
    s.includes("demo_mode") ||
    s.includes("not installed") ||
    s.includes("no sidecars") ||
    s.includes("sidecar") ||
    s.includes("binaries") ||
    s.includes("binary") ||
    s.includes("engine isn't running") ||
    (s.includes("no such file") && s.includes("os error 2")) ||
    s.includes("executablenotfound")
  ) {
    return "The box engine isn't running on this computer.";
  }

  // An expired or invalid pair code. The backend already speaks plainly for
  // these (pairing.rs), but normalise to one consistent line.
  if (
    s.includes("expired") ||
    (s.includes("pair code") &&
      (s.includes("malformed") ||
        s.includes("incompatible") ||
        s.includes("valid")))
  ) {
    return "That code didn't work — it may have expired (codes last 15 minutes). Ask for a fresh one.";
  }

  // Wrong credentials — Matrix forbids the login/registration.
  if (
    s.includes("m_forbidden") ||
    s.includes("forbidden") ||
    s.includes("invalid password") ||
    s.includes("wrong password") ||
    s.includes("invalid username") ||
    s.includes("m_user_in_use")
  ) {
    return "That didn't match — double-check and try again.";
  }

  // Couldn't reach the box / a peer over Tor (timeouts, refused, DNS, unreachable).
  if (
    s.includes("couldn't reach") ||
    s.includes("could not reach") ||
    s.includes("unreachable") ||
    s.includes("timed out") ||
    s.includes("timeout") ||
    s.includes("connection refused") ||
    s.includes("connection reset") ||
    s.includes("connection closed") ||
    s.includes("connect error") ||
    s.includes("dns error") ||
    s.includes("tor didn't produce an address") ||
    s.includes("502") ||
    s.includes("503") ||
    s.includes("504") ||
    s.includes("socks") ||
    s.includes("network") ||
    s.includes("reqwest") ||
    s.includes("hyper")
  ) {
    return "Couldn't reach the box over Tor — check it's online and try again.";
  }

  return FALLBACK;
}
