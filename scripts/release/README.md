# Release runbook

Privacy Lodge ships Linux x86-64 `.deb` and `.AppImage` installers, plus Docker box
and agent images. Windows and macOS use Docker; native installers for those
platforms have not been accepted.

## Source and validation

Work through a feature branch and PR. Run `pnpm check`, `cargo test --locked` in
`src-tauri`, `node --test scripts/test-stage-agentnode.mjs`, Python restore tests,
and `./scripts/test-pl-box.sh`. CI repeats these checks and builds complete Linux
installers. The restore smoke tests use throwaway volumes only.

Agentnode remains private. Public builds use the 124-file reviewed snapshot under
`vendor/agentnode-runtime`, never an implicit sibling checkout. The exact file list
and SHA-256 inventory are `scripts/agentnode-runtime-files.json`; provenance and
refresh rules are in `vendor/AGENTNODE.md`. Review the snapshot and scan it and all
release changes with Gitleaks before publication. Verify checksum-only findings
against the actual files; never dismiss a credential finding as a blanket exception.
Do not include local state, credentials, repository metadata, tests or deployment
tools from the private repository.

## Build and publish

1. Merge a passing PR, then tag the merged revision. The `v*` workflow builds Linux
   installers into a draft GitHub release. Wait for that workflow to finish.
2. Build the matching Docker images. Box: stage sidecars, build the release binary
   and `pl-crypt`, then run `docker/build.sh`. Agents: run `pnpm stage:agentnode`,
   then build `src-tauri/agentnode-runtime` with its `docker/Dockerfile`. Never use
   the entire private Agentnode checkout as a public image build context.
3. Test the resulting runtime with the isolated Docker lifecycle test and inspect
   installer contents. Push `jaimemelon/privacy-lodge-box:<version>` and
   `jaimemelon/privacy-lodge-agent:<version>`; verify their registry digests.
4. Create the signed update manifest locally using `scripts/sign-release.sh`.
   It requires both Ed25519 and SLH-DSA signing keys, which stay outside Git and
   GitHub Actions. The script verifies both signatures before publication.
5. Replace stale draft assets and release notes, attach the manifest and both
   signatures, and publish only after checking that all assets match the release.

## Upgrading to 0.2.0

This version needs updated sidecars and Agentnode resources. Its signed manifest
must have an empty `native` map: use
`./scripts/sign-release.sh 0.2.0 --installer-only <notes-file>`.
Native users must install the complete `.deb` or use the complete `.AppImage`;
the older executable-only updater cannot deliver those resources. Do not publish
a raw native binary as an in-app update for this release.

Docker users back up first, update their Compose/helper files from this release,
and then run `./pl-box update 0.2.0`. Existing identities and data must be preserved.
The new Agentnode containers use separate state volumes; old agent data is retained,
but legacy agent sessions are not automatically converted into Agentnode sessions.
Tuwunel upgrades its database; never put the old homeserver binary back on an
upgraded database. Restore a compatible pre-upgrade backup if rollback is required.

The app's custom updater uses `update.json`, `update.json.sig` and
`update.json.pqsig`. Tauri/minisign `latest.json` is not the active update mechanism.
