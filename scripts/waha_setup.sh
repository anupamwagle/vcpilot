#!/bin/bash
# Start the WAHA default session and also configure the webhook
API=http://localhost:3000
KEY=vcpilot_2026

echo "=== Starting default session ==="
curl -s -X POST "$API/api/sessions/start" \
  -H "X-Api-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"default"}' | python3 -m json.tool

sleep 3

echo ""
echo "=== Session status ==="
curl -s "$API/api/sessions/default" \
  -H "X-Api-Key: $KEY" | python3 -m json.tool

echo ""
echo "=== Configuring webhook ==="
curl -s -X PUT "$API/api/sessions/default" \
  -H "X-Api-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"webhooks":[{"url":"http://api:8501/webhook/whatsapp","events":["message","session.status"]}]}' | python3 -m json.tool
