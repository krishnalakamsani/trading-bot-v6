FROM node:20-alpine AS builder

WORKDIR /app

# Context is repo root
COPY frontend/package.json .
RUN yarn install --ignore-engines

COPY frontend/ .

ARG REACT_APP_BACKEND_URL
ENV REACT_APP_BACKEND_URL=$REACT_APP_BACKEND_URL
ARG REACT_APP_WS_TOKEN
ENV REACT_APP_WS_TOKEN=$REACT_APP_WS_TOKEN
RUN yarn build

FROM nginx:alpine

COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/build /usr/share/nginx/html

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
