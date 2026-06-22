"""magi host-level package — the shell's own code (settings DB, version).

Named `host` (not `core`) on purpose: betelgeuse's package uses `core`, and the
betelgeuse loader puts its dir on sys.path, so a host `core` would collide. Keep
host modules under this name.
"""
