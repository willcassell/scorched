# Testing Environment

Isolated setup for testing the onboarding wizard without affecting production.

## What's isolated

| File | Production | Testing |
|------|-----------|---------|
| `.env` | `tradebot/.env` | `testing/.env` |
| `strategy.json` | `tradebot/strategy.json` | `testing/strategy.json` |
| Database | `postgres_data` volume | `postgres_test_data` volume |
| Port | `8000` | `8001` |

The onboarding wizard's "Save Configuration" button writes to the **testing** copies only.

## Usage

```bash
cd testing

# Start the test environment
docker compose up -d --build

# Open the onboarding wizard
open http://localhost:8001/onboarding

# Watch logs
docker compose logs tradebot-test -f

# Stop (keeps test DB data)
docker compose down

# Stop and wipe everything (clean slate)
docker compose down -v
```

## After testing

Your production `.env` and `strategy.json` in the parent directory are untouched.
To clean up completely:

```bash
docker compose down -v
cd ..
rm -rf testing/
```
