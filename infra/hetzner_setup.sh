#!/bin/bash
# hetzner_setup.sh — One-command VPS bootstrap for Hetzner CX21/CPX21
# Run as root on fresh Ubuntu 24.04:
#   curl -fsSL https://your-repo/infra/hetzner_setup.sh | bash

set -euo pipefail

echo "=== AI Ecommerce System V4.0 — Hetzner Setup ==="

# 1. System update
apt-get update && apt-get upgrade -y
apt-get install -y curl git docker.io docker-compose python3.11 python3.11-venv \
                   python3-pip nginx certbot python3-certbot-nginx ufw

# 2. Docker
systemctl enable docker && systemctl start docker
usermod -aG docker $USER 2>/dev/null || true

# 3. Firewall
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 5678/tcp  # n8n (restrict to your IP in production)
ufw allow 3000/tcp  # Metabase (restrict to your IP)
ufw --force enable

# 4. Clone repo (update URL)
echo "Clone your repo to /opt/ecommerce-ai-v4"
# git clone https://github.com/YOUR_ORG/ecommerce-ai-v4.git /opt/ecommerce-ai-v4

# 5. Environment
echo "Copy and fill your .env file:"
echo "  cp /opt/ecommerce-ai-v4/infra/.env.example /opt/ecommerce-ai-v4/.env"
echo "  nano /opt/ecommerce-ai-v4/.env"

# 6. Start stack
# cd /opt/ecommerce-ai-v4 && docker-compose -f infra/docker-compose.yml up -d

# 7. Run DB migrations
# docker-compose exec api python -c "from shared.supabase_client import SupabaseClient; SupabaseClient().run_migrations()"

echo ""
echo "=== SETUP COMPLETE ==="
echo "Next steps:"
echo "  1. Fill /opt/ecommerce-ai-v4/.env with all API keys"
echo "  2. docker-compose -f infra/docker-compose.yml up -d"
echo "  3. Import n8n workflows at http://YOUR_IP:5678"
echo "  4. python scripts/test_pipeline_v4.py"
echo "  5. Configure Slack app at https://api.slack.com/apps"
echo ""
echo "n8n:      http://YOUR_IP:5678  (admin / n8n_change_me)"
echo "API:      http://YOUR_IP:8000/docs"
echo "Metabase: http://YOUR_IP:3000"
