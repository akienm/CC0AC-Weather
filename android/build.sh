#!/usr/bin/env bash
# Build cc0ac-weather.apk with the plain SDK tools: no Gradle, no Android Studio.
# Needs: a JDK 17 (JAVA_HOME or on PATH) and an Android SDK with build-tools;36.0.0
# and platforms;android-36 (ANDROID_HOME, default ~/Android/sdk).
set -euo pipefail
cd "$(dirname "$0")"

SDK="${ANDROID_HOME:-$HOME/Android/sdk}"
BT="$SDK/build-tools/36.0.0"
JAR="$SDK/platforms/android-36/android.jar"
if [ -z "${JAVA_HOME:-}" ] && ! command -v javac >/dev/null; then
    JAVA_HOME="$(ls -d "$HOME"/Android/jdk-17* 2>/dev/null | head -1)"
fi
[ -n "${JAVA_HOME:-}" ] && export PATH="$JAVA_HOME/bin:$PATH"
for f in "$BT/aapt2" "$BT/d8" "$BT/apksigner" "$JAR"; do
    [ -e "$f" ] || { echo "missing $f (see android/README.md, 'Building')" >&2; exit 1; }
done

# The signing key lives outside the repo. Keep it: an update must be signed by the same key.
KEYSTORE="${KIOSK_KEYSTORE:-$HOME/.cc0ac-weather/kiosk-release.jks}"
KEYPASS_FILE="$KEYSTORE.pass"
if [ ! -f "$KEYSTORE" ]; then
    mkdir -p "$(dirname "$KEYSTORE")"
    head -c 24 /dev/urandom | base64 | tr -d '/+=' > "$KEYPASS_FILE"
    chmod 600 "$KEYPASS_FILE"
    keytool -genkeypair -keystore "$KEYSTORE" -alias kiosk -keyalg RSA -keysize 3072 \
        -validity 36500 -dname "CN=CC0AC Weather Kiosk" \
        -storepass "$(cat "$KEYPASS_FILE")" -keypass "$(cat "$KEYPASS_FILE")" >/dev/null
    echo "made signing key $KEYSTORE"
fi

rm -rf build && mkdir -p build/res build/classes
"$BT/aapt2" compile --dir res -o build/res/
"$BT/aapt2" link -o build/unsigned.apk -I "$JAR" --manifest AndroidManifest.xml \
    --java build/gen build/res/*.flat
javac -source 17 -target 17 -Xlint:-options -classpath "$JAR" -d build/classes \
    $(find src build/gen -name '*.java')
"$BT/d8" --release --min-api 23 --lib "$JAR" --output build $(find build/classes -name '*.class')
(cd build && zip -qj unsigned.apk classes.dex)
"$BT/zipalign" -f 4 build/unsigned.apk build/aligned.apk
"$BT/apksigner" sign --ks "$KEYSTORE" --ks-pass "file:$KEYPASS_FILE" \
    --out build/cc0ac-weather.apk build/aligned.apk
"$BT/apksigner" verify build/cc0ac-weather.apk
echo "built $(pwd)/build/cc0ac-weather.apk"

# The same app as an Android App Bundle, for Google Play: the same code, resources and
# key (Play takes it as the upload key). bundletool: https://github.com/google/bundletool
BUNDLETOOL="${BUNDLETOOL:-$(ls "$HOME"/Android/bundletool/bundletool-all-*.jar 2>/dev/null | tail -1)}"
if [ -z "$BUNDLETOOL" ]; then
    echo "no bundletool jar (BUNDLETOOL, or ~/Android/bundletool/): built the .apk only" >&2
    exit 0
fi
"$BT/aapt2" link --proto-format -o build/proto.apk -I "$JAR" --manifest AndroidManifest.xml build/res/*.flat
rm -rf build/base && mkdir -p build/base/manifest build/base/dex
(cd build/base && unzip -q ../proto.apk && mv AndroidManifest.xml manifest/ && cp ../classes.dex dex/)
(cd build/base && zip -qr ../base.zip .)
java -jar "$BUNDLETOOL" build-bundle --modules=build/base.zip --output=build/unsigned.aab
cp build/unsigned.aab build/cc0ac-weather.aab
jarsigner -sigalg SHA256withRSA -digestalg SHA-256 -keystore "$KEYSTORE" \
    -storepass:file "$KEYPASS_FILE" build/cc0ac-weather.aab kiosk >/dev/null
jarsigner -verify build/cc0ac-weather.aab >/dev/null
echo "built $(pwd)/build/cc0ac-weather.aab"
