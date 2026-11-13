import hashlib
import json

SOURCE_FIELDS = ("source_key", "day", "channel", "region", "revenue", "cost", "refunds", "orders", "reference_net")


def rows_payload(rows):
    """把 ORM 行或预览字典统一成可哈希的规范负载。"""
    payload = []
    for row in rows:
        if isinstance(row, dict):
            payload.append([row[field] for field in SOURCE_FIELDS])
        else:
            payload.append([getattr(row, field) for field in SOURCE_FIELDS])
    payload.sort(key=lambda item: item[0])
    return payload


def rows_checksum(rows):
    """按来源编号排序后做稳定 SHA256，用于预览版本与重放校验。"""
    encoded = json.dumps(rows_payload(rows), ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()
