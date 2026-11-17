"""Persistent authorization incidents scoped to the failed operation/resource."""
import hashlib
import uuid


def incident_slot(scope):
    return "authorization_incident:" + hashlib.sha256(scope.encode()).hexdigest()


def clear_recovered(state, client):
    # A cached result, token check or unrelated API call cannot prove recovery.
    scopes = getattr(client, "authorized_scopes", ())
    if not isinstance(scopes, (set, frozenset)):
        return
    for scope in scopes:
        slot = incident_slot(scope)
        if state.get(slot):
            state.set(slot, None)


def alert_key(state, error, command):
    # Preserve any older unscoped incident until explicitly reconciled; its
    # original resource cannot safely be inferred from successful token checks.
    legacy = state.get("authorization_incident")
    if legacy:
        return legacy
    scope = error.scope or "unclassified:" + command
    slot = incident_slot(scope)
    key = state.get(slot)
    if not key:
        key = "auth-alert-" + uuid.uuid4().hex
        state.set(slot, key)
    return key
