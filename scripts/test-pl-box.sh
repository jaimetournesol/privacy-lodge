#!/usr/bin/env bash
# test-pl-box.sh — smoke tests for the pl-box CLI's backup/restore/destroy paths.
#
# Why this exists: pl-box is ~600 lines of bash guarding the box's identity, and it has
# already shipped one silent data-loss class bug (backup covering only the box volume) and
# one silent misclassification bug (`tar tzf | grep -q` under pipefail reading every large
# bundle as "no match" via SIGPIPE). Both would have been caught by exactly the round-trips
# below. Runs against THROWAWAY volumes and a scratch install dir — it never touches a real
# box's volumes, and it refuses to start if a privacy-lodge-box container is running.
#
#   ./scripts/test-pl-box.sh          # run all tests (needs docker; ~30s)
#
set -uo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
PPBOX="$HERE/docker/pl-box"
PREFIX="ppboxtest-$$"
WORK="$(mktemp -d)"
PASS=0
FAIL=0

say()  { printf '\033[34m==>\033[0m %s\n' "$*"; }
good() { printf '\033[32m ok \033[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '\033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }

cleanup() {
  # update-test leftovers: the stand-in compose project, the throwaway registry, its images
  docker rm -f "$PREFIX-upd-box" "$PREFIX-upd-agent" "$PREFIX-registry" >/dev/null 2>&1
  docker network rm "$PREFIX-upd_default" >/dev/null 2>&1
  docker images --format '{{.Repository}}:{{.Tag}}' | grep "^127.0.0.1:${RPORT:-none}/" | xargs -r docker rmi -f >/dev/null 2>&1
  docker ps -aq --filter "ancestor=$PREFIX-img" | xargs -r docker rm -f >/dev/null 2>&1
  docker rmi -f "$PREFIX-img" >/dev/null 2>&1
  docker volume ls --format '{{.Name}}' | grep "^$PREFIX" | xargs -r docker volume rm -f >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT

command -v docker >/dev/null || { echo "docker required"; exit 2; }
# Never run while a real box is up: restore paths refuse when the container exists, which
# would make every restore test fail confusingly — and pointing at a live install by
# accident must be impossible, so everything below uses $PREFIX-* volumes only.
if docker ps --format '{{.Names}}' | grep -qx privacy-lodge-box; then
  echo "a privacy-lodge-box container is running — stop it first (./pl-box down)"; exit 2
fi

# ---- a fake install: scratch dir with .env + pl-box, volumes seeded with marker files ----
BOXV="$PREFIX-box"; AGENTV="$PREFIX-agent"; HANDV="$PREFIX-handoff"
INSTALL="$WORK/install"; mkdir -p "$INSTALL"
cp "$PPBOX" "$INSTALL/pl-box"; chmod +x "$INSTALL/pl-box"
cat > "$INSTALL/.env" <<EOF
PL_USER=tester
PL_BOX=testbox
PL_PASS=irrelevant
PL_SECRETS_KEY=THE-REAL-KEY
PL_VOLUME=$BOXV
PL_AGENT_VOLUME=$AGENTV
PL_AGENT_HANDOFF_VOLUME=$HANDV
EOF

seed() { # $1=volume $2=marker-relpath $3=content
  docker volume create "$1" >/dev/null
  docker run --rm -v "$1":/v -e P="$2" -e C="$3" alpine \
    sh -c 'mkdir -p "/v/$(dirname "$P")"; printf "%s" "$C" > "/v/$P"'
}
say "seeding throwaway volumes"
seed "$BOXV" "box.json" '{"box_name":"testbox","username":"tester","onion":"aaaabbbbccccddddeeeeffffgggghhhhiiiijjjjkkkkllllmmmmnnnn.onion"}'
seed "$BOXV" "data/tor/hs/hs_ed25519_secret_key" "FAKE-ONION-KEY"
seed "$AGENTV" "hermes/auth.json" '{"providers":[{"provider":"fake"}]}'
seed "$HANDV" "webui-password" "hunter2"

vol_file() { docker run --rm -v "$1":/v:ro alpine cat "/v/$2" 2>/dev/null; }

# ------------------------------------------------- rename (0.2.0): PP_* -> PL_* compat ----
# A pre-rename install's .env says PP_*. pl-box must still READ it (status, qr, backup keep
# working), must REFUSE the compose-backed commands (compose can't see PP_*, so `up` would
# create a new empty volume = a new box), and `migrate-env` must fix it in place, once.
say "pre-rename PP_* .env: read by pl-box, refused by compose commands, fixed by migrate-env"
INSTALLC="$WORK/installc"; mkdir -p "$INSTALLC"
cp "$PPBOX" "$INSTALLC/pl-box"; chmod +x "$INSTALLC/pl-box"
sed -e 's/^PL_/PP_/' "$INSTALL/.env" > "$INSTALLC/.env"
( cd "$INSTALLC" && ./pl-box backup "$WORK/compat" ) >/dev/null 2>&1
CB="$(ls "$WORK/compat"/pl-box-*.tgz 2>/dev/null | head -1)"
if [ -n "$CB" ] && tar xzf "$CB" -O ./MANIFEST 2>/dev/null | grep -q '^secrets_key=THE-REAL-KEY$'; then
  good "PP_* keys are read (backup carries the secrets key)"
else
  bad "PP_* .env not read: no bundle / key missing"
fi
out="$( cd "$INSTALLC" && ./pl-box up 2>&1 )"; rc=$?
if [ "$rc" != 0 ] && echo "$out" | grep -q 'migrate-env'; then
  good "compose commands refuse an un-migrated .env and point at migrate-env"
else
  bad "up did not refuse an un-migrated .env (rc=$rc)"
fi
( cd "$INSTALLC" && ./pl-box migrate-env ) >/dev/null 2>&1
if grep -q '^PL_VOLUME=' "$INSTALLC/.env" && ! grep -q '^PP_' "$INSTALLC/.env"; then
  good "migrate-env renamed every key"
else
  bad "migrate-env left $(grep -c '^PP_' "$INSTALLC/.env") PP_* key(s)"
fi
ls "$INSTALLC"/.env.bak-* >/dev/null 2>&1 && good "migrate-env kept a backup" || bad "no .env backup kept"
grep -q '^PL_SECRETS_KEY=THE-REAL-KEY$' "$INSTALLC/.env" && good "values untouched by migrate-env" || bad "a value changed"
( cd "$INSTALLC" && ./pl-box migrate-env ) 2>&1 | grep -q 'already' \
  && good "migrate-env is idempotent" || bad "second migrate-env did not no-op"
printf 'PL_IMAGE=jaimemelon/pureprivacy-box:0.1.10\n' >> "$INSTALLC/.env"
out="$( cd "$INSTALLC" && ./pl-box up 2>&1 )"; rc=$?
if [ "$rc" != 0 ] && echo "$out" | grep -q 'update 0.2.0'; then
  good "a pre-rename image is refused and pointed at update 0.2.0"
else
  bad "pre-rename image not refused (rc=$rc)"
fi

# ------------------------------------------------------ update: a Docker-Hub install ----
# `pl-box update <ver>` on an install that runs a REGISTRY image must pull that tag, pin it
# in .env (box — and agent only while the add-on is on), and recreate; it must never call
# build.sh, and a failed pull must leave .env and the containers exactly as they were.
# Before this it always ran build.sh (which needs a Tauri build no user machine has), and
# the `docker pull` the box told owners to run first never reached compose anyway — it kept
# the tag .env named. A throwaway local registry stands in for Docker Hub, alpine for the
# images, and a stand-in compose file carries the same env contract as the real one.
say "update on a Docker-Hub install pulls + pins the version, never builds, fails closed"
RPORT=""
docker run -d --name "$PREFIX-registry" -p 127.0.0.1:0:5000 registry:2 >/dev/null 2>&1
RPORT="$(docker port "$PREFIX-registry" 5000/tcp 2>/dev/null | head -1 | sed 's/.*://')"
if [ -n "$RPORT" ]; then
  for i in $(seq 1 30); do
    if command -v curl >/dev/null 2>&1; then curl -sf "http://127.0.0.1:$RPORT/v2/" >/dev/null 2>&1 && break
    else sleep 3; break; fi
    sleep 1
  done
  BOXREPO="127.0.0.1:$RPORT/pl-box"; AGREPO="127.0.0.1:$RPORT/pp-agent"
  for t in 0.1.10 0.1.11; do
    docker tag alpine "$BOXREPO:$t" && docker push -q "$BOXREPO:$t" >/dev/null 2>&1
    docker tag alpine "$AGREPO:$t"  && docker push -q "$AGREPO:$t"  >/dev/null 2>&1
  done
  # Drop the local copies of the NEW tag: an update has to genuinely pull it.
  docker rmi "$BOXREPO:0.1.11" "$AGREPO:0.1.11" >/dev/null 2>&1
  INSTALLU="$WORK/installu"; mkdir -p "$INSTALLU"
  cp "$PPBOX" "$INSTALLU/pl-box"; chmod +x "$INSTALLU/pl-box"
  # A build.sh that only leaves a fingerprint — the Hub path must never reach it.
  printf '#!/bin/sh\ntouch "$(dirname "$0")/BUILD-SH-WAS-CALLED"\n' > "$INSTALLU/build.sh"
  chmod +x "$INSTALLU/build.sh"
  cat > "$INSTALLU/docker-compose.yml" <<EOF
name: $PREFIX-upd
services:
  box:
    image: \${PL_IMAGE:-privacy-lodge-box:dev}
    container_name: $PREFIX-upd-box
    command: ["sleep", "300"]
  agent:
    image: \${PL_AGENT_IMAGE:-jaimemelon/privacy-lodge-agent:latest}
    container_name: $PREFIX-upd-agent
    profiles: ["agents"]
    command: ["sleep", "300"]
EOF
  cat > "$INSTALLU/.env" <<EOF
PL_USER=tester
PL_BOX=testbox
PL_PASS=irrelevant
PL_SECRETS_KEY=k
PL_VOLUME=$PREFIX-upd-vol
PL_AGENTS=1
PL_IMAGE=$BOXREPO:0.1.10
PL_AGENT_IMAGE=$AGREPO:0.1.10
EOF
  ( cd "$INSTALLU" && ./pl-box update 0.1.11 ) >/dev/null 2>&1; rc=$?
  [ "$rc" = 0 ] && good "update 0.1.11 exits 0 on a Hub install" || bad "update 0.1.11 failed (rc=$rc)"
  [ ! -e "$INSTALLU/BUILD-SH-WAS-CALLED" ] \
    && good "build.sh never invoked on a Hub install" || bad "build.sh was invoked on a Hub install"
  grep -q "^PL_IMAGE=$BOXREPO:0.1.11\$" "$INSTALLU/.env" \
    && good ".env pins PL_IMAGE to 0.1.11" || bad ".env has $(grep ^PL_IMAGE= "$INSTALLU/.env")"
  grep -q "^PL_AGENT_IMAGE=$AGREPO:0.1.11\$" "$INSTALLU/.env" \
    && good ".env pins PL_AGENT_IMAGE to 0.1.11 (add-on on)" || bad ".env has $(grep ^PL_AGENT_IMAGE= "$INSTALLU/.env")"
  bimg="$(docker inspect "$PREFIX-upd-box" --format '{{.Config.Image}}' 2>/dev/null)"
  aimg="$(docker inspect "$PREFIX-upd-agent" --format '{{.Config.Image}}' 2>/dev/null)"
  [ "$bimg" = "$BOXREPO:0.1.11" ] && good "box container recreated on 0.1.11" || bad "box container runs '$bimg'"
  [ "$aimg" = "$AGREPO:0.1.11" ] && good "agent container recreated on 0.1.11" || bad "agent container runs '$aimg'"

  # A tag that doesn't exist: fail, and change nothing.
  ( cd "$INSTALLU" && ./pl-box update 9.9.9 ) >/dev/null 2>&1; rc=$?
  [ "$rc" != 0 ] && good "update to a missing tag fails" || bad "update 9.9.9 exited 0"
  grep -q "^PL_IMAGE=$BOXREPO:0.1.11\$" "$INSTALLU/.env" \
    && good ".env untouched after the failed pull" || bad ".env changed after a failed pull"
  [ "$(docker inspect "$PREFIX-upd-box" --format '{{.Config.Image}}' 2>/dev/null)" = "$BOXREPO:0.1.11" ] \
    && good "container untouched after the failed pull" || bad "container changed after a failed pull"

  # A plain box (add-on off) must not touch the agent image at all.
  sed -i 's/^PL_AGENTS=.*/PL_AGENTS=0/' "$INSTALLU/.env"
  ( cd "$INSTALLU" && ./pl-box update 0.1.10 ) >/dev/null 2>&1
  grep -q "^PL_IMAGE=$BOXREPO:0.1.10\$" "$INSTALLU/.env" && grep -q "^PL_AGENT_IMAGE=$AGREPO:0.1.11\$" "$INSTALLU/.env" \
    && good "add-on off: box repinned, agent image left alone" || bad "add-on off: $(grep -E '^PL_(AGENT_)?IMAGE=' "$INSTALLU/.env" | tr '\n' ' ')"

  # A source install (local image name, no registry namespace) still goes through build.sh.
  sed -i 's|^PL_IMAGE=.*|PL_IMAGE=pl-box:dev|' "$INSTALLU/.env"
  ( cd "$INSTALLU" && ./pl-box update ) >/dev/null 2>&1
  [ -e "$INSTALLU/BUILD-SH-WAS-CALLED" ] \
    && good "source install still rebuilds via build.sh" || bad "source install skipped build.sh"
  ( cd "$INSTALLU" && docker compose --profile agents down --remove-orphans ) >/dev/null 2>&1
else
  bad "could not start a throwaway registry (registry:2) — update path untested"
fi

# --------------------------------------------------------------------- backup: bundle ----
say "backup produces a format-2 bundle covering all three volumes"
( cd "$INSTALL" && ./pl-box backup "$WORK/backups" ) >/dev/null 2>&1
BUNDLE="$(ls "$WORK/backups"/pl-box-*.tgz 2>/dev/null | head -1)"
if [ -n "$BUNDLE" ]; then good "backup wrote $(basename "$BUNDLE")"; else bad "no bundle written"; fi

MEMBERS="$(tar tzf "$BUNDLE" 2>/dev/null)"
missing=""
for m in MANIFEST box.tgz agent-data.tgz agent-handoff.tgz; do
  echo "$MEMBERS" | grep -qx "./$m" || missing="$missing $m"
done
if [ -z "$missing" ]; then
  good "bundle holds MANIFEST + box + agent-data + agent-handoff"
else
  bad "bundle is missing:$missing (has: $(echo "$MEMBERS" | tr '\n' ' '))"
fi

MAN="$(tar xzf "$BUNDLE" -O ./MANIFEST 2>/dev/null)"
echo "$MAN" | grep -q '^secrets_key=THE-REAL-KEY$' \
  && good "MANIFEST carries the secrets key" \
  || bad "MANIFEST is missing the secrets key (an undecryptable backup)"
echo "$MAN" | grep -q '^onion=aaaabbbbccccddddeeeeffffgggghhhhiiiijjjjkkkkllllmmmmnnnn.onion$' \
  && good "MANIFEST records the onion (box was down — read from the volume, not the container)" \
  || bad "MANIFEST onion wrong/missing: $(echo "$MAN" | grep ^onion= || echo none)"

perm="$(stat -c %a "$BUNDLE" 2>/dev/null || stat -f %Lp "$BUNDLE")"
[ "$perm" = "600" ] && good "bundle is 0600" || bad "bundle perms are $perm, wanted 600"

# ------------------------------------------------- restore: bundle into fresh volumes ----
say "restore round-trips the bundle into a second set of volumes"
INSTALL2="$WORK/install2"; mkdir -p "$INSTALL2"
cp "$PPBOX" "$INSTALL2/pl-box"; chmod +x "$INSTALL2/pl-box"
# Same volume names would collide with the seeded set; a restore must land in ITS install's
# volumes. Wrong secrets key on purpose: the repair prompt is part of the contract.
sed -e "s/$BOXV/$PREFIX-box2/" -e "s/$AGENTV/$PREFIX-agent2/" -e "s/$HANDV/$PREFIX-handoff2/" \
    -e 's/THE-REAL-KEY/WRONG-KEY/' "$INSTALL/.env" > "$INSTALL2/.env"
( cd "$INSTALL2" && printf 'y\ny\n' | ./pl-box restore "$BUNDLE" ) >/dev/null 2>&1

[ "$(vol_file "$PREFIX-box2" data/tor/hs/hs_ed25519_secret_key)" = "FAKE-ONION-KEY" ] \
  && good "box volume restored (onion key byte-identical)" || bad "box volume content wrong after restore"
[ "$(vol_file "$PREFIX-agent2" hermes/auth.json)" = '{"providers":[{"provider":"fake"}]}' ] \
  && good "agent volume restored" || bad "agent volume content wrong after restore"
[ "$(vol_file "$PREFIX-handoff2" webui-password)" = "hunter2" ] \
  && good "handoff volume restored" || bad "handoff volume content wrong after restore"
grep -q '^PL_SECRETS_KEY=THE-REAL-KEY$' "$INSTALL2/.env" \
  && good "mismatched PL_SECRETS_KEY repaired in .env (with consent)" \
  || bad ".env key not repaired: $(grep ^PL_SECRETS_KEY= "$INSTALL2/.env")"
ls "$INSTALL2"/.env.bak-* >/dev/null 2>&1 \
  && good "previous .env kept alongside" || bad "no .env backup was kept"

# ---------------------------------------------- restore: legacy single-volume backups ----
say "legacy (pre-bundle) backups still restore"
docker run --rm -v "$BOXV":/v:ro -v "$WORK":/out alpine tar czf /out/legacy.tgz -C /v . >/dev/null
INSTALL3="$WORK/install3"; mkdir -p "$INSTALL3"
cp "$PPBOX" "$INSTALL3/pl-box"; chmod +x "$INSTALL3/pl-box"
sed "s/$BOXV/$PREFIX-box3/" "$INSTALL/.env" > "$INSTALL3/.env"
( cd "$INSTALL3" && printf 'y\n' | ./pl-box restore "$WORK/legacy.tgz" ) >/dev/null 2>&1
[ "$(vol_file "$PREFIX-box3" data/tor/hs/hs_ed25519_secret_key)" = "FAKE-ONION-KEY" ] \
  && good "legacy tar restored as a plain volume (not treated as a bundle)" \
  || bad "legacy restore broken"

# The reverse misclassification is the SIGPIPE bug: a large bundle read as legacy fills
# the volume with tarballs. The bundle restore above already proves bundles classify as
# bundles — this asserts the extracted volume holds real content, not member tarballs.
if docker run --rm -v "$PREFIX-box2":/v:ro alpine sh -c 'ls /v/box.tgz' >/dev/null 2>&1; then
  bad "bundle was extracted AS a legacy tar (member tarballs in the volume)"
else
  good "no member tarballs in restored volume (bundle/legacy sniff is sound)"
fi

# ------------------------------------------------------------- backup: a PLAIN box ----
# No agents add-on and no handoff volume — the README's bare `docker run` install, or any
# box that never ran under compose. This used to exit 1 with NO bundle and NO message: the
# manifest string's last $(...) was `[ no = yes ] && echo`, its status became the
# assignment's status, and `set -e` killed the script. The one command that "IS the box",
# silently doing nothing.
say "a plain box (box volume only, no agent/handoff volumes) still backs up"
seed "$PREFIX-plain" "box.json" '{"box_name":"plain","username":"tester","onion":"aaaabbbbccccddddeeeeffffgggghhhhiiiijjjjkkkkllllmmmmnnnn.onion"}'
seed "$PREFIX-plain" "data/tor/hs/hs_ed25519_secret_key" "PLAIN-ONION-KEY"
INSTALLP="$WORK/installp"; mkdir -p "$INSTALLP"
cp "$PPBOX" "$INSTALLP/pl-box"; chmod +x "$INSTALLP/pl-box"
sed -e "s/$BOXV/$PREFIX-plain/" -e "s/$AGENTV/$PREFIX-no-such-agent/" -e "s/$HANDV/$PREFIX-no-such-handoff/" \
    "$INSTALL/.env" > "$INSTALLP/.env"
( cd "$INSTALLP" && ./pl-box backup "$WORK/plain" ) >/dev/null 2>&1; rc=$?
PLAINB="$(ls "$WORK/plain"/pl-box-*.tgz 2>/dev/null | head -1)"
if [ "$rc" = 0 ] && [ -n "$PLAINB" ]; then
  good "plain-box backup exits 0 and writes a bundle"
else
  bad "plain-box backup failed (rc=$rc, bundle: ${PLAINB:-none}) — silent set -e death in the manifest?"
fi
if [ -n "$PLAINB" ]; then
  tar tzf "$PLAINB" 2>/dev/null | grep -qx ./agent-data.tgz \
    && bad "plain bundle carries an agent-data member it can't have" \
    || good "plain bundle holds MANIFEST + box only"
  tar xzf "$PLAINB" -O ./MANIFEST 2>/dev/null | grep -q '^handoff_volume=$' \
    && good "MANIFEST records no handoff volume (restore won't look for one)" \
    || bad "MANIFEST handoff_volume should be empty for a plain box"
fi

# -------------------------------------------------------------- encrypted backups ----
# pl-box resolves pl-crypt relative to ITS OWN location (the scratch install), so point it
# at the repo's build explicitly; build it if missing (CI runs this before tauri build).
if [ -z "${PL_CRYPT:-}" ]; then
  for c in "$HERE/src-tauri/target/release/pl-crypt" "$HERE/src-tauri/target/debug/pl-crypt"; do
    [ -x "$c" ] && { export PL_CRYPT="$c"; break; }
  done
fi
if [ -z "${PL_CRYPT:-}" ]; then
  say "building pl-crypt (not found in target/)"
  (cd "$HERE/src-tauri" && cargo build --locked --bin pl-crypt >/dev/null 2>&1) \
    && export PL_CRYPT="$HERE/src-tauri/target/debug/pl-crypt"
fi

if [ -n "${PL_CRYPT:-}" ]; then
  say "encrypted backup seals, restores, and fails closed"
  # Re-seed (the restore tests consumed nothing, but destroy below wants the originals).
  ( cd "$INSTALL" && PL_BACKUP_PASSPHRASE=correct-horse ./pl-box backup "$WORK/enc" --encrypt ) >/dev/null 2>&1
  ENC="$(ls "$WORK/enc"/pl-box-*.tgz.enc 2>/dev/null | head -1)"
  if [ -n "$ENC" ]; then good "encrypted backup wrote $(basename "$ENC")"; else bad "no .enc written"; fi
  [ -z "$(ls "$WORK/enc"/pl-box-*.tgz 2>/dev/null)" ] \
    && good "plaintext bundle removed after sealing" || bad "plaintext bundle left next to the .enc"
  head -c 512 "$ENC" | head -1 | grep -q '"ppcrypt":1' \
    && good ".enc carries the pl-crypt header" || bad ".enc header missing/wrong"

  INSTALL5="$WORK/install5"; mkdir -p "$INSTALL5"
  cp "$PPBOX" "$INSTALL5/pl-box"; chmod +x "$INSTALL5/pl-box"
  sed -e "s/$BOXV/$PREFIX-box5/" -e "s/$AGENTV/$PREFIX-agent5/" -e "s/$HANDV/$PREFIX-handoff5/" \
      "$INSTALL/.env" > "$INSTALL5/.env"
  ( cd "$INSTALL5" && PL_BACKUP_PASSPHRASE=correct-horse printf 'y\n' | \
      PL_BACKUP_PASSPHRASE=correct-horse ./pl-box restore "$ENC" ) >/dev/null 2>&1
  [ "$(vol_file "$PREFIX-box5" data/tor/hs/hs_ed25519_secret_key)" = "FAKE-ONION-KEY" ] \
    && good "encrypted bundle restored (onion key byte-identical)" \
    || bad "encrypted restore content wrong"

  INSTALL6="$WORK/install6"; mkdir -p "$INSTALL6"
  cp "$PPBOX" "$INSTALL6/pl-box"; chmod +x "$INSTALL6/pl-box"
  sed "s/$BOXV/$PREFIX-box6/" "$INSTALL/.env" > "$INSTALL6/.env"
  ( cd "$INSTALL6" && PL_BACKUP_PASSPHRASE=wrong-horse1 printf 'y\n' | \
      PL_BACKUP_PASSPHRASE=wrong-horse1 ./pl-box restore "$ENC" ) >/dev/null 2>&1
  docker volume inspect "$PREFIX-box6" >/dev/null 2>&1 \
    && bad "wrong passphrase still created/filled a volume" \
    || good "wrong passphrase fails closed — nothing restored"

  # ------------------------------------------ encrypted backups: the DOCKER path ----
  # An INSTALLED pl-box has no repo next to it: pp_crypt() falls through to running
  # pl-crypt out of the box image named by PL_IMAGE in .env. Everything above pins
  # PL_CRYPT to a host binary, so this path was untested — and it shipped broken twice
  # over: the image's ENTRYPOINT swallowed the arguments and booted a whole box (hang,
  # orphan container, the web-setup banner written to .enc), and IMAGE ignored PL_IMAGE.
  # A stand-in image reproduces both hazards faithfully: the real pl-crypt at the real
  # path, behind a decoy ENTRYPOINT that hangs (as the box does) unless it is bypassed.
  say "encrypted backup + restore via the box image (installed layout, PL_IMAGE from .env)"
  TIMG="$PREFIX-img"; IMGCTX="$WORK/imgctx"; mkdir -p "$IMGCTX"
  cp "$PL_CRYPT" "$IMGCTX/pl-crypt"
  printf 'FROM ubuntu:26.04\nCOPY pl-crypt /opt/privacy-lodge/bin/pl-crypt\nENTRYPOINT ["/bin/sh","-c","echo BOOTING-A-WHOLE-BOX; sleep 120"]\n' > "$IMGCTX/Dockerfile"
  if docker build -q -t "$TIMG" "$IMGCTX" >/dev/null 2>&1; then
    # Bound the run: without --entrypoint pl-box would sit inside the decoy forever.
    T=""; command -v timeout >/dev/null 2>&1 && T="timeout 90"
    INSTALL7="$WORK/install7"; mkdir -p "$INSTALL7"
    cp "$PPBOX" "$INSTALL7/pl-box"; chmod +x "$INSTALL7/pl-box"
    { cat "$INSTALL/.env"; printf 'PL_IMAGE=%s\n' "$TIMG"; } > "$INSTALL7/.env"
    # env -u: pl-box must find pl-crypt on its own — no override, no repo at ../src-tauri.
    ( cd "$INSTALL7" && env -u PL_CRYPT -u IMAGE PL_BACKUP_PASSPHRASE=correct-horse \
        $T ./pl-box backup "$WORK/enc7" --encrypt ) >/dev/null 2>&1; rc=$?
    ENC7="$(ls "$WORK/enc7"/pl-box-*.tgz.enc 2>/dev/null | head -1)"
    if [ "$rc" = 0 ] && [ -n "$ENC7" ]; then
      good "sealed by the image's pl-crypt (ENTRYPOINT bypassed, PL_IMAGE honoured)"
    else
      bad "docker-path backup failed (rc=$rc; 124 = hung in the image's ENTRYPOINT)"
    fi
    [ -z "$(docker ps -aq --filter "ancestor=$TIMG" 2>/dev/null)" ] \
      && good "no orphan container left behind" \
      || bad "orphan container(s) from $TIMG left behind"
    [ -z "$(ls "$WORK/enc7"/pl-box-*.tgz 2>/dev/null)" ] \
      && good "plaintext removed after sealing (docker path)" \
      || bad "plaintext bundle left next to the .enc (docker path)"
    if [ -n "$ENC7" ]; then
      INSTALL8="$WORK/install8"; mkdir -p "$INSTALL8"
      cp "$PPBOX" "$INSTALL8/pl-box"; chmod +x "$INSTALL8/pl-box"
      { sed -e "s/$BOXV/$PREFIX-box8/" -e "s/$AGENTV/$PREFIX-agent8/" -e "s/$HANDV/$PREFIX-handoff8/" \
            "$INSTALL/.env"; printf 'PL_IMAGE=%s\n' "$TIMG"; } > "$INSTALL8/.env"
      ( cd "$INSTALL8" && printf 'y\ny\n' | env -u PL_CRYPT -u IMAGE PL_BACKUP_PASSPHRASE=correct-horse \
          $T ./pl-box restore "$ENC7" ) >/dev/null 2>&1
      [ "$(vol_file "$PREFIX-box8" data/tor/hs/hs_ed25519_secret_key)" = "FAKE-ONION-KEY" ] \
        && good "opened by the image's pl-crypt and restored (onion key byte-identical)" \
        || bad "docker-path restore content wrong"
    fi
  else
    bad "could not build the stand-in image (docker build FROM ubuntu:26.04) — docker path untested"
  fi
else
  bad "pl-crypt not available and could not be built — encrypted-backup path untested"
fi

# ------------------------------------------------------------------------- destroy ----
say "destroy removes ALL three volumes, not just the box"
( cd "$INSTALL" && printf 'testbox\n' | ./pl-box destroy ) >/dev/null 2>&1
left="$(docker volume ls --format '{{.Name}}' | grep -cE "^($BOXV|$AGENTV|$HANDV)$")"
[ "$left" = "0" ] && good "box, agent and handoff volumes all gone" \
  || bad "$left volume(s) survived destroy"

# destroy must be gated on the exact box name — a wrong name removes nothing.
seed "$PREFIX-box4" "box.json" '{}'
INSTALL4="$WORK/install4"; mkdir -p "$INSTALL4"
cp "$PPBOX" "$INSTALL4/pl-box"; chmod +x "$INSTALL4/pl-box"
sed "s/$BOXV/$PREFIX-box4/" "$INSTALL/.env" > "$INSTALL4/.env"
( cd "$INSTALL4" && printf 'WRONG-NAME\n' | ./pl-box destroy ) >/dev/null 2>&1
docker volume inspect "$PREFIX-box4" >/dev/null 2>&1 \
  && good "wrong confirmation name removes nothing" \
  || bad "destroy ran despite a wrong confirmation name"

# ------------------------------------------------------------------------- summary ----
echo
if [ "$FAIL" = 0 ]; then
  printf '\033[32m%d passed\033[0m, 0 failed\n' "$PASS"
else
  printf '%d passed, \033[31m%d FAILED\033[0m\n' "$PASS" "$FAIL"
  exit 1
fi
