FROM mcr.microsoft.com/playwright:v1.61.1-noble@sha256:5b8f294aff9041b7191c34a4bab3ac270157a28774d4b0660e9743297b697e48

ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    NODE_ENV=production

WORKDIR /opt/astra-browser-smoke
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force

COPY subscription_browser_smoke.mjs browser_launch_selftest.mjs browser_assertions.mjs ./

LABEL ai.reeftotem.astra.browser-smoke-schema="2"

USER pwuser
ENTRYPOINT ["node", "/opt/astra-browser-smoke/subscription_browser_smoke.mjs"]
