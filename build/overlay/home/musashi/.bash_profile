# Start the Phosh (mobile-style) session automatically on the first virtual terminal.
if [ -z "$WAYLAND_DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    exec /usr/bin/phosh-session
fi
