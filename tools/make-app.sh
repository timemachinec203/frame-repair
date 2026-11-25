#!/bin/zsh
# Builds "Frame Repair.app" (self-contained: app + engine + RIFE model + shaders),
# signs it for this Mac, and installs it to /Applications (or ~/Applications).
# Usage: tools/make-app.sh [--no-install]
# Output: build/Frame Repair.app (and the installed copy).
set -euo pipefail
cd "${0:A:h}/.."
ROOT="$PWD"
VERSION="1.0.0"

swift build -c release
REL="$(swift build -c release --show-bin-path)"

# Assemble outside ~/Documents: iCloud adds Finder metadata there, which code signing rejects.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
APP="$STAGE/Frame Repair.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/engine"

cp "$REL/FrameRepair" "$APP/Contents/MacOS/FrameRepair"
cp "$REL/rife-repair" "$APP/Contents/Resources/engine/rife-repair"
for b in RifeMetal_RifeMetal.bundle RifeMetal_RifeMetalCore.bundle; do
  cp -R "$REL/$b" "$APP/Contents/Resources/engine/$b"
done

# Icon
ICONSET="$STAGE/AppIcon.iconset"
rm -rf "$ICONSET"
swift "$ROOT/tools/make-icon.swift" "$ICONSET"
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
rm -rf "$ICONSET"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Frame Repair</string>
  <key>CFBundleDisplayName</key><string>Frame Repair</string>
  <key>CFBundleIdentifier</key><string>local.framerepair</string>
  <key>CFBundleExecutable</key><string>FrameRepair</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$(date +%Y%m%d%H%M)</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.video</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>CFBundleDocumentTypes</key>
  <array>
    <dict>
      <key>CFBundleTypeName</key><string>Video</string>
      <key>CFBundleTypeRole</key><string>Viewer</string>
      <key>LSHandlerRank</key><string>Alternate</string>
      <key>LSItemContentTypes</key><array><string>public.movie</string></array>
    </dict>
  </array>
</dict>
</plist>
PLIST

# Ad-hoc signature (this Mac only). Inner code first, then the app.
xattr -cr "$APP"      # Finder info / provenance attributes on copied files block signing
codesign --force --sign - "$APP/Contents/Resources/engine/rife-repair"
codesign --force --sign - "$APP/Contents/Resources/engine/RifeMetal_RifeMetal.bundle"
codesign --force --sign - "$APP/Contents/Resources/engine/RifeMetal_RifeMetalCore.bundle"
codesign --force --sign - "$APP"
codesign --verify --deep --strict "$APP"
echo "Built and verified."
mkdir -p "$ROOT/build"
rm -rf "$ROOT/build/Frame Repair.app"
ditto "$APP" "$ROOT/build/Frame Repair.app"   # copy kept with the source

if [[ "${1:-}" != "--no-install" ]]; then
  DEST="/Applications"
  [[ -w "$DEST" ]] || DEST="$HOME/Applications"
  mkdir -p "$DEST"
  rm -rf "$DEST/Frame Repair.app"
  ditto "$APP" "$DEST/Frame Repair.app"
  codesign --verify --deep --strict "$DEST/Frame Repair.app"
  echo "Installed: $DEST/Frame Repair.app"
fi
