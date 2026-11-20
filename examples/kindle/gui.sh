#!/bin/sh
# gui.sh freeze|thaw|list: pause or resume the Kindle's own interface (library, status bar, framework,
# window manager) so it cannot draw over the weather. Wi-Fi, power and the display server keep running.
JOBS="framework pillow statusbar webreader kppmainapp kfxreader kfxview"
roots() {
  for j in $JOBS; do status $j 2>/dev/null | sed -n 's/.*process \([0-9]*\).*/\1/p'; done
  pidof awesome
}
tree() {   # each root and all its descendants
  for d in /proc/[0-9]*; do awk '/^PPid/ { print FILENAME, $2 }' $d/status 2>/dev/null; done |
    sed 's#/proc/\([0-9]*\)/status#\1#' | awk -v r="$(roots | tr '\n' ' ')" '
    { kids[$2] = kids[$2] " " $1 }
    END { n = split(r, q, " "); for (i = 1; i <= n; i++) { p = q[i]; if (p == "" || seen[p]++) continue; print p
          m = split(kids[p], k, " "); for (j = 1; j <= m; j++) q[++n] = k[j] } }'
}
case "$1" in
  freeze) for p in $(tree); do kill -STOP $p; done ;;
  thaw)   for p in $(tree); do kill -CONT $p; done ;;
  *)      for p in $(tree); do printf '%s ' $p; cat /proc/$p/comm; done ;;
esac
