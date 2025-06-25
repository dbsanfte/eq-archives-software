# Stage 1: Build the React app
FROM node:22 AS build

# Set working directory
WORKDIR /app

# Copy package manifests first for better layer caching
COPY package.json yarn.lock ./

# Install dependencies
RUN yarn install --frozen-lockfile

# Copy source files (test files excluded via .dockerignore)
COPY . .

# Run tests with coverage and fail if below 90%
RUN yarn test --coverage --watchAll=false

# Build the React application
RUN yarn build

# Stage 2: Serve the app with NGINX
FROM nginx:stable

# Install envsubst to allow for environment variable substitution
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y gettext-base && \
    apt-get autoremove -y && \
    apt-get clean

# Copy in the NGINX configuration file
COPY nginx.conf.template /etc/nginx/conf.d/nginx.conf.template

# Copy the React build output to NGINX html directory
COPY --from=build /app/build /usr/share/nginx/html

# By default, NGINX in this container will start by running nginx -g daemon off;
CMD ["sh", "-c", \
    "\
        ELASTICSEARCH_USERNAME=$(cat /run/secrets/es_readonly_username); \
        ELASTICSEARCH_PASSWORD=$(cat /run/secrets/es_readonly_password); \
        export ELASTICSEARCH_AUTHORIZATION=$(echo -n \"$ELASTICSEARCH_USERNAME:$ELASTICSEARCH_PASSWORD\" | base64); \
        export OPENAI_API_KEY=$(cat /run/secrets/openai_api_key); \
        envsubst '$ELASTICSEARCH_URL $ELASTICSEARCH_AUTHORIZATION $ELASTICSEARCH_INDEX $OPENAI_URL $OPENAI_API_KEY' < /etc/nginx/conf.d/nginx.conf.template > /etc/nginx/conf.d/default.conf; \
        nginx -g 'daemon off;' \
    "\
]