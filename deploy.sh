#!/bin/bash

set -e  # Stop script if any command fails

echo "📥 Pulling latest code..."
git pull

echo "🛑 Stopping existing containers..."
docker compose down -v --remove-orphans

echo "🔨 Rebuilding and starting containers..."
docker compose up -d --build

echo "✅ Deployment completed successfully!"
