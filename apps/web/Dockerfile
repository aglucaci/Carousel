# CAROUSEL web app (Next.js). Build context: apps/web.
FROM node:22-slim AS build
WORKDIR /web
ENV NEXT_TELEMETRY_DISABLED=1
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build && npm prune --omit=dev

FROM node:22-slim
WORKDIR /web
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    CAROUSEL_API_URL=http://api:8000
COPY --from=build --chown=node /web/package.json /web/next.config.ts ./
COPY --from=build --chown=node /web/node_modules ./node_modules
COPY --from=build --chown=node /web/.next ./.next
USER node
EXPOSE 3000
CMD ["node_modules/.bin/next", "start", "--hostname", "0.0.0.0", "--port", "3000"]
