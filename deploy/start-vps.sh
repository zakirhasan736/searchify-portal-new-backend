#!/usr/bin/env bash
set -euo pipefail
cd /var/www/searchify

if [ ! -d backend ] || [ ! -d frontend ]; then
  echo "Expected /var/www/searchify/backend and /var/www/searchify/frontend"
  exit 1
fi

if [ ! -f docker-compose.yml ]; then
  if [ -f backend/deploy/vps-docker-compose.yml ]; then
    cp backend/deploy/vps-docker-compose.yml docker-compose.yml
  else
    echo "Missing docker-compose.yml. Copy backend/deploy/vps-docker-compose.yml here."
    exit 1
  fi
fi

if [ ! -f backend/.env ]; then
  echo "Create backend/.env first (copy from .env.example and set secrets + public URLs)."
  exit 1
fi

export PUBLIC_SITE_URL="${PUBLIC_SITE_URL:-https://searchify.nextcreavo.com}"
export CORS_ORIGINS="${CORS_ORIGINS:-https://searchify.nextcreavo.com}"

docker compose up -d --build
docker compose ps

NGINX_SRC=backend/deploy/nginx
if [ -d "$NGINX_SRC" ]; then
  cp "$NGINX_SRC/searchify.nextcreavo.com.conf" /etc/nginx/sites-available/searchify.nextcreavo.com.conf
  cp "$NGINX_SRC/searchify-api.nextcreavo.com.conf" /etc/nginx/sites-available/searchify-api.nextcreavo.com.conf
  ln -sfn /etc/nginx/sites-available/searchify.nextcreavo.com.conf /etc/nginx/sites-enabled/searchify.nextcreavo.com.conf
  ln -sfn /etc/nginx/sites-available/searchify-api.nextcreavo.com.conf /etc/nginx/sites-enabled/searchify-api.nextcreavo.com.conf
  nginx -t && systemctl reload nginx
  echo "Nginx reloaded. After DNS works, run:"
  echo "certbot --nginx -d searchify.nextcreavo.com -d searchify-api.nextcreavo.com"
fi

echo
echo "Site:  $PUBLIC_SITE_URL"
echo "API:   https://searchify-api.nextcreavo.com/api/v1/health"
