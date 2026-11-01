---
name: api_troubleshooting
description: Diagnose Clash Royale API key / IP allow-list failures
version: 1
tags: [api, ip, proxy, 403, token, key]
---
## Procedure
1. Detect egress (direct and active trusted proxy): IPv4/IPv6, latency.
2. Compare with the approved IP. Mismatch → requests are blocked by design.
3. Test `/cards` with the resolved key; classify: invalidIp, accessDenied, notFound, throttled, maintenance.
4. invalidIp → user must add the observed egress IP at developer.clashroyale.com (or use a stable trusted proxy).
5. On GitHub-hosted runners without a proxy, explain that the IP is dynamic.

## Pitfalls
- A developer key is not a player password. Never ask for game credentials.
- Never send the Authorization header through an unverified proxy.
