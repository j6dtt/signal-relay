#!/usr/bin/env bash
# Renders docs/signal-cli-reference.md to docs/signal-cli-reference.pdf, in a throwaway Debian container
# (pandoc + headless Chromium; the host needs only Docker). Diagrams come from docs/diagrams/*.png:
# re-export them from their .drawio sources first if you changed any.
#   ./docs/render/render.sh
set -euo pipefail
DOCS=$(cd "$(dirname "$0")/.." && pwd)
OUT=$(mktemp -d)
docker run --rm --shm-size=1g -v "$DOCS":/docs:ro -v "$OUT":/out debian:trixie-slim bash -c '
  set -e
  apt-get update -qq >/dev/null
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends chromium pandoc fonts-dejavu-core python3-bs4 >/dev/null 2>&1
  pandoc -f gfm+implicit_figures -t html5 -s --section-divs --metadata title="signal-cli in the Signal Chat Relay" \
         --css /docs/render/style.css --embed-resources --resource-path=/docs -o /out/ref.html /docs/signal-cli-reference.md
  python3 /docs/render/move_figs.py /out/ref.html     # each diagram after its section, on its own landscape page
  chromium --headless --no-sandbox --disable-gpu --no-pdf-header-footer --print-to-pdf=/out/ref.pdf file:///out/ref.html 2>/dev/null
  chown '"$(id -u):$(id -g)"' /out/*'
cp "$OUT/ref.pdf" "$DOCS/signal-cli-reference.pdf"
rm -rf "$OUT"
echo "Wrote $DOCS/signal-cli-reference.pdf"
