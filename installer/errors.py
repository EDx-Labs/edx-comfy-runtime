"""Erros compartilhados pelas fronteiras do runtime."""

from __future__ import annotations


class ProvisioningError(RuntimeError):
    """Falha operacional ou de contrato durante o provisionamento."""
