"""Local BLE protocol for ResMed Air-family CPAPs (AirSense/AirCurve 11). No Home Assistant deps."""
from .client import AirSenseClient, AirSenseError, AuthError, RpcError
from .protocol import (
    FG_STATE_BY_CODE,
    FG_STATE_NAMES,
    FG_THERAPY_STATES,
    NAME_PREFIX,
    SERVICE_UUID,
    Credentials,
    normalize_fg_state,
)
from .srp import SrpError

__all__ = [
    "AirSenseClient",
    "AirSenseError",
    "AuthError",
    "RpcError",
    "Credentials",
    "FG_STATE_BY_CODE",
    "FG_STATE_NAMES",
    "FG_THERAPY_STATES",
    "NAME_PREFIX",
    "SERVICE_UUID",
    "SrpError",
    "normalize_fg_state",
]
