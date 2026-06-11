#!/bin/bash
# Start the demo interceptor script
cd "$(dirname "$0")/.."

# Run the interceptor using the local virtual environment
.venv/bin/python mock_server/demo_interceptor.py
