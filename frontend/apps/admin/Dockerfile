# Build context: ./frontend
FROM node:24-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
COPY packages packages
COPY apps/admin/package.json apps/admin/
COPY apps/terminal/package.json apps/terminal/
RUN npm ci
COPY apps/admin apps/admin
RUN npm run build -w apps/admin

FROM nginx:1.29-alpine
COPY apps/admin/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/apps/admin/dist /usr/share/nginx/html
