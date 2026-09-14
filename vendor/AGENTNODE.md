# Agentnode runtime snapshot

This directory contains only the reviewed runtime subset required by Privacy Lodge.
The upstream Agentnode repository remains private. Its repository metadata,
Android app, tests, development tools, production deployment scripts, speech worker,
and local state are not distributed here.

Source revision: `79fa5900856f8d180996e0e164642355cdbaa8af`.

`scripts/agentnode-runtime-files.json` is the exact allowlist and SHA-256 inventory.
Normal builds read this snapshot and verify every file against that inventory.
They never search for a sibling checkout. Only an explicit
`PRIVACY_LODGE_AGENTNODE_SOURCE` development override uses another source directory;
it still copies only allowlisted names and rejects symlinks. Review and scan every
snapshot refresh before updating the inventory or publishing a release.

The shared Python server and browser modules retain their runtime dependencies;
Lodge enables only Codex and text interaction. The Surface sources and presentation
extension are needed to build and display agent stages. Bundled third-party browser
library license notices remain alongside their code.

The source owner authorized distribution of this runtime subset with Privacy Lodge.
This does not grant access to, or publish, the remainder of the private repository.
