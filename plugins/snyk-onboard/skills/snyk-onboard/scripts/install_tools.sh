#!/usr/bin/env bash
# Install the GitHub CLI (gh) and/or the Snyk CLI (snyk) without sudo, from the vendors' official
# downloads, into ~/.local/bin (override with INSTALL_DIR). Each download is checked against the
# SHA-256 checksum the vendor publishes; a mismatch aborts without installing anything.
#
#   install_tools.sh gh            # GitHub CLI, latest release from github.com/cli/cli
#   install_tools.sh snyk          # Snyk CLI standalone binary (no Node.js needed)
#   install_tools.sh gh snyk
#   install_tools.sh --dry-run gh snyk   # print what would be downloaded, install nothing
#
# Linux (x86_64, arm64) and macOS. On macOS `brew install gh snyk-cli` works too.
set -euo pipefail

DRY=0
[[ "${1:-}" == "--dry-run" ]] && { DRY=1; shift; }
[[ $# -eq 0 ]] && { sed -n '2,13p' "$0"; exit 2; }

BIN="${INSTALL_DIR:-$HOME/.local/bin}"
os=$(uname -s)
case "$(uname -m)" in
  x86_64|amd64) arch=amd64 ;;
  aarch64|arm64) arch=arm64 ;;
  *) echo "Unsupported CPU: $(uname -m)" >&2; exit 1 ;;
esac
sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | awk '{print $1}'; }
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

fetch() {  # url dest
  echo "  download $1"
  [[ $DRY -eq 1 ]] && return 0
  curl -fsSL --retry 3 -o "$2" "$1"
}

verify() {  # file expected-sha
  [[ $DRY -eq 1 ]] && return 0
  local got; got=$(sha256 "$1")
  if [[ "$got" != "$2" ]]; then
    echo "Checksum mismatch for $(basename "$1"): expected $2, got $got. Nothing installed." >&2
    exit 1
  fi
  echo "  checksum ok"
}

place() {  # src name
  [[ $DRY -eq 1 ]] && { echo "  would install $BIN/$2"; return 0; }
  mkdir -p "$BIN"
  install -m 0755 "$1" "$BIN/$2"
  echo "  installed $BIN/$2: $("$BIN/$2" --version 2>/dev/null | head -1)"
}

install_gh() {
  echo "GitHub CLI:"
  local ver plat ext file base
  ver=$(curl -fsSL https://api.github.com/repos/cli/cli/releases/latest | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"].lstrip("v"))')
  case "$os" in
    Linux) plat="linux_${arch}"; ext=tar.gz ;;
    Darwin) plat="macOS_${arch}"; ext=zip ;;
    *) echo "Unsupported OS: $os" >&2; exit 1 ;;
  esac
  file="gh_${ver}_${plat}.${ext}"
  base="https://github.com/cli/cli/releases/download/v${ver}"
  fetch "$base/$file" "$tmp/$file"
  fetch "$base/gh_${ver}_checksums.txt" "$tmp/checksums.txt"
  if [[ $DRY -eq 0 ]]; then
    verify "$tmp/$file" "$(awk -v f="$file" '$2 == f {print $1}' "$tmp/checksums.txt")"
    if [[ $ext == tar.gz ]]; then tar -xzf "$tmp/$file" -C "$tmp"; else (cd "$tmp" && unzip -q "$file"); fi
  fi
  place "$tmp/gh_${ver}_${plat}/bin/gh" gh
}

install_snyk() {
  echo "Snyk CLI:"
  local name base
  case "$os:$arch" in
    Linux:amd64) name=snyk-linux ;;
    Linux:arm64) name=snyk-linux-arm64 ;;
    Darwin:amd64) name=snyk-macos ;;
    Darwin:arm64) name=snyk-macos-arm64 ;;
    *) echo "Unsupported platform: $os $arch" >&2; exit 1 ;;
  esac
  base="https://downloads.snyk.io/cli/stable"
  fetch "$base/$name" "$tmp/$name"
  fetch "$base/$name.sha256" "$tmp/$name.sha256"
  [[ $DRY -eq 0 ]] && verify "$tmp/$name" "$(awk '{print $1}' "$tmp/$name.sha256")"
  place "$tmp/$name" snyk
}

for tool in "$@"; do
  case "$tool" in
    gh) install_gh ;;
    snyk) install_snyk ;;
    *) echo "Unknown tool '$tool' (use gh and/or snyk)" >&2; exit 2 ;;
  esac
done

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo
     echo "Note: $BIN isn't on your PATH. Add it, then restart Claude Code:"
     echo "  echo 'export PATH=\"$BIN:\$PATH\"' >> ~/.bashrc" ;;
esac
