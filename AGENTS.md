# Project Guidelines

## Architecture
- The FastAPI backend and host sampling logic are in `app/main.py`. It serves two views: a single-server status view (`app/static/index.html`) and a multi-server aggregator dashboard (`app/static/dashboard.html`). There is no frontend build step.
- Keep the `/api/stats` response shape consistent with the dashboard, and handle unavailable hardware or sensor data gracefully.
- The Compose files intentionally differ for NVIDIA GPU support. Preserve the differences and document configuration or deployment changes in `README.md`.

## Development and verification
- No standalone test or lint suite is configured. Don’t assume one exists.
- Validate Compose changes with `docker compose config`; for NVIDIA, use `docker compose -f docker-compose.nvidia.yml config`.
- When practical, build the affected deployment with `docker compose build` (or the NVIDIA Compose file).

## Host access and security
- Host networking, the host PID namespace, `SYS_PTRACE`, and the read-only host filesystem mount enable host monitoring. Understand their security and behavior implications before changing them.
- The dashboard has no authentication and exposes host details. Do not expand host access or introduce host writes or commands driven by request input; document security-relevant changes in `README.md`.
