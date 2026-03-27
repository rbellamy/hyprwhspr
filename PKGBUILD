# Maintainer: Richard Bellamy <rbellamy@pteradigm.com>
# Upstream: goodroot <hyprwhspr@goodroot.ca>

pkgname=hyprwhspr
pkgver=1.24.0
pkgrel=2
pkgdesc="Fast, local speech-to-text for Arch/Omarchy"
arch=('x86_64')
url="https://github.com/rbellamy/hyprwhspr"
license=('MIT')
makedepends=()
depends=(
  'cmake'
  'git'
  'python'
  'python-pip'
  'python-setuptools'
  'python-sounddevice'
  'python-numpy'
  'python-scipy'
  'python-evdev'
  'python-pyperclip'
  'python-requests'
  'python-psutil'
  'python-pyudev'
  'python-rich'
  'python-gobject'
  'python-pulsectl'
  'python-dbus'
  'ydotool'
  'wtype'
  'python-pywayland'
  'pipewire' 'pipewire-alsa' 'pipewire-pulse'
)
optdepends=('gtk4-layer-shell: for themed Mic-OSD visualization')

install=$pkgname.install

# Build from local git checkout
source=()
sha256sums=()

pkgver() {
  cd "$startdir"
  # Use the version from the latest tag, falling back to PKGBUILD default
  git describe --tags --abbrev=0 2>/dev/null | sed 's/^v//' || echo "$pkgver"
}

build() {
  # nothing to build; user runs 'hyprwhspr setup' post-install
  true
}

package() {
  cd "$startdir"

  # Payload into /usr/lib keeps repo layout intact
  install -d "$pkgdir/usr/lib/$pkgname"
  cp -r lib bin config share README.md LICENSE requirements.txt "$pkgdir/usr/lib/$pkgname"

  # Make sure main launcher is executable
  if [ -f "$pkgdir/usr/lib/$pkgname/bin/hyprwhspr" ]; then
    chmod 755 "$pkgdir/usr/lib/$pkgname/bin/hyprwhspr"
  fi

  # Install systemd user service
  install -Dm644 "config/systemd/hyprwhspr.service" \
    "$pkgdir/usr/lib/systemd/user/hyprwhspr.service"

  # Create wrapper that uses the repo's bin/hyprwhspr with fixed paths
  install -d "$pkgdir/usr/bin"
  sed 's|PACKAGE_ROOT="$(dirname "$SCRIPT_DIR")"|PACKAGE_ROOT="/usr/lib/hyprwhspr"|' \
    "bin/$pkgname" > "$pkgdir/usr/bin/$pkgname"
  chmod 755 "$pkgdir/usr/bin/$pkgname"

  # Docs & license
  install -d "$pkgdir/usr/share/doc/$pkgname" "$pkgdir/usr/share/licenses/$pkgname"
  install -m644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
  install -m644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
