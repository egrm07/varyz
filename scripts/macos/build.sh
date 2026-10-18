#!/bin/sh
# Run on a Mac; all download/install operations below stay inside the build workspace.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$root"
[ "$(uname -s)" = Darwin ] || { echo "macOS is required." >&2; exit 1; }
arch=$(uname -m)
case "$arch" in
  arm64) python_arch=aarch64 ;;
  x86_64) python_arch=x86_64 ;;
  *) echo "Unsupported Mac architecture." >&2; exit 1 ;;
esac
export UV_CACHE_DIR="$root/.build-cache/macos-uv-cache"
export UV_PYTHON_INSTALL_DIR="$root/.build-cache/macos-python"
mkdir -p "$root/.build-cache/macos-tools" "$root/.build-cache/macos-wheels-$arch"
uv python install --no-bin 3.12.13
runtime="$UV_PYTHON_INSTALL_DIR/cpython-3.12.13-macos-$python_arch-none"
uv export --frozen --no-dev --no-default-groups --no-emit-project \
  --output-file "$root/.build-cache/macos-requirements.txt"
uv build
uv run --no-project --python "$runtime/bin/python3" --with pip python -m pip download \
  --require-hashes --only-binary=:all: --dest "$root/.build-cache/macos-wheels-$arch" \
  -r "$root/.build-cache/macos-requirements.txt"
cp "$(command -v uv)" "$root/.build-cache/macos-tools/uv"
for license in MIT APACHE; do
  curl --fail --location --retry 3 --max-time 120 \
    "https://raw.githubusercontent.com/astral-sh/uv/0.11.2/LICENSE-$license" \
    --output "$root/.build-cache/macos-tools/UV-LICENSE-$license"
done
"$runtime/bin/python3" scripts/macos/build.py --runtime "$runtime" \
  --uv "$root/.build-cache/macos-tools/uv" \
  --wheelhouse "$root/.build-cache/macos-wheels-$arch" \
  --stage "$root/.build-cache/macos-stage-$arch-$(date +%Y%m%d%H%M%S)"
