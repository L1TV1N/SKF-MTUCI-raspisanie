#!/usr/bin/env bash
# выкладка на сервер по ssh-ключу: deploy/deploy.sh <ip-сервера> [домен]
set -euo pipefail
HOST="${1:?укажи ip сервера}"
DOMAIN="${2:-litvinskiy-dev.online}"
KEY="${RASPISAN_SSH_KEY:-$HOME/.ssh/raspisan_deploy}"
SSH=(ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes root@"$HOST")

cd "$(dirname "$0")/.."
# код заливаем целиком заново, база на сервере лежит отдельно в /var/lib/raspisan и не трогается
tar czf - --exclude=./data --exclude=./.git --exclude=./.claude --exclude='__pycache__' . \
  | "${SSH[@]}" 'rm -rf /opt/raspisan.new && mkdir -p /opt/raspisan.new && tar xzf - -C /opt/raspisan.new \
                 && rm -rf /opt/raspisan && mv /opt/raspisan.new /opt/raspisan'
"${SSH[@]}" "bash /opt/raspisan/deploy/setup.sh $DOMAIN"
