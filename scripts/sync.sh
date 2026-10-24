#!/usr/bin/env bash
# Re-import Home Assistant's homekit integration at a release tag and re-apply
# the linked-switches patch on top.
#
#   scripts/sync.sh 2026.11.0              re-import that release and re-patch
#   scripts/sync.sh --diff-only 2026.10.2  regenerate the patch from the
#                                          component against that release
#
# Run it after every Home Assistant update: the override shadows the built-in
# integration completely, so a stale copy keeps running old bridge code.
set -euo pipefail

diff_only=false
if [[ "${1:-}" == "--diff-only" ]]; then
    diff_only=true
    shift
fi
version="${1:?usage: scripts/sync.sh [--diff-only] <ha-version>}"

root="$(cd "$(dirname "$0")/.." && pwd)"
component="$root/custom_components/homekit"
patch_file="$root/patches/linked-switches.patch"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$version" \
    --filter=blob:none --sparse https://github.com/home-assistant/core.git "$work/core"
git -C "$work/core" sparse-checkout set homeassistant/components/homekit
upstream="$work/core/homeassistant/components/homekit"

if $diff_only; then
    mkdir -p "$work/a" && cp -R "$upstream" "$work/a/homekit"
    # Diff from the parent so both sides read homekit/<file>.
    (cd "$work/a" && diff -ru --exclude __pycache__ homekit "$component") \
        | sed -e "s|^+++ $component|+++ homekit|" > "$patch_file" || true
    echo "Wrote $patch_file"
    exit 0
fi

rm -rf "$component"
cp -R "$upstream" "$component"
# The patch also adds the manifest "version" custom integrations need. A hunk
# that no longer applies leaves .rej files: port it by hand, then `make patch`.
patch --quiet --directory "$root/custom_components" --strip 0 < "$patch_file"
echo "Synced $component to $version with the linked-switches patch."
echo "Set the manifest version to $version.1, bump requirements-test.txt to the"
echo "harness release for $version, then: make check && make patch"
