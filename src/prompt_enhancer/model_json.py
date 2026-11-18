"""Protocol-strict JSON at model and artifact boundaries."""
import json
import math


MAX_JSON_DEPTH = 128  # Protocol bound shared by recursive schema, copy and serialization consumers.


def check_json_depth(value):
    """Refuse nesting beyond what downstream recursive consumers can safely process."""
    stack = [iter((value,))]
    while stack:
        try:
            item = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        if isinstance(item, (dict, list, tuple)):
            if len(stack) > MAX_JSON_DEPTH:
                raise ValueError('JSON nesting exceeds the supported depth')
            stack.append(iter(item.values() if isinstance(item, dict) else item))


def strict_json(raw):
    """Reject duplicate names, nonfinite numbers and excessive nesting."""
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('duplicate JSON key')
            value[key] = item
        return value

    def constant(_):
        raise ValueError('nonfinite JSON number')

    def number(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError('nonfinite JSON number')
        return value

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
    except RecursionError as error:
        raise ValueError('JSON nesting exceeds the supported depth') from error
    check_json_depth(value)
    return value
