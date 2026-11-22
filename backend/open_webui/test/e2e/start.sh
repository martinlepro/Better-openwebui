#!/bin/bash
cd /home/claude/open-webui-main/backend
export DATA_DIR=/tmp/owui-data WEBUI_SECRET_KEY=e2e-secret OPENAI_API_BASE_URLS=http://127.0.0.1:9911/v1 OPENAI_API_KEYS=sk-fake ENABLE_OLLAMA_API=false ENABLE_LOCAL_AGENT=true LOCAL_AGENT_ALLOWED_ROOTS=/tmp/agent-ws CHAT_QUEUE_POLL_INTERVAL=1 CHAT_QUEUE_LIMIT_MARGIN=0 ENABLE_SIGNUP=true WEBUI_AUTH=true OFFLINE_MODE=true ENABLE_OPENAI_API=true SCARF_NO_ANALYTICS=true DO_NOT_TRACK=true ANONYMIZED_TELEMETRY=false USE_SLIM_DOCKER=true VECTOR_DB=pgvector
exec /tmp/owui-venv/bin/python -m uvicorn open_webui.main:app --host 127.0.0.1 --port 8099
