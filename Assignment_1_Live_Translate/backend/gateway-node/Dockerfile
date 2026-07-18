# Build context is the repo root (Assignment_1_Live_Translate/), not this
# directory — server.js resolves the widget file via
# path.join(__dirname, "..", "..", "widget", "translation-widget.js"), so the
# image needs that same two-levels-up layout, which means this Dockerfile
# needs the sibling widget/ folder in its build context.
# Deploy with:
#   cd Assignment_1_Live_Translate
#   flyctl deploy --config backend/gateway-node/fly.toml \
#                  --dockerfile backend/gateway-node/Dockerfile \
#                  -a live-translate-gateway-pananthg
FROM node:20-slim

WORKDIR /app

COPY backend/gateway-node/package.json backend/gateway-node/package-lock.json ./backend/gateway-node/
RUN cd backend/gateway-node && npm ci --omit=dev

COPY backend/gateway-node/ ./backend/gateway-node/
COPY widget/ ./widget/

WORKDIR /app/backend/gateway-node

EXPOSE 8787

CMD ["node", "server.js"]
