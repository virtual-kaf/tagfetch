#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 /absolute/path/to/application-python" >&2
    exit 2
fi

python_bin=$1
if [[ $python_bin != /* || ! -x $python_bin ]]; then
    echo "application Python must be an absolute executable path: $python_bin" >&2
    exit 2
fi

if [[ ! -r /etc/os-release ]]; then
    echo "cannot identify the operating system: /etc/os-release is missing" >&2
    exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
if [[ ${ID:-} != "alinux" || ${VERSION_ID:-} != 3* ]]; then
    echo "this installer supports Alibaba Cloud Linux 3 only" >&2
    exit 1
fi

if ! command -v dnf >/dev/null 2>&1; then
    echo "dnf is required on Alibaba Cloud Linux 3" >&2
    exit 1
fi

if [[ $EUID -eq 0 ]]; then
    privilege=()
elif command -v sudo >/dev/null 2>&1; then
    privilege=(sudo)
else
    echo "run as root or install sudo before provisioning renderer dependencies" >&2
    exit 1
fi

"$python_bin" -c \
    'import sys; assert sys.version_info >= (3, 10), "application Python 3.10+ is required"'

"${privilege[@]}" dnf install -y \
    fontconfig \
    cairo cairo-devel cairo-gobject cairo-gobject-devel \
    pango pango-devel \
    gobject-introspection gobject-introspection-devel \
    glib2-devel libffi-devel pkgconf-pkg-config gcc \
    google-noto-sans-fonts \
    google-noto-sans-devanagari-fonts \
    google-noto-sans-gujarati-fonts \
    google-noto-sans-tibetan-fonts \
    google-noto-sans-georgian-fonts \
    google-noto-sans-symbols-fonts \
    stix-math-fonts

"$python_bin" -m pip install --no-cache-dir "pycairo==1.29.0"
"$python_bin" -m pip install --no-cache-dir "PyGObject==3.44.2"

"${privilege[@]}" fc-cache -f

pkg-config --modversion cairo >/dev/null
pkg-config --modversion pango >/dev/null
pkg-config --modversion pangocairo >/dev/null

if ! rpm -ql pango | grep -q '/Pango-1\.0\.typelib$'; then
    echo "Pango-1.0.typelib was not installed by the pango RPM" >&2
    exit 1
fi
if ! rpm -ql pango | grep -q '/PangoCairo-1\.0\.typelib$'; then
    echo "PangoCairo-1.0.typelib was not installed by the pango RPM" >&2
    exit 1
fi

declare -A required_charsets=(
    [Devanagari]=0905
    [Gujarati]=0A85
    [Tibetan]=0F40
    [Georgian]=10D0
    [Math]=2211
)
for script_name in "${!required_charsets[@]}"; do
    charset=${required_charsets[$script_name]}
    if ! fc-list ":charset=$charset" file | grep -q .; then
        echo "fontconfig has no $script_name coverage for U+$charset" >&2
        exit 1
    fi
done

"$python_bin" -c \
    'import cairo, gi; gi.require_version("Pango", "1.0"); gi.require_version("PangoCairo", "1.0"); from gi.repository import Pango, PangoCairo; assert cairo.cairo_version() >= 11510; assert Pango.SCALE > 0; assert PangoCairo.FontMap.get_default() is not None'

echo "Alibaba Cloud Linux 3 renderer dependencies are ready."
