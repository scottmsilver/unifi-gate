#!/bin/bash
# Unattended Android SDK setup script

set -e

ANDROID_HOME="${ANDROID_HOME:-$HOME/Android/Sdk}"
CMDLINE_TOOLS_VERSION="11076708"  # Latest as of 2024
CMDLINE_TOOLS_URL="https://dl.google.com/android/repository/commandlinetools-linux-${CMDLINE_TOOLS_VERSION}_latest.zip"

echo "=== Android SDK Unattended Setup ==="
echo "Installing to: $ANDROID_HOME"
echo ""

# 1. Install dependencies
echo "[1/9] Installing dependencies..."
sudo apt-get update -qq
sudo apt-get install -y -qq openjdk-17-jdk wget unzip libpulse0 libnss3 libxcomposite1 libxcursor1 libxi6 libxtst6

# Set JAVA_HOME
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
echo "JAVA_HOME set to: $JAVA_HOME"

# 2. Create SDK directory structure
echo "[2/9] Creating SDK directories..."
mkdir -p "$ANDROID_HOME/cmdline-tools"

# 3. Download command-line tools
echo "[3/9] Downloading Android command-line tools..."
cd /tmp
wget -q --show-progress "$CMDLINE_TOOLS_URL" -O cmdline-tools.zip
unzip -q -o cmdline-tools.zip
rm -rf "$ANDROID_HOME/cmdline-tools/latest"
mv cmdline-tools "$ANDROID_HOME/cmdline-tools/latest"
rm cmdline-tools.zip

# 4. Set up environment
echo "[4/9] Setting up environment..."
export PATH="$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:$PATH"

# Add to shell config if not already there
SHELL_RC="$HOME/.bashrc"
if ! grep -q "ANDROID_HOME" "$SHELL_RC" 2>/dev/null; then
    echo "" >> "$SHELL_RC"
    echo "# Android SDK" >> "$SHELL_RC"
    echo "export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64" >> "$SHELL_RC"
    echo "export ANDROID_HOME=\"$ANDROID_HOME\"" >> "$SHELL_RC"
    echo "export PATH=\"\$JAVA_HOME/bin:\$ANDROID_HOME/cmdline-tools/latest/bin:\$ANDROID_HOME/platform-tools:\$ANDROID_HOME/emulator:\$PATH\"" >> "$SHELL_RC"
    echo "Added Android SDK and JAVA_HOME to $SHELL_RC"
fi

# 5. Accept licenses (unattended)
echo "[5/9] Accepting licenses..."
yes | sdkmanager --licenses > /dev/null 2>&1 || true

# 6. Install required SDK components
echo "[6/9] Installing SDK components (this may take a few minutes)..."
sdkmanager --install \
    "platform-tools" \
    "platforms;android-34" \
    "build-tools;34.0.0" \
    --verbose | grep -E "^(Preparing|Installing|Done)" || true

# 7. Install emulator and system image
echo "[7/9] Installing emulator and system image (this may take several minutes)..."
sdkmanager --install \
    "emulator" \
    "system-images;android-34;google_apis;x86_64" \
    --verbose | grep -E "^(Preparing|Installing|Done)" || true

# 8. Create AVD (Android Virtual Device)
echo "[8/9] Creating Android Virtual Device..."
echo "no" | avdmanager create avd \
    --name "Pixel_6_API_34" \
    --package "system-images;android-34;google_apis;x86_64" \
    --device "pixel_6" \
    --force

# 9. Create local.properties for Gradle
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "[9/9] Creating local.properties..."
cat > "$SCRIPT_DIR/local.properties" << EOF
sdk.dir=$ANDROID_HOME
EOF

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Android SDK installed to: $ANDROID_HOME"
echo "Emulator AVD created: Pixel_6_API_34"
echo "local.properties created at: $SCRIPT_DIR/local.properties"
echo ""
echo "To use immediately, run:"
echo "  source ~/.bashrc"
echo ""
echo "To start the emulator:"
echo "  emulator -avd Pixel_6_API_34"
echo ""
echo "To build and install the app:"
echo "  cd $SCRIPT_DIR"
echo "  ./gradlew assembleDebug"
echo "  adb install app/build/outputs/apk/debug/app-debug.apk"
echo ""
echo "Or build and run directly:"
echo "  ./gradlew installDebug"
echo ""
