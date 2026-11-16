#!/usr/bin/env bash
# Check the release image CI built, and pack it for shipping:
#
#   scripts/release-image.sh <image tag> <output dir>
#
# The image must already exist (built from a clean checkout of HEAD, and
# through scripts/container-smoke.sh --prebuilt and scripts/rollback-smoke.sh
# first). This checks what it is, then writes:
#
#   blocktail-<sha12>.tar.gz   `docker save` of the image, gzip -n
#   image.json                 id, platform, user, labels, base, layers
#   app-files.txt              git blob hash and path of every file in /srv/app
#   packages.txt               pip freeze inside the image
#   SHA256SUMS                 of the four files above
#
# What it checks: linux/amd64; the OCI revision label is HEAD; the image's
# first layers are exactly the base image the Dockerfile pins by digest; it
# runs as EXPECT_USER (default 100:101: the owner of production's data
# volume); every file under /srv/app is the file HEAD has under app/, and
# nothing else is there.
#
# On the server it is loaded with `docker load` under the tag deploy.sh will
# look for (blocktail:<sha12>), and remote-deploy.sh then uses it instead of
# building: it never rebuilds a tag that exists.
set -euo pipefail

TAG="${1:?usage: release-image.sh <image tag> <output dir>}"
OUT="${2:?usage: release-image.sh <image tag> <output dir>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXPECT_USER="${EXPECT_USER:-100:101}"
SHA="$(git -C "$ROOT" rev-parse HEAD)"
SHORT="$(git -C "$ROOT" rev-parse --short=12 HEAD)"

say() { printf 'release-image: %s\n' "$*"; }
fail() { printf 'release-image: FAILED: %s\n' "$*" >&2; exit 1; }
inspect() { docker image inspect -f "$1" "$TAG"; }

[ "$TAG" = "blocktail:$SHORT" ] || fail "$TAG is not the tag deploy.sh looks for (blocktail:$SHORT)"
docker image inspect "$TAG" >/dev/null 2>&1 || fail "no image $TAG"
[ -z "$(git -C "$ROOT" status --porcelain=v1 --untracked-files=no)" ] \
  || fail "the checkout has changes; the image would not be HEAD"

platform="$(inspect '{{.Os}}/{{.Architecture}}')"
[ "$platform" = "linux/amd64" ] || fail "platform $platform"
revision="$(inspect '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
[ "$revision" = "$SHA" ] || fail "revision label $revision, HEAD is $SHA"

# The base: the digest the Dockerfile pins, and its layers as ours' first.
base="$(sed -n 's/^FROM[[:space:]]\{1,\}\([^[:space:]]*@sha256:[0-9a-f]\{64\}\).*/\1/p' "$ROOT/Dockerfile")"
[ -n "$base" ] || fail "the Dockerfile's FROM is not pinned by digest"
docker pull -q --platform linux/amd64 "$base" >/dev/null
base_layers="$(docker image inspect -f '{{range .RootFS.Layers}}{{.}} {{end}}' "$base")"
our_layers="$(inspect '{{range .RootFS.Layers}}{{.}} {{end}}')"
case "$our_layers" in
  "$base_layers"*) ;;
  *) fail "the image is not built on $base" ;;
esac

user="$(docker run --rm --entrypoint sh "$TAG" -c 'echo "$(id -u):$(id -g)"')"
[ "$user" = "$EXPECT_USER" ] || fail "runs as $user, the data volume belongs to $EXPECT_USER"

mkdir -p "$OUT"
docker run --rm --entrypoint python "$TAG" -c '
import hashlib, pathlib
for f in sorted(pathlib.Path("/srv/app").rglob("*")):
    if f.is_file() and "__pycache__" not in f.parts:
        b = f.read_bytes()
        print(hashlib.sha1(b"blob %d\0" % len(b) + b).hexdigest(), f.relative_to("/srv"))
' > "$OUT/app-files.txt"
git -C "$ROOT" ls-tree -r HEAD app | awk '{print $3, $4}' | sort -k2 > "$OUT/.expected"
sort -k2 "$OUT/app-files.txt" | diff -u "$OUT/.expected" - >&2 \
  || fail "/srv/app is not HEAD's app/ (above: - HEAD, + image)"
rm "$OUT/.expected"
docker run --rm --entrypoint python "$TAG" -m pip freeze --all > "$OUT/packages.txt"

id="$(inspect '{{.Id}}')"
python3 - "$OUT/image.json" <<EOF
import json, sys
json.dump({
    "tag": "$TAG", "id": "$id", "revision": "$SHA", "platform": "$platform",
    "user": "$user", "base": "$base",
    "layers": "$our_layers".split(),
    "labels": json.loads('''$(inspect '{{json .Config.Labels}}')'''),
}, open(sys.argv[1], "w"), indent=1)
EOF

docker save "$TAG" | gzip -n -6 > "$OUT/blocktail-$SHORT.tar.gz"
(cd "$OUT" && sha256sum "blocktail-$SHORT.tar.gz" image.json app-files.txt packages.txt > SHA256SUMS)
say "ok: $TAG ($id), $platform, user $user, on $base, revision $SHA"
say "ok: $(wc -l < "$OUT/app-files.txt") files in /srv/app are HEAD's app/"
say "$(cat "$OUT/SHA256SUMS")"
