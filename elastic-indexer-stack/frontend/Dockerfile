FROM node:22-bookworm-slim@sha256:48e4b67d85f87bd551df43704e24d252f56cc5f8e9718841aace50f19948f0f9 AS dependencies
WORKDIR /app
COPY package.json yarn.lock ./
RUN yarn install --frozen-lockfile --non-interactive

FROM dependencies AS build
COPY . .
RUN CI=true yarn test:ci --runInBand
ARG GIT_SHA
RUN REACT_APP_GIT_SHA="$GIT_SHA" yarn build

FROM nginx:1.31.9-alpine@sha256:90b6bd2aec278b16b98221d64deb1d6d35d164567f507ab0716e6053878315aa
COPY nginx-main.conf /etc/nginx/frontend-main.conf
COPY security-headers.conf /etc/nginx/security-headers.conf
COPY nginx.conf.template /etc/nginx/frontend.conf.template
COPY docker-entrypoint.sh /usr/local/bin/start-frontend
RUN chmod 755 /usr/local/bin/start-frontend
COPY --from=build /app/build /usr/share/nginx/html
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/start-frontend"]
