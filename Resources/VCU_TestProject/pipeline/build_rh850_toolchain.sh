#!/usr/bin/env bash
# Build the free GCC cross compiler for Renesas RH850 (target v850-elf) into
# rh850-toolchain/ next to this file. Does nothing if it's already there, so
# pipelines can cache the folder. Takes about 5 minutes on 12 cores.
#
# Linux only. Needs a host compiler plus: libgmp-dev libmpfr-dev libmpc-dev
# texinfo bison flex curl xz-utils. The compiler needs libgmp, libmpfr and libmpc
# at run time too, so a cached copy still needs those packages installed.
set -euo pipefail

BINUTILS=2.44
GCC=14.2.0
PREFIX="$(cd "$(dirname "$0")" && pwd)/rh850-toolchain"

if [ -x "$PREFIX/bin/v850-elf-gcc" ]; then
  echo "RH850 toolchain already built: $("$PREFIX/bin/v850-elf-gcc" --version | head -1)"
  exit 0
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"
curl -fsSL --retry 3 "https://ftp.gnu.org/gnu/binutils/binutils-$BINUTILS.tar.xz" | tar xJ
curl -fsSL --retry 3 "https://ftp.gnu.org/gnu/gcc/gcc-$GCC/gcc-$GCC.tar.xz" | tar xJ

mkdir build-binutils
(cd build-binutils
 "../binutils-$BINUTILS/configure" --target=v850-elf --prefix="$PREFIX" \
   --disable-nls --disable-werror > /dev/null
 make -j"$(nproc)" > /dev/null
 make install-strip > /dev/null)

export PATH="$PREFIX/bin:$PATH"
mkdir build-gcc
(cd build-gcc
 "../gcc-$GCC/configure" --target=v850-elf --prefix="$PREFIX" --enable-languages=c \
   --without-headers --with-newlib --disable-nls --disable-shared --disable-threads \
   --disable-libssp --disable-libquadmath --disable-libgomp --disable-libatomic \
   --disable-decimal-float > /dev/null
 make -j"$(nproc)" all-gcc all-target-libgcc > /dev/null
 make install-strip-gcc install-target-libgcc > /dev/null)

rm -rf "$PREFIX/share"
"$PREFIX/bin/v850-elf-gcc" --version | head -1
