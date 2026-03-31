#!/bin/bash
# sync_and_push.sh

SOURCE_DIR="/srv/aidev/flashquest/ai_team/"
VM_SHARE_DIR="/home/addohm/VMs/vm_share/"
DEST_DIR="$(pwd)"

if [ ! -d "$SOURCE_DIR" ]; then
    echo "Error: Source directory $SOURCE_DIR does not exist."
    exit 1
fi

echo "Syncing changes from $SOURCE_DIR to $DEST_DIR..."

# Use rsync to sync files.
# --filter=':- .gitignore' tells rsync to use the rules in .gitignore
# -a (archive mode) preserves permissions/timestamps, -v is verbose
rsync -av --filter=':- .gitignore' "$SOURCE_DIR" "$DEST_DIR"

echo "Syncing changes from $DEST_DIR to $VM_SHARE_DIR..."

rsync "$DEST_DIR" "$VM_SHARE_DIR"

# Git operations
if [[ -n $(git status -s) ]]; then
    echo "Changes detected. Committing and pushing..."
    git add .
    git commit -m "Auto-sync from /srv/aidev/flashquest/ai_team - $(date '+%Y-%m-%d %H:%M:%S')"
    git push origin main
else
    echo "No changes detected. Nothing to push."
fi
