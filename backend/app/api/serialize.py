from datetime import datetime

from sqlalchemy import inspect


def to_dict(obj, exclude: tuple[str, ...] = ()) -> dict:
    out = {}
    for col in inspect(obj).mapper.column_attrs:
        if col.key in exclude:
            continue
        v = getattr(obj, col.key)
        out[col.key] = v.isoformat() if isinstance(v, datetime) else v
    return out


def many(objs, exclude: tuple[str, ...] = ()) -> list[dict]:
    return [to_dict(o, exclude) for o in objs]
