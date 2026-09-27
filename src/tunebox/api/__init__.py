"""The HTTP service (PLAN.md Phase 2). LAN-only, token-authenticated, thin over the use cases."""

from .app import create_app

__all__ = ["create_app"]
