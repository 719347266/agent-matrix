"""Validate fresh and cached message receipts before reporting delivery."""
from .models import MatrixError


def message_id(receipt):
    if isinstance(receipt, dict):
        value = receipt.get("message_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
        for nested in receipt.values():
            found = message_id(nested)
            if found:
                return found
    elif isinstance(receipt, list):
        for nested in receipt:
            found = message_id(nested)
            if found:
                return found
    return None


def require_message_receipt(receipt):
    if not message_id(receipt):
        raise MatrixError("发送结果缺少有效 message_id，送达情况待核对，已阻止自动重发。")
    return receipt


def send_once(state, key, send):
    previous = state.operation(key)
    if previous and previous["status"] == "done" and not message_id(previous["receipt"]):
        # Older code could store an empty receipt as done. Do not replay it.
        state.invalidate_done_receipt(key)
        raise MatrixError("已有发送记录缺少有效 message_id，已改为待核对，不自动重发。")
    return require_message_receipt(state.once(key, lambda: require_message_receipt(send())))
