"""ADE adaptors. A new ADE is one module implementing `Adaptor` plus one entry in ADAPTORS."""

from __future__ import annotations

from .base import Adaptor, LaunchPlan, LaunchRequest
from .hc_adaptor import HcAdaptor
from .native_adaptor import NativeAdaptor
from .orca_adaptor import OrcaAdaptor

ADAPTORS: dict[str, Adaptor] = {a.name: a for a in (HcAdaptor(), OrcaAdaptor(), NativeAdaptor())}
# init asks each ADE in this order whether the Router runs on it.
ROUTER_ORDER = ("hc", "orca")


def get_adaptor(name: str | None) -> Adaptor:
    if name not in ADAPTORS:
        raise ValueError(f"unknown ADE '{name}'; known: {', '.join(ADAPTORS)}")
    return ADAPTORS[name]


__all__ = ["ADAPTORS", "ROUTER_ORDER", "Adaptor", "LaunchPlan", "LaunchRequest", "get_adaptor"]
