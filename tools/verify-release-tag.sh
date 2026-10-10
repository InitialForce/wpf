#!/usr/bin/env bash
# verify-release-tag.sh: check that a release tag may be published.
#
# The tag is data. In order, this script checks that:
#   1. the name matches if-<x.y.z>-perf.<yyyymmdd>[.<rev>], before the name
#      is used anywhere;
#   2. PUBKEY_FILE holds exactly one primary key, and its fingerprint is
#      BOT_FPR (so a swapped key file is refused);
#   3. the tag is an annotated tag with a good signature from that key,
#      checked in an empty keyring that holds only that key;
#   4. the tag's commit is reachable from MAIN_REF.
# On success it prints the tag's commit SHA and exits 0. On failure it prints
# the reason to stderr and exits 1. Bad usage exits 2.
#
# Usage: verify-release-tag.sh TAG
# Environment:
#   BOT_FPR      expected fingerprint of the bot signing key (hex, spaces ok)
#   PUBKEY_FILE  armored public key from a trusted if/main checkout
#   MAIN_REF     commit or ref the tag must be reachable from
#   FETCH        1 = fetch refs/tags/TAG from origin after the name check;
#                otherwise the tag must already exist locally
set -euo pipefail

fail() { echo "$1" >&2; exit 1; }

[ $# -eq 1 ] || { echo "usage: verify-release-tag.sh TAG" >&2; exit 2; }
TAG=$1
: "${PUBKEY_FILE:?}" "${MAIN_REF:?}"

[[ "$TAG" =~ ^if-[0-9]+\.[0-9]+\.[0-9]+-perf\.[0-9]{8}(\.[0-9]+)?$ ]] \
  || fail "Invalid tag format. Expected if-<x.y.z>-perf.<yyyymmdd>[.<rev>]"

FPR=$(printf '%s' "${BOT_FPR:-}" | tr -d ' ' | tr '[:lower:]' '[:upper:]')
[[ "$FPR" =~ ^([0-9A-F]{40}|[0-9A-F]{64})$ ]] \
  || fail "BOT_FPR (the GH_BOT_GPG_FINGERPRINT secret) is missing or not a fingerprint"
[ -s "$PUBKEY_FILE" ] || fail "Public key file $PUBKEY_FILE is missing or empty"

if [ "${FETCH:-0}" = 1 ]; then
  git fetch -q --no-tags origin "+refs/tags/$TAG:refs/tags/$TAG" \
    || fail "Tag $TAG does not exist on origin"
fi
[ "$(git cat-file -t "refs/tags/$TAG" 2>/dev/null)" = tag ] \
  || fail "Tag $TAG is missing or lightweight; release tags must be signed"

GNUPGHOME=$(mktemp -d)
export GNUPGHOME
trap 'gpgconf --homedir "$GNUPGHOME" --kill all 2>/dev/null; rm -rf "$GNUPGHOME"' EXIT
gpg --batch --quiet --import "$PUBKEY_FILE" 2>/dev/null \
  || fail "Could not import $PUBKEY_FILE"
KEYS=$(gpg --batch --with-colons --list-keys \
  | awk -F: '/^pub:/ {p = 1; next} p && /^fpr:/ {print $10; p = 0}')
[ "$KEYS" = "$FPR" ] \
  || fail "$PUBKEY_FILE must hold exactly the bot key $FPR; it holds: ${KEYS:-nothing}"

STATUS=$(git verify-tag --raw "$TAG" 2>&1) \
  || { echo "$STATUS" >&2; fail "Unsigned or invalid tag: $TAG"; }
# VALIDSIG's last field is the primary key fingerprint, also for a subkey
# signature.
awk -v f="$FPR" '$2 == "GOODSIG" {g = 1} $2 == "VALIDSIG" && $NF == f {v = 1}
  END {exit !(g && v)}' <<<"$STATUS" \
  || { echo "$STATUS" >&2; fail "Tag $TAG has no good signature from the bot key $FPR"; }

COMMIT=$(git rev-parse "refs/tags/$TAG^{commit}")
git merge-base --is-ancestor "$COMMIT" "$MAIN_REF" \
  || fail "Tag $TAG ($COMMIT) is not reachable from $MAIN_REF"
echo "$COMMIT"
