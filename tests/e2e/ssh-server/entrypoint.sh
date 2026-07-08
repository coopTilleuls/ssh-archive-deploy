#!/bin/sh
set -eu

mkdir -p /srv/project /srv/deploy /home/deploy/.ssh
cp -a /fixture/. /srv/project/
cp /authorized_keys /home/deploy/.ssh/authorized_keys

chown -R deploy:deploy /srv/project /srv/deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys

exec /usr/sbin/sshd -D -e \
  -o PasswordAuthentication=no \
  -o PermitRootLogin=no \
  -o PubkeyAuthentication=yes \
  -o AuthorizedKeysFile=.ssh/authorized_keys
