# Copy this to deploy/config.sh and fill in your Mac mini's details.
#   cp deploy/config.example.sh deploy/config.sh
# deploy/config.sh is git-ignored and is NOT rsynced to the mini.

MINI_USER="kai"                                  # SSH username on the Mac mini
MINI_HOST="mac-mini.local"                       # Bonjour name (or IP) of the Mac mini
REMOTE_DIR="/Users/${MINI_USER}/betelgeuse"      # where the app lives on the mini
PORT=8000
