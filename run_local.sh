#!/usr/bin/env bash
# Start the app locally on port 8600 (8501 is Streamlit's default and may be taken).
cd "$(dirname "$0")" && source .venv/bin/activate && exec streamlit run app.py --server.port "${PORT:-8600}"
