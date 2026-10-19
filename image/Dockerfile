# In-house signal-cli image: the official signal-cli release (with Signal's own libsignal) on distroless.
# Nothing third-party: every download is checked, and the build stops if any check fails.
#   - signal-cli tarball: AsamK's GPG signature (key pinned by fingerprint) and a pinned SHA-256
#   - libsignal_jni_amd64.so inside it: byte-identical to Signal's official libsignal-client release
#   - wire-runtime-jvm: replaced with the patched release (CVE-2026-45799), Block's GPG signature + pinned SHA-256
#   - Java and qrencode: Debian packages (signed by Debian); both base images pinned by digest
# The runtime image has no shell: Java starts directly from an argument file written at build time,
# through our launcher (launcher/MessageLogLauncher.java), which redacts the received-message log (MESSAGE_LOG).
# Build:   docker build -t signal-cli:0.14.9 /apps/signal-relay/image
# Update:  see README.md ("Updating").
# linux/amd64 only (the checked libsignal build is the amd64 one).

ARG BUILD_BASE=debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f
ARG RUNTIME_BASE=gcr.io/distroless/cc-debian13@sha256:159783207c2cd44c2aa5715961d13c8612368ac9bd450f887e3f08fc8ea461e3

# ---------- build: download, verify, trim ----------
FROM ${BUILD_BASE} AS build
ARG SIGNAL_CLI_VERSION=0.14.9
ARG SIGNAL_CLI_SHA256=c32b87f587198cbd277c9d71228716ee6047ee2b63e8e88728e8280d9407d65a
ARG SIGNAL_CLI_SIGNER=FA10826A74907F9EC6BBB7FC2BA2CD21B5B09570
# Signal's official libsignal-client 0.103.0 (build-artifacts.signal.org), file libsignal_jni_amd64.so
ARG LIBSIGNAL_SO_SHA256=e0f4e783c25d0eea3252d9da36dd1dc9752c075dd5c10d1d3f30d9798346050b
# Wire runtime: signal-cli 0.14.9 bundles 6.0.0-alpha02 (CVE-2026-45799); 6.3.0 is the fixed release.
# Drop this swap once signal-cli ships 6.3.0 or later.
ARG WIRE_OLD=6.0.0-alpha02
ARG WIRE_VERSION=6.3.0
ARG WIRE_SHA256=015fe322956d43efeed350ece606e3c2eb6f7448e1b982eca64dcc4ff9b4a5d0
ARG WIRE_SIGNER=1D217F8475EEE9F19AB8DD6B793FD5751A0F0780

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates ca-certificates-java curl gnupg gpgv unzip zip binutils \
      openjdk-25-jdk-headless qrencode \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY keys/asamk-signal-cli.asc keys/block-oss-releases.asc ./
RUN gpg --batch --quiet --import asamk-signal-cli.asc block-oss-releases.asc

# 1. signal-cli release: good signature from the pinned key, and the pinned checksum
RUN set -eu; \
    gpg --batch --with-colons --fingerprint | grep -q "^fpr:::::::::${SIGNAL_CLI_SIGNER}:" \
      || { echo "signing key fingerprint does not match ${SIGNAL_CLI_SIGNER}"; exit 1; }; \
    gpg --batch --export "${SIGNAL_CLI_SIGNER}" > asamk.gpg; \
    url=https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz; \
    curl -fsSL -o signal-cli.tar.gz "$url"; \
    curl -fsSL -o signal-cli.tar.gz.asc "$url.asc"; \
    gpgv --keyring ./asamk.gpg signal-cli.tar.gz.asc signal-cli.tar.gz; \
    echo "${SIGNAL_CLI_SHA256}  signal-cli.tar.gz" | sha256sum -c -; \
    tar xzf signal-cli.tar.gz; \
    mv signal-cli-${SIGNAL_CLI_VERSION} signal-cli

# 2. Signal's encryption library must be Signal's own build; drop the macOS/Windows copies (~87 MB)
RUN set -eu; \
    jar=$(ls signal-cli/lib/libsignal-client-*.jar); \
    got=$(unzip -p "$jar" libsignal_jni_amd64.so | sha256sum | cut -d' ' -f1); \
    [ "$got" = "${LIBSIGNAL_SO_SHA256}" ] || { echo "libsignal_jni_amd64.so is $got, expected ${LIBSIGNAL_SO_SHA256}"; exit 1; }; \
    zip -q -d "$jar" '*.dylib' '*.dll'; \
    echo "native libraries left in $jar:"; unzip -l "$jar" | grep -E '\.(so|dylib|dll)$'

# 3. Patched Wire runtime from Maven Central: good signature from Block's pinned key, and the pinned checksum
RUN set -eu; \
    [ -f signal-cli/lib/wire-runtime-jvm-${WIRE_OLD}.jar ] \
      || { echo "wire-runtime-jvm-${WIRE_OLD}.jar not in this signal-cli: review or remove the Wire swap"; exit 1; }; \
    gpg --batch --with-colons --fingerprint | grep -q "^fpr:::::::::${WIRE_SIGNER}:" \
      || { echo "signing key fingerprint does not match ${WIRE_SIGNER}"; exit 1; }; \
    gpg --batch --export "${WIRE_SIGNER}" > block.gpg; \
    jar=wire-runtime-jvm-${WIRE_VERSION}.jar; \
    url=https://repo1.maven.org/maven2/com/squareup/wire/wire-runtime-jvm/${WIRE_VERSION}/$jar; \
    curl -fsSL -o "$jar" "$url"; \
    curl -fsSL -o "$jar.asc" "$url.asc"; \
    gpgv --keyring ./block.gpg "$jar.asc" "$jar"; \
    echo "${WIRE_SHA256}  $jar" | sha256sum -c -; \
    rm signal-cli/lib/wire-runtime-jvm-${WIRE_OLD}.jar; \
    mv "$jar" signal-cli/lib/

# 4. Start-up without a shell: the official launcher's java options and class path, as a java @argfile
RUN set -eu; \
    cp=$(sed -n 's/^CLASSPATH=//p' signal-cli/bin/signal-cli \
         | sed "s#\$APP_HOME#/opt/signal-cli#g; s#wire-runtime-jvm-${WIRE_OLD}.jar#wire-runtime-jvm-${WIRE_VERSION}.jar#"); \
    grep -q "^DEFAULT_JVM_OPTS='\"--enable-native-access=ALL-UNNAMED\"'$" signal-cli/bin/signal-cli \
      || { echo "launcher JVM options changed: update java.args"; exit 1; }; \
    for j in $(echo "$cp" | tr ':' ' '); do [ -f "signal-cli/lib/$(basename "$j")" ] || { echo "missing $j"; exit 1; }; done; \
    printf '%s\n' --enable-native-access=ALL-UNNAMED -classpath "$cp:/opt/signal-cli/launcher" relay.MessageLogLauncher \
      > signal-cli/java.args; \
    rm -r signal-cli/bin

# 4b. Our launcher (launcher/MessageLogLauncher.java): starts signal-cli's own Main with its received-message log
#     redacted, full or off (MESSAGE_LOG). Uses only Java and the Jackson already bundled with signal-cli.
#     TLS_LISTEN also starts a mutual-TLS gateway in front of signal-cli's HTTP service (launcher/TlsGateway.java).
#     launcher/SignalHealth.java (r2): signal-cli's link to Signal's servers and its own problems, for /api/v1/check.
COPY launcher/MessageLogLauncher.java launcher/TlsGateway.java launcher/SignalHealth.java launcher/
RUN javac --release 25 -Xlint:all -Werror -cp 'signal-cli/lib/*' -d signal-cli/launcher launcher/*.java

# 5. A Java runtime with only the modules signal-cli uses: the union of each library's needs
#    (jdeps can't analyse all the jars in one run because some are named modules that clash)
RUN set -eu; \
    mods=$(for j in signal-cli/lib/*.jar; do \
             jdeps --multi-release 25 --ignore-missing-deps --print-module-deps --class-path 'signal-cli/lib/*' "$j" \
               2>/dev/null | tail -n 1; \
           done | grep -E '^(java|jdk)\.' | tr ',' '\n' | sort -u | paste -sd, -); \
    echo "Java modules: $mods"; \
    jlink --add-modules "$mods" --output /opt/java \
          --strip-debug --no-header-files --no-man-pages --compress=zip-6; \
    rm -f /opt/java/lib/security/cacerts; \
    cp -L /etc/ssl/certs/java/cacerts /opt/java/lib/security/cacerts

# 6. qrencode for link QR codes, with the two libraries distroless doesn't have
RUN set -eu; \
    mkdir -p /stage/usr/bin /stage/usr/lib/x86_64-linux-gnu; \
    cp /usr/bin/qrencode /stage/usr/bin/; \
    for lib in libqrencode.so.4 libpng16.so.16; do cp -L /usr/lib/x86_64-linux-gnu/$lib /stage/usr/lib/x86_64-linux-gnu/; done

# 6b. Licence notices (r2): signal-cli (GPL-3.0), libsignal (AGPL-3.0), the swapped-in Wire runtime (Apache-2.0), and the
#     Debian copyright files of qrencode and libpng. The libraries in lib/ carry their own licences inside their jars;
#     the Java runtime's are in /opt/java/legal. Texts come from Debian's base-files (pinned base image), not downloads.
RUN set -eu; d=/stage/usr/share/doc/signal-cli-image; mkdir -p "$d"; \
    cp /usr/share/common-licenses/GPL-3 "$d/signal-cli.GPL-3.0.txt"; \
    cp /usr/share/common-licenses/AGPL-3.0 "$d/libsignal.AGPL-3.0.txt"; \
    cp /usr/share/common-licenses/Apache-2.0 "$d/wire-runtime.Apache-2.0.txt"; \
    for pkg in $(dpkg -S /usr/bin/qrencode /usr/lib/x86_64-linux-gnu/libqrencode.so.4 /usr/lib/x86_64-linux-gnu/libpng16.so.16 \
                 | cut -d: -f1 | sort -u); do cp "/usr/share/doc/$pkg/copyright" "$d/$pkg.copyright"; done; \
    libsignal=$(ls signal-cli/lib/libsignal-client-*.jar | sed 's/.*libsignal-client-//; s/\.jar$//'); \
    { echo "This image contains:"; \
      echo "- signal-cli ${SIGNAL_CLI_VERSION}, (c) AsamK and contributors, GPL-3.0 (signal-cli.GPL-3.0.txt)."; \
      echo "  Source: https://github.com/AsamK/signal-cli/tree/v${SIGNAL_CLI_VERSION}"; \
      echo "- libsignal ${libsignal}, (c) Signal Messenger LLC, AGPL-3.0-only (libsignal.AGPL-3.0.txt)."; \
      echo "  Source: https://github.com/signalapp/libsignal/tree/v${libsignal}"; \
      echo "- wire-runtime-jvm ${WIRE_VERSION}, (c) Block, Inc., Apache-2.0 (wire-runtime.Apache-2.0.txt)."; \
      echo "- qrencode and libpng from Debian (their *.copyright files here)."; \
      echo "- The other Java libraries in /opt/signal-cli/lib, with their licences inside each jar."; \
      echo "- A Java runtime made with jlink from Debian's OpenJDK (licences in /opt/java/legal)."; \
      echo "- The relay launcher (/opt/signal-cli/launcher): from this image's build files (image/launcher)."; \
    } > "$d/NOTICE"

# ---------- runtime: distroless (glibc, libstdc++, zlib, CA certs; no shell, no package manager) ----------
FROM ${RUNTIME_BASE}
ARG SIGNAL_CLI_VERSION=0.14.9
COPY --from=build /opt/java /opt/java
COPY --from=build /build/signal-cli /opt/signal-cli
COPY --from=build /stage/ /
ENV JAVA_HOME=/opt/java \
    PATH=/opt/java/bin:/usr/bin:/bin \
    LANG=C.UTF-8
LABEL org.opencontainers.image.title="signal-cli (in-house)" \
      org.opencontainers.image.version="${SIGNAL_CLI_VERSION}" \
      org.opencontainers.image.description="Official signal-cli and libsignal on distroless, verified at build time"
USER 1000:1000
WORKDIR /data
VOLUME /data
# Account data (keys) lives in /data. libsignal unpacks itself into /tmp at start (~200 MB):
# run with --read-only --tmpfs /tmp:rw,exec,size=400m (exec is required to load it; see README.md).
ENTRYPOINT ["/opt/java/bin/java", "@/opt/signal-cli/java.args", "--config", "/data"]
CMD ["--help"]
