# Copy this to deploy/config.sh and fill in your Mac mini's details.
#   cp deploy/config.example.sh deploy/config.sh
# deploy/config.sh is git-ignored and is NOT rsynced to the mini.

MINI_USER="kai"                              # SSH username on the Mac mini
MINI_HOST="mac-mini.local"                   # Bonjour name (or IP) of the Mac mini
REMOTE_DIR="/Users/${MINI_USER}/magi"        # where the unified app lives on the mini
PORT=8080                                    # the magi web port (LAN)
MAGI_ENV="prod"                              # mode the mini's LaunchAgents serve in (dev|prod);
                                             # baked into the plists by setup-mini.sh. Also the
                                             # default --env for deploy.sh (override with --env).
