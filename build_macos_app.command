#!/bin/bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

echo "=========================================================="
echo "  Building SonicFlow.app for macOS (/Applications)        "
echo "=========================================================="

APP_PATH="/Applications/SonicFlow.app"
CONTENTS_PATH="$APP_PATH/Contents"
MACOS_PATH="$CONTENTS_PATH/MacOS"
RESOURCES_PATH="$CONTENTS_PATH/Resources"

mkdir -p "$MACOS_PATH"
mkdir -p "$RESOURCES_PATH"

# 1. Install Application Icon
if [ -f "$DIR/SonicFlow.icns" ]; then
    cp "$DIR/SonicFlow.icns" "$RESOURCES_PATH/SonicFlow.icns"
    echo "✓ Installed application icon"
fi

# 2. Write Info.plist
cat << 'EOF' > "$CONTENTS_PATH/Info.plist"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleExecutable</key>
    <string>SonicFlow</string>
    <key>CFBundleIconFile</key>
    <string>SonicFlow</string>
    <key>CFBundleIdentifier</key>
    <string>com.sanjaysprasad.sonicflow</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>SonicFlow</string>
    <key>CFBundleDisplayName</key>
    <string>SonicFlow</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.3.1</string>
    <key>CFBundleVersion</key>
    <string>2</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSSupportsAutomaticGraphicsSwitching</key>
    <true/>
</dict>
</plist>
EOF
echo "✓ Generated Info.plist"

# 3. Compile Launcher Binary
TEMP_C="/tmp/sonicflow_launcher.c"
cat << 'EOF' > "$TEMP_C"
#include <unistd.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>

int main(int argc, char *argv[]) {
    const char *project_dir = "/Users/ssp/Documents/SSP CODES/Yt to MP3";
    if (chdir(project_dir) != 0) {
        perror("chdir failed");
    }

    const char *existing_path = getenv("PATH");
    char path_buf[4096];
    if (existing_path) {
        snprintf(path_buf, sizeof(path_buf),
            "/opt/homebrew/bin:/opt/homebrew/sbin:/Library/Frameworks/Python.framework/Versions/3.14/bin:/Library/Frameworks/Python.framework/Versions/3.13/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin:/Library/Frameworks/Python.framework/Versions/3.11/bin:/usr/local/bin:%s",
            existing_path);
    } else {
        snprintf(path_buf, sizeof(path_buf),
            "/opt/homebrew/bin:/opt/homebrew/sbin:/Library/Frameworks/Python.framework/Versions/3.14/bin:/Library/Frameworks/Python.framework/Versions/3.13/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin");
    }
    setenv("PATH", path_buf, 1);
    setenv("PYTHONUNBUFFERED", "1", 1);

    const char *python_candidates[] = {
        "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
        "python3",
        NULL
    };

    const char *python_bin = "python3";
    for (int i = 0; python_candidates[i] != NULL; i++) {
        if (access(python_candidates[i], X_OK) == 0) {
            python_bin = python_candidates[i];
            break;
        }
    }

    char *args[] = { (char *)python_bin, "/Users/ssp/Documents/SSP CODES/Yt to MP3/app.py", NULL };
    execv(python_bin, args);
    execvp(python_bin, args);
    perror("Failed to launch SonicFlow");
    return 1;
}
EOF

clang -O3 -arch arm64 "$TEMP_C" -o "$MACOS_PATH/SonicFlow"
chmod +x "$MACOS_PATH/SonicFlow"
rm -f "$TEMP_C"
echo "✓ Compiled native Mach-O arm64 launcher binary"

# 4. Refresh macOS LaunchServices
touch "$APP_PATH"
if [ -x "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister" ]; then
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP_PATH" || true
fi

echo ""
echo "=========================================================="
echo "  🎉 SUCCESS: SonicFlow.app rebuilt in /Applications!      "
echo "  You can now launch it from Launchpad, Finder, or Dock.  "
echo "=========================================================="
