FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# Inlined into the browser bundle: the browser (not the container) calls the API.
ARG NEXT_PUBLIC_AEOS_API=http://localhost:8800
ENV NEXT_PUBLIC_AEOS_API=$NEXT_PUBLIC_AEOS_API NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
COPY --from=build /app/package.json /app/package-lock.json ./
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/.next ./.next
COPY --from=build /app/next.config.mjs ./
EXPOSE 3100
CMD ["npx", "next", "start", "-p", "3100"]
