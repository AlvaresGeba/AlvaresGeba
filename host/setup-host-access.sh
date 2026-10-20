#!/usr/bin/env bash
# Gives the executor container SSH access to this host, as the account
# geba-exec. Run once on the host, from the project directory:
#
#   sudo host/setup-host-access.sh
#
# What it does:
#   1. creates the system account geba-exec (no password; can read the
#      journal, cannot use docker or sudo beyond host/sudoers.geba-exec)
#   2. makes a key pair in ssh/ (mounted read-only into the executor)
#   3. lets that key in for geba-exec only from docker networks
#      (172.16.0.0/12), with no forwarding and no pty
#   4. installs geba-root and the sudoers rules
#   5. writes ssh/known_hosts and ssh/config for the container
#
# Safe to run again. To undo:
#   sudo userdel -r geba-exec; sudo rm -r /etc/sudoers.d/geba-exec /usr/local/lib/geba
#   rm -r ssh/
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "run it with sudo" >&2; exit 1; }

PROJECT=$(cd "$(dirname "$0")/.." && pwd)
OWNER=${SUDO_USER:-root}
OWNER_GROUP=$(id -gn "$OWNER")
ACCOUNT=geba-exec
SSH_DIR=$PROJECT/ssh

echo "== account $ACCOUNT"
if ! id "$ACCOUNT" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir /var/lib/$ACCOUNT --shell /bin/sh "$ACCOUNT"
fi
# '*' = no password, but not "locked": sshd still accepts the key.
usermod -p '*' -aG systemd-journal,adm "$ACCOUNT"
HOME_DIR=$(getent passwd "$ACCOUNT" | cut -d: -f6)

echo "== key pair in $SSH_DIR"
install -d -m 700 -o "$OWNER" -g "$OWNER_GROUP" "$SSH_DIR"
if [ ! -f "$SSH_DIR/id_ed25519" ]; then
    sudo -u "$OWNER" ssh-keygen -q -t ed25519 -N '' -C geba-executor -f "$SSH_DIR/id_ed25519"
fi

echo "== authorized_keys"
install -d -m 700 -o "$ACCOUNT" -g "$ACCOUNT" "$HOME_DIR/.ssh"
echo "restrict,from=\"172.16.0.0/12\" $(cat "$SSH_DIR/id_ed25519.pub")" > "$HOME_DIR/.ssh/authorized_keys"
chown "$ACCOUNT:$ACCOUNT" "$HOME_DIR/.ssh/authorized_keys"
chmod 600 "$HOME_DIR/.ssh/authorized_keys"

echo "== geba-root and sudoers"
install -D -m 755 -o root -g root "$PROJECT/host/geba-root" /usr/local/lib/geba/geba-root
visudo -cqf "$PROJECT/host/sudoers.geba-exec"
install -m 440 -o root -g root "$PROJECT/host/sudoers.geba-exec" /etc/sudoers.d/geba-exec

echo "== ssh config for the container"
ssh-keyscan -t ed25519 127.0.0.1 2>/dev/null \
    | sed 's/^127\.0\.0\.1 /host.docker.internal /' > "$SSH_DIR/known_hosts"
[ -s "$SSH_DIR/known_hosts" ] || { echo "ssh-keyscan failed - is sshd running?" >&2; exit 1; }
cat > "$SSH_DIR/config" <<EOF
Host host.docker.internal
    User $ACCOUNT
    IdentityFile /app/ssh/id_ed25519
    IdentitiesOnly yes
    UserKnownHostsFile /app/ssh/known_hosts
    StrictHostKeyChecking yes
EOF
chown "$OWNER:$OWNER_GROUP" "$SSH_DIR/known_hosts" "$SSH_DIR/config"
chmod 644 "$SSH_DIR/known_hosts"
chmod 600 "$SSH_DIR/config"

echo
echo "Done. Now: docker compose up -d --build"
