#!/bin/sh
set -eu
umask 077
: "${SEARCH_API_URL:?Set SEARCH_API_URL to the bounded search service}"
ELASTICSEARCH_INDEX=${ELASTICSEARCH_INDEX:-eq-archive}
export ELASTICSEARCH_INDEX
# This static server receives no database/model credentials.
envsubst '$SEARCH_API_URL $ELASTICSEARCH_INDEX' \
  < /etc/nginx/frontend.conf.template > /tmp/frontend.conf
exec nginx -c /etc/nginx/frontend-main.conf -g 'daemon off;'
