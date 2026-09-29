# Build context: ./frontend
FROM node:24-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
COPY packages packages
COPY apps/admin/package.json apps/admin/
COPY apps/terminal/package.json apps/terminal/
RUN npm ci
COPY apps/terminal apps/terminal
RUN npm run build -w apps/terminal

FROM nginx:1.29-alpine
COPY apps/terminal/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/apps/terminal/dist /usr/share/nginx/html
