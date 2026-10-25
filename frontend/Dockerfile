# Frontend image for the tender matching system (PRD 14).
# One of two images: this is the React app, served as static files.
#
# VITE_API_URL is read by the client at build time, so it must be the address
# the *browser* uses to reach the backend, not the one the container sees.

FROM node:22-alpine AS build

WORKDIR /app

# Dependencies first so code edits do not invalidate the install layer.
COPY package.json package-lock.json ./
RUN npm ci

COPY index.html vite.config.ts tsconfig.json tsconfig.app.json tsconfig.node.json ./
COPY public ./public
COPY src ./src

ARG VITE_API_URL=http://localhost:8000
ENV VITE_API_URL=${VITE_API_URL}
RUN npm run build

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/healthz || exit 1
