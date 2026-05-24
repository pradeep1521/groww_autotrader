#!/usr/bin/env bash
# Run the AutoTrader app
cd "$(dirname "$0")"
python3 -m streamlit run app.py --server.port 8502 --server.headless false
