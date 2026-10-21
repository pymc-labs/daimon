#!/bin/sh
set -eu
scratch=/tmp/l15-first-turn.4QeXQP
mkdir -p "$scratch/curl/lib"
cp /usr/bin/curl "$scratch/curl/"
ldd /usr/bin/curl | awk '/=> \// {print $3}' | while IFS= read -r lib; do cp "$lib" "$scratch/curl/lib/"; done
cp /lib64/ld-linux-x86-64.so.2 "$scratch/curl/"
tar -C "$scratch" -cf "$scratch/curl.tar" curl
