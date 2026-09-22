#!/bin/sh
set -eu

: "${ELASTICSEARCH_URL:?Set ELASTICSEARCH_URL}"
: "${ELASTICSEARCH_INDEX:?Set ELASTICSEARCH_INDEX}"
: "${OPENAI_URL:?Set OPENAI_URL}"
for name in es_readonly_username es_readonly_password openai_api_key; do
  if [ ! -s "/run/secrets/$name" ]; then
    echo "Missing runtime secret file: $name" >&2
    exit 1
  fi
done
username=$(cat /run/secrets/es_readonly_username)
password=$(cat /run/secrets/es_readonly_password)
ELASTICSEARCH_AUTHORIZATION=$(printf '%s:%s' "$username" "$password" | base64 | tr -d '\n')
OPENAI_API_KEY=$(cat /run/secrets/openai_api_key)
export ELASTICSEARCH_AUTHORIZATION OPENAI_API_KEY
unset username password
envsubst '$ELASTICSEARCH_URL $ELASTICSEARCH_AUTHORIZATION $ELASTICSEARCH_INDEX $OPENAI_URL $OPENAI_API_KEY' \
  < /etc/nginx/frontend.conf.template > /etc/nginx/conf.d/default.conf
exec nginx -g 'daemon off;'
