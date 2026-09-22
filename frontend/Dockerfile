# ──────────────────────────────────────────────────────────────────────────────
# Aetheris Health AI — Frontend Dockerfile
#
# Development image that runs the Vite dev server with HMR.
# Bind-mount the source directory so edits reflect instantly.
# ──────────────────────────────────────────────────────────────────────────────

FROM node:20-alpine

WORKDIR /app

# Install dependencies first (leverages Docker layer caching)
COPY package.json package-lock.json ./
RUN npm ci

EXPOSE 5173

# Run the Vite dev server; --host exposes it beyond localhost
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
