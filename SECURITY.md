# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Scorched, please report it responsibly:

**Email:** Open a private GitHub Security Advisory at [github.com/willcassell/scorched/security/advisories](https://github.com/willcassell/scorched/security/advisories)

Do **not** open a public issue for security vulnerabilities.

I'll acknowledge receipt within 48 hours and aim to provide a fix or mitigation within 7 days for critical issues.

## Security Model

Scorched is designed as a **single-user, self-hosted trading tool**. The security model assumes:

- The system runs on infrastructure you control (your machine or your VM)
- Network access is restricted to you (VPN, firewall, or localhost)
- A shared-secret PIN (`SETTINGS_PIN`) gates all mutation operations

This is **not** designed for multi-tenant, public-facing, or shared-server deployment.

## What's Protected

When `SETTINGS_PIN` is configured:

| Surface | Protection |
|---------|-----------|
| REST mutation endpoints (POST/PUT) | `X-Owner-Pin` header required |
| MCP mutation tools (`confirm_trade`, `reject_recommendation`, `get_recommendations`) | `pin` parameter required |
| Dashboard strategy editor | PIN required to save |
| Read-only endpoints and tools | No PIN required |

## Recommended Deployment

1. **Set `SETTINGS_PIN`** in your `.env` file
2. **Do not expose port 8000 to the public internet**
3. Use one of:
   - Cloud firewall with IP allowlist (`ufw allow from YOUR_IP to any port 8000`)
   - Tailscale or WireGuard VPN
   - Reverse proxy (nginx/Caddy) with authentication
4. Keep API keys in `.env` only — never commit them to the repo
5. The `.env` file is excluded from rsync deploys by default

## Known Limitations

- No rate limiting on API endpoints (mitigated by network restriction)
- No session-based authentication or user accounts
- Database credentials are Docker-internal defaults (not exposed externally)
- The system does not encrypt data at rest in PostgreSQL

For the intended single-user, self-hosted use case, these are acceptable trade-offs. If your deployment requirements differ, evaluate whether additional controls are needed.
