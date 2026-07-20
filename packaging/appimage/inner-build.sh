#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME="${1:?cpu or nvidia runtime required}"
OUT_DIR="${2:-$ROOT/dist/appimage}"
PYTHON_VERSION=3.13.14
PYTHON_MINOR=3.13

if [[ "$RUNTIME" != cpu && "$RUNTIME" != nvidia ]]; then
  echo "runtime must be cpu or nvidia" >&2
  exit 2
fi

uv python install "$PYTHON_VERSION"
PYTHON="$(uv python find "$PYTHON_VERSION")"
PYTHON_ROOT="$(cd "$(dirname "$PYTHON")/.." && pwd)"
BUILD_VENV="${BUILD_VENV:-/opt/waystone-build-venv}"
rm -rf "$BUILD_VENV"
uv venv --python "$PYTHON" "$BUILD_VENV"
uv pip sync --python "$BUILD_VENV/bin/python" "$ROOT/requirements/$RUNTIME.lock"
uv pip install --python "$BUILD_VENV/bin/python" --no-deps "$ROOT/poed"

npm ci --prefix "$ROOT/brain"
npm ci --prefix "$ROOT/brain/vendor/ee2/renderer"
npm run make-index-files --prefix "$ROOT/brain"
npm run build --prefix "$ROOT/brain/vendor/ee2/renderer"
npm run build --prefix "$ROOT/brain"

MODEL_ROOT="${MODEL_ROOT:-/tmp/waystone-models}"
WAYSTONE_OCR_MODEL_ROOT="$MODEL_ROOT" "$ROOT/scripts/fetch-ocr-models"

BUILD_DIR="${BUILD_DIR:-/tmp/waystone-appimage}"
APPDIR="$BUILD_DIR/Kwaystone.AppDir"
rm -rf "$BUILD_DIR"
mkdir -p \
  "$APPDIR/usr/bin" \
  "$APPDIR/usr/lib/waystone/brain/dist" \
  "$APPDIR/usr/lib/waystone/brain/vendor/ee2" \
  "$APPDIR/usr/lib/waystone/brain/vendor/ee2/renderer" \
  "$APPDIR/usr/lib/girepository-1.0" \
  "$APPDIR/usr/share/applications" \
  "$APPDIR/usr/share/doc/waystone" \
  "$APPDIR/usr/share/glib-2.0/schemas" \
  "$APPDIR/usr/share/icons/hicolor/scalable/apps" \
  "$APPDIR/usr/share/licenses/waystone" \
  "$APPDIR/usr/share/metainfo" \
  "$APPDIR/usr/share/waystone/ocr-models" \
  "$OUT_DIR"

rsync -a --delete \
  --exclude='__pycache__/' --exclude='*.py[co]' \
  "$PYTHON_ROOT/" "$APPDIR/usr/python/"
rsync -a \
  --exclude='__pycache__/' --exclude='*.py[co]' \
  "$BUILD_VENV/lib/python$PYTHON_MINOR/site-packages/" \
  "$APPDIR/usr/python/lib/python$PYTHON_MINOR/site-packages/"
for sysconfig_data in "$APPDIR/usr/python/lib/python$PYTHON_MINOR"/_sysconfigdata_*.py; do
  [[ -f "$sysconfig_data" ]] || continue
  sed -i "s|$PYTHON_ROOT|/usr/python|g" "$sysconfig_data"
done
find "$APPDIR/usr/python" -type f -name direct_url.json -delete

install -Dm755 /opt/node/bin/node "$APPDIR/usr/bin/node"
for command in wl-copy wl-paste xdotool grim gdk-pixbuf-query-loaders; do
  path="$(command -v "$command" 2>/dev/null || true)"
  [[ -n "$path" ]] && install -Dm755 "$path" "$APPDIR/usr/bin/$command"
done

install -Dm644 "$ROOT/brain/dist/server.mjs" \
  "$APPDIR/usr/lib/waystone/brain/dist/server.mjs"
rsync -a --delete "$ROOT/brain/vendor/ee2/public/" \
  "$APPDIR/usr/lib/waystone/brain/vendor/ee2/public/"
rsync -a --delete "$ROOT/brain/vendor/ee2/renderer/dist/" \
  "$APPDIR/usr/lib/waystone/brain/vendor/ee2/renderer/dist/"

while read -r _hash relative; do
  install -Dm644 "$MODEL_ROOT/$relative" \
    "$APPDIR/usr/share/waystone/ocr-models/$relative"
done < "$ROOT/packaging/appimage/models.sha256"
(cd "$APPDIR/usr/share/waystone/ocr-models" &&
  sha256sum -c "$ROOT/packaging/appimage/models.sha256")

rsync -a /usr/lib/x86_64-linux-gnu/girepository-1.0/ \
  "$APPDIR/usr/lib/girepository-1.0/"
if [[ -d /usr/local/lib/x86_64-linux-gnu/girepository-1.0 ]]; then
  rsync -a /usr/local/lib/x86_64-linux-gnu/girepository-1.0/ \
    "$APPDIR/usr/lib/girepository-1.0/"
fi
rsync -a /usr/share/glib-2.0/schemas/ "$APPDIR/usr/share/glib-2.0/schemas/"
glib-compile-schemas "$APPDIR/usr/share/glib-2.0/schemas"
if [[ -d /usr/lib/x86_64-linux-gnu/gdk-pixbuf-2.0 ]]; then
  rsync -a /usr/lib/x86_64-linux-gnu/gdk-pixbuf-2.0/ \
    "$APPDIR/usr/lib/gdk-pixbuf-2.0/"
fi
if [[ -d /usr/share/icons/Adwaita ]]; then
  rsync -a /usr/share/icons/Adwaita/ "$APPDIR/usr/share/icons/Adwaita/"
fi

install -Dm644 "$ROOT/LICENSE" "$APPDIR/usr/share/licenses/waystone/LICENSE"
install -Dm644 "$ROOT/brain/vendor/ee2/LICENSE" \
  "$APPDIR/usr/share/licenses/waystone/Exiled-Exchange-2-MIT"
install -Dm644 /usr/share/common-licenses/Apache-2.0 \
  "$APPDIR/usr/share/licenses/waystone/PaddleOCR-Apache-2.0"

site="$APPDIR/usr/python/lib/python$PYTHON_MINOR/site-packages"
while IFS= read -r license; do
  rel="${license#"$site/"}"
  package="${rel%%/*}"
  install -Dm644 "$license" \
    "$APPDIR/usr/share/licenses/waystone/python/$package/$(basename "$license")"
done < <(
  find "$site" -type f \( -iname 'LICENSE*' -o -iname 'COPYING*' \) | sort
)

"$APPDIR/usr/python/bin/python$PYTHON_MINOR" \
  "$ROOT/packaging/appimage/sbom.py" \
  "$APPDIR/usr/share/doc/waystone/sbom.spdx.json" \
  "$ROOT/brain/package-lock.json" \
  "$ROOT/brain/vendor/ee2/renderer/package-lock.json" \
  "$RUNTIME"

install -Dm644 "$ROOT/packaging/io.github.kriskruse.waystone.desktop" \
  "$APPDIR/io.github.kriskruse.waystone.desktop"
install -Dm644 "$ROOT/packaging/io.github.kriskruse.waystone.desktop" \
  "$APPDIR/usr/share/applications/io.github.kriskruse.waystone.desktop"
install -Dm644 "$ROOT/packaging/io.github.kriskruse.waystone.appdata.xml" \
  "$APPDIR/usr/share/metainfo/io.github.kriskruse.waystone.appdata.xml"
install -Dm644 "$ROOT/packaging/waystone.svg" "$APPDIR/waystone.svg"
install -Dm644 "$ROOT/packaging/waystone.svg" \
  "$APPDIR/usr/share/icons/hicolor/scalable/apps/waystone.svg"

gcc -shared -fPIC -O2 \
  -o "$APPDIR/usr/lib/waystone/webkit-exec-redirect.so" \
  "$ROOT/packaging/appimage/webkit-exec-redirect.c" -ldl

cat > "$APPDIR/AppRun" <<EOF
#!/bin/sh
set -eu
SELF="\$(readlink -f "\$0")"
APPDIR="\${APPDIR:-\$(cd "\$(dirname "\$SELF")" && pwd)}"
PYTHON_MINOR="$PYTHON_MINOR"
RUNTIME="$RUNTIME"

cache_root="\${XDG_CACHE_HOME:-\$HOME/.cache}/waystone"
mkdir -p "\$cache_root"

export WAYSTONE_BRAIN_DIR="\$APPDIR/usr/lib/waystone/brain"
export WAYSTONE_OCR_MODEL_ROOT="\$APPDIR/usr/share/waystone/ocr-models"
if [ "\$RUNTIME" = nvidia ]; then
  export WAYSTONE_PADDLE_DEVICE="\${WAYSTONE_PADDLE_DEVICE:-gpu:0}"
else
  export WAYSTONE_PADDLE_DEVICE=cpu
fi
export WAYSTONE_LAYER_SHELL="\$APPDIR/usr/lib/libgtk4-layer-shell.so.0"
export PADDLE_PDX_CACHE_HOME="\$cache_root/paddlex"
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
export PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT=0
export PYTHONDONTWRITEBYTECODE=1
export PYTHONHOME="\$APPDIR/usr/python"
export PATH="\$APPDIR/usr/bin:\$APPDIR/usr/python/bin:\$PATH"
export GI_TYPELIB_PATH="\$APPDIR/usr/lib/girepository-1.0\${GI_TYPELIB_PATH:+:\$GI_TYPELIB_PATH}"
export GSETTINGS_SCHEMA_DIR="\$APPDIR/usr/share/glib-2.0/schemas"
export XDG_DATA_DIRS="\$APPDIR/usr/share:\${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
export GDK_BACKEND="\${GDK_BACKEND:-wayland}"
# Release WebKitGTK ignores WEBKIT_EXEC_PATH and hard-codes its helper
# directory, which does not exist outside the build distro. The preloaded
# shim rewrites helper spawn/dlopen paths into the bundle; the sandbox is
# disabled because it cannot start helpers from a relocated bundle either.
webkit_bundle=""
webkit_src=""
if [ -d "\$APPDIR/usr/libexec/webkitgtk-6.0" ]; then
  webkit_bundle="\$APPDIR/usr/libexec/webkitgtk-6.0"
  webkit_src="/usr/libexec/webkitgtk-6.0/"
elif [ -d "\$APPDIR/usr/lib/x86_64-linux-gnu/webkitgtk-6.0" ]; then
  webkit_bundle="\$APPDIR/usr/lib/x86_64-linux-gnu/webkitgtk-6.0"
  webkit_src="/usr/lib/x86_64-linux-gnu/webkitgtk-6.0/"
fi
if [ -n "\$webkit_bundle" ] && [ -f "\$APPDIR/usr/lib/waystone/webkit-exec-redirect.so" ]; then
  export WAYSTONE_WEBKIT_EXEC_SRC="\$webkit_src"
  export WAYSTONE_WEBKIT_EXEC_REDIRECT="\$webkit_bundle"
  export LD_PRELOAD="\$APPDIR/usr/lib/waystone/webkit-exec-redirect.so\${LD_PRELOAD:+:\$LD_PRELOAD}"
  export WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1
fi

python_site="\$APPDIR/usr/python/lib/python\$PYTHON_MINOR/site-packages"
library_path="\$APPDIR/usr/lib:\$APPDIR/usr/python/lib:\$python_site/paddle/libs"
for dir in "\$python_site"/nvidia/*/lib; do
  [ -d "\$dir" ] && library_path="\$library_path:\$dir"
done
export LD_LIBRARY_PATH="\$library_path\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"

loader_dir="\$APPDIR/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders"
if [ -d "\$loader_dir" ] && [ -x "\$APPDIR/usr/bin/gdk-pixbuf-query-loaders" ]; then
  loader_cache="\$cache_root/gdk-pixbuf-loaders-\$(printf %s "\$APPDIR" | sha256sum | cut -c1-12).cache"
  if [ ! -s "\$loader_cache" ]; then
    GDK_PIXBUF_MODULEDIR="\$loader_dir" \
      "\$APPDIR/usr/bin/gdk-pixbuf-query-loaders" "\$loader_dir"/*.so > "\$loader_cache.tmp"
    mv "\$loader_cache.tmp" "\$loader_cache"
  fi
  export GDK_PIXBUF_MODULE_FILE="\$loader_cache"
fi

case "\${WAYSTONE_APPIMAGE_TEST:-}" in
  imports)
    exec "\$APPDIR/usr/python/bin/python\$PYTHON_MINOR" -c \
      'import cv2, gi, numpy, poed; gi.require_version("Gtk", "4.0"); gi.require_version("Gtk4LayerShell", "1.0"); gi.require_version("WebKit", "6.0"); from gi.repository import Gtk, Gtk4LayerShell, WebKit; print("imports ok", Gtk.MAJOR_VERSION, cv2.__version__, numpy.__version__, WebKit.MAJOR_VERSION)'
    ;;
  ocr)
    [ "\$#" -eq 1 ] || { echo "WAYSTONE_APPIMAGE_TEST=ocr requires one image" >&2; exit 2; }
    exec "\$APPDIR/usr/python/bin/python\$PYTHON_MINOR" -m poed.ocr_paddle "\$1"
    ;;
  brain)
    cd "\$WAYSTONE_BRAIN_DIR"
    exec node dist/server.mjs
    ;;
esac

exec "\$APPDIR/usr/python/bin/python\$PYTHON_MINOR" -m poed "\$@"
EOF
chmod +x "$APPDIR/AppRun"
ln -s ../../AppRun "$APPDIR/usr/bin/waystone"

copy_library() {
  local source="$1" base destination
  [[ -f "$source" ]] || return 0
  case "$source" in
    "$APPDIR"/*) return 0 ;;
  esac
  base="$(basename "$source")"
  case "$base" in
    ld-linux-*|libc.so.*|libm.so.*|libpthread.so.*|libdl.so.*|librt.so.*|libresolv.so.*|\
    libGL.so.*|libEGL.so.*|libGLX.so.*|libOpenGL.so.*|libdrm.so.*|libgbm.so.*|libvulkan.so.*)
      return 0
      ;;
    libGLdispatch.so.*|libfontconfig.so.*|libfreetype.so.*)
      return 0
      ;;
  esac
  destination="$APPDIR/usr/lib/$base"
  [[ -e "$destination" ]] || cp -L "$source" "$destination"
}

library_for() {
  ldconfig -p | awk -v name="$1" '$1 == name { print $NF; exit }'
}

for name in \
  libgtk-4.so.1 libgtk4-layer-shell.so.0 libgirepository-1.0.so.1 \
  libwebkitgtk-6.0.so.4 libjavascriptcoregtk-6.0.so.1 libsoup-3.0.so.0 \
  libgdk_pixbuf-2.0.so.0 libgio-2.0.so.0 libglib-2.0.so.0 \
  libgobject-2.0.so.0 libpango-1.0.so.0 libpangocairo-1.0.so.0 libcairo.so.2; do
  copy_library "$(library_for "$name")"
done

for webkit_dir in \
  /usr/libexec/webkitgtk-6.0 \
  /usr/lib/x86_64-linux-gnu/webkitgtk-6.0; do
  if [[ -d "$webkit_dir" ]]; then
    mkdir -p "$APPDIR$(dirname "$webkit_dir")"
    rsync -a "$webkit_dir/" "$APPDIR$webkit_dir/"
  fi
done

for _ in 1 2 3 4; do
  while IFS= read -r elf; do
    while IFS= read -r dependency; do
      copy_library "$dependency"
    done < <(
      ldd "$elf" 2>/dev/null |
        sed -n -e 's/.*=> \(\/[^ ]*\) .*/\1/p' -e 's/^[[:space:]]*\(\/[^ ]*\) .*/\1/p'
    )
  done < <(
    find "$APPDIR" -type f -print0 |
      xargs -0 file |
      awk -F: '/ELF .* (executable|shared object)/ { print $1 }'
  )
done

# Do not strip wheel-provided native libraries. Several manylinux wheels use
# carefully aligned/renamed ELF payloads that GNU strip can corrupt. Only the
# Debian libraries copied into our flat runtime directory are safe to strip.
find "$APPDIR/usr/lib" -maxdepth 1 -type f \
  \( -name '*.so' -o -name '*.so.*' \) \
  -exec strip --strip-unneeded {} + 2>/dev/null || true
find "$APPDIR" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
find "$APPDIR" -depth -type d -name __pycache__ -exec rm -rf {} +

validation_library_path="$APPDIR/usr/lib:$APPDIR/usr/python/lib:$site/paddle/libs"
for directory in "$site"/nvidia/*/lib; do
  [[ -d "$directory" ]] && validation_library_path="$validation_library_path:$directory"
done
missing="$BUILD_DIR/missing-libraries.txt"
while IFS= read -r elf; do
  LD_LIBRARY_PATH="$validation_library_path" ldd "$elf" 2>/dev/null |
    grep 'not found' && printf 'required by %s\n' "$elf"
done < <(
  find "$APPDIR/usr/bin" "$APPDIR/usr/python/bin" "$APPDIR/usr/lib" "$APPDIR/usr/libexec" \
    -type f -print0 2>/dev/null |
    xargs -0 file |
    awk -F: '/ELF .* (executable|shared object)/ { print $1 }'
) > "$missing" || true
if [[ -s "$missing" ]]; then
  cat "$missing" >&2
  exit 1
fi

actual_models="$BUILD_DIR/models.txt"
(cd "$APPDIR/usr/share/waystone/ocr-models" &&
  find . -type f -printf '%P\n' | sort) > "$actual_models"
expected_models="$BUILD_DIR/models.expected"
awk '{print $2}' "$ROOT/packaging/appimage/models.sha256" | sort > "$expected_models"
diff -u "$expected_models" "$actual_models"

env APPDIR="$APPDIR" WAYSTONE_APPIMAGE_TEST=imports "$APPDIR/AppRun"
if [[ "$RUNTIME" == cpu ]]; then
  env APPDIR="$APPDIR" WAYSTONE_APPIMAGE_TEST=ocr \
    "$APPDIR/AppRun" "$ROOT/screenshots/Currency_lookup.png" >/dev/null
fi

private_paths="$BUILD_DIR/private-paths.txt"
rm -f "$private_paths"
for private_pattern in \
  "$ROOT/poed" \
  "$ROOT/brain" \
  /home/runner/work/waystone \
  /github/home; do
  grep -R -a -l -F "$private_pattern" "$APPDIR" >> "$private_paths" || true
done
if [[ -s "$private_paths" ]]; then
  echo "AppDir contains build-machine paths:" >&2
  sort -u "$private_paths" | head -30 >&2
  exit 1
fi

artifact="Kwaystone-$([[ "$RUNTIME" == nvidia ]] && printf nvidia- || true)x86_64.AppImage"
rm -f "$OUT_DIR/$artifact"
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 \
  /opt/appimagetool --runtime-file /opt/runtime-x86_64 \
    --comp zstd "$APPDIR" "$OUT_DIR/$artifact"
chmod +x "$OUT_DIR/$artifact"

size="$(stat -c %s "$OUT_DIR/$artifact")"
if [[ "$RUNTIME" == cpu && "$size" -ge 2000000000 ]]; then
  echo "$artifact exceeds GitHub's per-asset release limit" >&2
  exit 1
fi
if [[ "$RUNTIME" == nvidia && "$size" -gt 5700000000 ]]; then
  echo "$artifact is too large to divide into three sub-1.9-GB release assets" >&2
  exit 1
fi
if [[ "$RUNTIME" == nvidia && "$size" -ge 2000000000 ]]; then
  echo "$artifact will be split into sub-2-GiB release assets" >&2
fi
(cd "$OUT_DIR" && sha256sum "$artifact" > "SHA256SUMS-$RUNTIME")
cp "$APPDIR/usr/share/doc/waystone/sbom.spdx.json" \
  "$OUT_DIR/Kwaystone-$RUNTIME.spdx.json"
