FROM node:22-bookworm-slim
WORKDIR /app
COPY --chown=node:node package.json server.mjs /app/
COPY --chown=node:node public /app/public
USER node
EXPOSE 5000
CMD ["node", "server.mjs"]
