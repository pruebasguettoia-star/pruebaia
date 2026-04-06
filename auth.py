"""
auth.py — Autenticación por token para endpoints sensibles.

Configura ADMIN_TOKEN en Railway (Settings → Variables).
Los endpoints protegidos requieren:
  - Header: Authorization: Bearer <token>
  - O query param: ?token=<token>
"""
import os
from functools import wraps
from flask import request, jsonify


def require_admin(f):
    """Decorator: protege un endpoint con ADMIN_TOKEN."""
    @wraps(f)
    def decorated(*args, **kwargs):
        ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

        if not ADMIN_TOKEN:
            # Si no hay token configurado, BLOQUEAR por seguridad
            return jsonify({"error": "ADMIN_TOKEN no configurado en Railway — endpoint bloqueado"}), 403

        # Aceptar token en header o query param
        token = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        if not token:
            token = request.args.get("token", "")

        if token != ADMIN_TOKEN:
            return jsonify({"error": "Token inválido o ausente"}), 401

        return f(*args, **kwargs)
    return decorated
