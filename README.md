# Bulwark

Pre-Deployment Production Simulation Agent — autonomously evaluates every merge request against real production traffic before code reaches production.

## Quick Start

```bash
# Copy environment template and fill in your secrets
cp .env.example .env

# Start the development stack
docker-compose up

# Run tests
pytest tests/ -v
```

## License

MIT
