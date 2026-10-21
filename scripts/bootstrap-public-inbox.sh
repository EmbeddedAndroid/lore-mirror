#!/bin/sh
# Rootless Debian 13 installer from signed Debian packages and official upstream.
# Other distributions: install the listed Perl dependencies using the distro
# package manager, then run with LORE_SKIP_DEPS=1.
set -eu
PREFIX=${1:-"$PWD/.runtime"}
case "$PREFIX" in /*) ;; *) PREFIX="$PWD/$PREFIX" ;; esac
PIN=d6b9850df271f3fed85f6d222ec39249d8bc061f
mkdir -p "$PREFIX/src" "$PREFIX/deps" "$PREFIX/logs"
if [ "${LORE_SKIP_DEPS:-0}" != 1 ]; then
    # XS modules must match the OS Perl ABI: do not load these into another Perl.
    grep -q '^VERSION_ID="13"' /etc/os-release || {
        echo 'Rootless dependency install is tested on Debian 13 only.' >&2
        echo 'Install DBI, DBD::SQLite, Search::Xapian, URI, Email::Address::XS,' >&2
        echo 'Socket::MsgHdr, Linux::Inotify2 and Net::Server from your distro;' >&2
        echo 'then rerun with LORE_SKIP_DEPS=1.' >&2
        exit 1
    }
    ARCH=$(dpkg --print-architecture)
    TRIPLET=$(dpkg-architecture -qDEB_HOST_MULTIARCH)
    PERL_ABI=$(perl -MConfig -e 'print "$Config{PERL_REVISION}.$Config{PERL_VERSION}"')
    APT_DIR="$PREFIX/apt"
    mkdir -p "$APT_DIR/etc/apt.conf.d" "$APT_DIR/etc/trusted.gpg.d" \
      "$APT_DIR/etc/preferences.d" "$APT_DIR/lists/partial" "$APT_DIR/cache/archives/partial"
    printf 'deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://deb.debian.org/debian trixie main\n' > "$APT_DIR/etc/sources.list"
    cat > "$APT_DIR/apt.conf" <<EOF
Dir::Etc "$APT_DIR/etc";
Dir::State::lists "$APT_DIR/lists";
Dir::Cache "$APT_DIR/cache";
APT::Get::List-Cleanup "0";
EOF
    APT_CONFIG="$APT_DIR/apt.conf" /usr/bin/apt-get update
    (cd "$PREFIX/deps" && APT_CONFIG="$APT_DIR/apt.conf" /usr/bin/apt-get download \
      libdbi-perl libdbd-sqlite3-perl libsearch-xapian-perl libxapian30 liburi-perl \
      libemail-address-xs-perl libsocket-msghdr-perl liblinux-inotify2-perl \
      libnet-server-perl libtimedate-perl time)
    for deb in "$PREFIX"/deps/*.deb; do dpkg-deb -x "$deb" "$PREFIX/deps"; done
else
    TRIPLET=$(perl -MConfig -e 'print $Config{archname}' | sed 's/-thread-multi//')
    PERL_ABI=$(perl -MConfig -e 'print "$Config{PERL_REVISION}.$Config{PERL_VERSION}"')
fi
if [ ! -d "$PREFIX/src/public-inbox/.git" ]; then
    git -c http.userAgent='lore-mirror/0.1 bootstrap' clone --no-checkout \
      https://kernel.googlesource.com/pub/scm/infra/public-inbox "$PREFIX/src/public-inbox"
fi
git -C "$PREFIX/src/public-inbox" cat-file -e "$PIN^{commit}"
git -C "$PREFIX/src/public-inbox" checkout --detach "$PIN"
[ "$(git -C "$PREFIX/src/public-inbox" rev-parse HEAD)" = "$PIN" ]
cat > "$PREFIX/env.sh" <<EOF
export LORE_DATA='$PREFIX'
export PERL5LIB='$PREFIX/install/lib/perl5:$PREFIX/src/public-inbox/lib:$PREFIX/deps/usr/share/perl5:$PREFIX/deps/usr/lib/$TRIPLET/perl5/$PERL_ABI'
export LD_LIBRARY_PATH='$PREFIX/deps/usr/lib/$TRIPLET'
export PATH='$PREFIX/install/bin:$PREFIX/deps/usr/bin':\$PATH
export PI_CONFIG='$PREFIX/public-inbox-config'
export XDG_CONFIG_HOME='$PREFIX/runtime/config'
export XDG_DATA_HOME='$PREFIX/runtime/data'
export XDG_CACHE_HOME='$PREFIX/runtime/cache'
export XDG_RUNTIME_DIR='$PREFIX/runtime/run'
EOF
. "$PREFIX/env.sh"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
perl -MDBI -MDBD::SQLite -MSearch::Xapian -MURI -MSocket::MsgHdr -e 'print "Perl dependencies verified\n"'
(cd "$PREFIX/src/public-inbox" && perl Makefile.PL INSTALL_BASE="$PREFIX/install" && make -j2 && make install) > "$PREFIX/logs/install.log" 2>&1
perl -MPublicInbox::Search -e 'PublicInbox::Search::load_xapian() or die "Xapian unavailable"; print "public-inbox + Xapian verified\n"'
printf '%s\n' "$PIN" > "$PREFIX/public-inbox.commit"
printf 'Installed pinned public-inbox. Load environment: . %s/env.sh\n' "$PREFIX"
echo 'LEI additionally requires Unix SOCK_SEQPACKET sockets. The direct query helper does not.'
