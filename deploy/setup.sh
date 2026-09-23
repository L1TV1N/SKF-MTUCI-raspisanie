#!/usr/bin/env bash
# настройка сервера (debian/ubuntu), запускается от root уже на сервере:
#   bash /opt/raspisan/deploy/setup.sh litvinskiy-dev.online
set -euo pipefail
DOMAIN="${1:?укажи домен}"
APP=/opt/raspisan

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 nginx curl >/dev/null

id raspisan &>/dev/null || useradd --system --home-dir /var/lib/raspisan --shell /usr/sbin/nologin raspisan
install -d -o raspisan -g raspisan -m 750 /var/lib/raspisan

# пароль админки создаётся один раз и больше не трогается
if [ ! -f /etc/raspisan.env ]; then
  PW=$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')
  (umask 077; printf 'RASPISAN_ADMIN_PASSWORD=%s\n' "$PW" > /etc/raspisan.env)
  echo ">>> пароль админки: $PW (хранится в /etc/raspisan.env)"
fi

install -m 644 "$APP/deploy/raspisan.service" /etc/systemd/system/raspisan.service
systemctl daemon-reload
systemctl enable raspisan >/dev/null
systemctl restart raspisan

# nginx: если certbot уже дописал https в конфиг, не перетираем его
if ! grep -q "managed by Certbot" /etc/nginx/sites-available/raspisan 2>/dev/null; then
  sed "s/__DOMAIN__/$DOMAIN/g" "$APP/deploy/nginx.conf" > /etc/nginx/sites-available/raspisan
fi
ln -sf /etc/nginx/sites-available/raspisan /etc/nginx/sites-enabled/raspisan
rm -f /etc/nginx/sites-enabled/default  # приветственная страница nginx больше не нужна
nginx -t -q
systemctl reload nginx

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 'Nginx Full' >/dev/null
fi

sleep 1
systemctl is-active --quiet raspisan && echo ">>> сервис работает: http://$DOMAIN"
curl -fsS -o /dev/null -w ">>> проверка через nginx: %{http_code}\n" -H "Host: $DOMAIN" http://127.0.0.1/api/meta
