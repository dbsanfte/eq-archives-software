FROM node:22-bookworm-slim@sha256:48e4b67d85f87bd551df43704e24d252f56cc5f8e9718841aace50f19948f0f9 AS dependencies
WORKDIR /app
COPY package.json yarn.lock ./
RUN yarn install --frozen-lockfile --non-interactive

FROM dependencies AS build
COPY . .
RUN CI=true yarn test:ci --runInBand
RUN yarn build

FROM nginx:stable-alpine@sha256:ef8676b33d681f272ba429b27658bdd7e640963279714c96bddf1dc76307f7b6
COPY nginx.conf.template /etc/nginx/frontend.conf.template
COPY docker-entrypoint.sh /usr/local/bin/start-frontend
RUN chmod 755 /usr/local/bin/start-frontend
COPY --from=build /app/build /usr/share/nginx/html
ENTRYPOINT ["/usr/local/bin/start-frontend"]
