#!/usr/bin/env bash
# Build the signed update manifest for a Privacy Lodge box release (feature H).
#
# Boxes fetch update.json + update.json.sig over Tor and verify the detached ed25519
# signature against the public key COMPILED INTO the box binary
# (updater::UPDATE_PUBKEY_HEX). An unsigned or mis-signed manifest is refused, so this
# script is the only way a release becomes installable.
#
#   ./scripts/sign-release.sh <version> <native-binary> [release-notes-file]
#
# e.g.  ./scripts/sign-release.sh 0.1.1 dist/privacy-lodge-0.1.1-linux-x86_64 notes.txt
#
# Writes update.json + update.json.sig into dist/. Attach BOTH to the GitHub release,
# together with the native binary itself (the manifest pins it by sha256).
set -euo pipefail

KEY="${PL_UPDATE_KEY:-$HOME/Tournesol/_special-project/pureprivacy/pp-update-key.pem}"
PUB="${PL_UPDATE_PUB:-$HOME/Tournesol/_special-project/pureprivacy/pp-update-pub.pem}"
REPO="${PL_REPO:-jaimetournesol/privacy-lodge}"
OUT="${PL_OUT:-dist}"

VERSION="${1:-}"
BINARY="${2:-}"
NOTES_FILE="${3:-}"

if [ -z "$VERSION" ] || [ -z "$BINARY" ]; then
  echo "usage: $0 <version> <native-binary> [release-notes-file]" >&2; exit 2
fi
[ -f "$BINARY" ] || { echo "no such binary: $BINARY" >&2; exit 2; }
[ -f "$KEY" ] || { echo "signing key not found: $KEY" >&2
  echo "  (it lives in _special-project and is NEVER in this repo)" >&2; exit 2; }

mkdir -p "$OUT"
ASSET="$(basename "$BINARY")"
SHA="$(sha256sum "$BINARY" | cut -d' ' -f1)"
SIZE="$(stat -c%s "$BINARY")"
URL="https://github.com/$REPO/releases/download/v$VERSION/$ASSET"
# The box keys native builds by "<os>-<arch>" (std::env::consts), e.g. linux-x86_64.
TARGET="${PL_TARGET:-linux-x86_64}"

# Release notes -> a JSON array of short lines (what PP Config shows under "What's new").
if [ -n "$NOTES_FILE" ] && [ -f "$NOTES_FILE" ]; then
  NOTES="$(python3 -c "
import json,sys
lines=[l.strip().lstrip('-*• ').strip() for l in open(sys.argv[1]) if l.strip()]
print(json.dumps(lines[:8]))" "$NOTES_FILE")"
else
  NOTES='[]'
fi

python3 - "$VERSION" "$TARGET" "$URL" "$SHA" "$SIZE" "$NOTES" "$OUT" <<'PY'
import json, sys, datetime
version, target, url, sha, size, notes, out = sys.argv[1:8]
m = {
    "version": version,
    "released": datetime.date.today().isoformat(),
    "notes": json.loads(notes),
    # Both images: the agents add-on releases in lockstep with the box, and the manifest is
    # the only SIGNED statement of which agent image belongs to a release — without it a
    # Docker install has no trusted answer to "which agent image matches my box".
    "docker": {
        "image": f"jaimemelon/privacy-lodge-box:{version}",
        "agent_image": f"jaimemelon/privacy-lodge-agent:{version}",
    },
    "native": {target: {"url": url, "sha256": sha, "size": int(size)}},
}
# Compact + stable: the signature covers these EXACT bytes.
with open(f"{out}/update.json", "w") as f:
    json.dump(m, f, indent=2, sort_keys=True)
    f.write("\n")
print(json.dumps(m, indent=2, sort_keys=True))
PY

# Detached ed25519 signature over the exact manifest bytes, base64 for transport.
openssl pkeyutl -sign -inkey "$KEY" -rawin -in "$OUT/update.json" -out "$OUT/update.sig.raw"
base64 -w0 "$OUT/update.sig.raw" > "$OUT/update.json.sig"
rm -f "$OUT/update.sig.raw"

# Never publish something we haven't verified ourselves.
base64 -d "$OUT/update.json.sig" > /tmp/pp-verify.sig
if openssl pkeyutl -verify -pubin -inkey "$PUB" -rawin \
     -in "$OUT/update.json" -sigfile /tmp/pp-verify.sig >/dev/null 2>&1; then
  echo "✅ signature verifies against $PUB"
else
  echo "❌ signature FAILED to verify — do not publish" >&2; rm -f /tmp/pp-verify.sig; exit 1
fi
rm -f /tmp/pp-verify.sig

# --- post-quantum half (feature K) ------------------------------------------------------
# Boxes REQUIRE both signatures, so a release without this one simply won't install. Fail
# loudly here rather than publishing something every box will refuse.
PQKEY="${PL_UPDATE_PQ_KEY:-$HOME/Tournesol/_special-project/pureprivacy/pp-update-pq.key}"
PQPUB="${PL_UPDATE_PQ_PUB:-$HOME/Tournesol/_special-project/pureprivacy/pp-update-pq.pub.hex}"
PPSIGN="${PL_SIGN_BIN:-$(dirname "$0")/../src-tauri/target/release/pl-sign}"
[ -x "$PPSIGN" ] || PPSIGN="$(dirname "$0")/../src-tauri/target/debug/pl-sign"
[ -f "$PQKEY" ] || { echo "post-quantum signing key not found: $PQKEY" >&2; exit 2; }
[ -x "$PPSIGN" ] || { echo "pl-sign not built (cargo build --bin pl-sign)" >&2; exit 2; }
"$PPSIGN" sign "$PQKEY" "$OUT/update.json" > "$OUT/update.json.pqsig"
if "$PPSIGN" verify "$PQPUB" "$OUT/update.json" "$OUT/update.json.pqsig" >/dev/null 2>&1; then
  echo "✅ post-quantum signature verifies (SLH-DSA-SHA2-128s)"
else
  echo "❌ post-quantum signature FAILED to verify — do not publish" >&2; exit 1
fi

echo
echo "Wrote $OUT/update.json + $OUT/update.json.sig + $OUT/update.json.pqsig"
echo "Publish with:"
echo "  gh release create v$VERSION --repo $REPO --title \"Privacy Lodge box $VERSION\" \\"
echo "    \"$BINARY\" \"$OUT/update.json\" \"$OUT/update.json.sig\" \"$OUT/update.json.pqsig\""
