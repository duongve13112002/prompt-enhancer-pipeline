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


class JsonObject(dict):
    """A decoded JSON object that remembers the exact reply text it was decoded from (T017-FIX5).

    Equal to the plain dict, serialized as one; only the text-property checks of an admin validator read `reply_text`.
    """
    reply_text = None

    def __deepcopy__(self, memo):
        from copy import deepcopy
        result = type(self)((key, deepcopy(item, memo)) for key, item in self.items())
        result.reply_text = self.reply_text
        return result

    def __reduce__(self):
        return (_restore, (type(self), dict(self), self.reply_text))


class JsonArray(list):
    """The array counterpart of `JsonObject`."""
    reply_text = None

    def __deepcopy__(self, memo):
        from copy import deepcopy
        result = type(self)(deepcopy(item, memo) for item in self)
        result.reply_text = self.reply_text
        return result

    def __reduce__(self):
        return (_restore, (type(self), list(self), self.reply_text))


def _restore(cls, content, text):
    result = cls(content)
    result.reply_text = text
    return result


def with_reply_text(value, text):
    """`value` decoded from `text`, carrying that text; a scalar has no carrier and comes back unchanged."""
    carrier = JsonObject if isinstance(value, dict) else JsonArray if isinstance(value, list) else None
    if carrier is None:
        return value
    result = carrier(value)
    result.reply_text = text
    return result


def reply_text(value):
    """The exact text `value` was decoded from, or None when it has none or the text no longer decodes to `value`
    (a changed value never reuses an old text)."""
    text = getattr(value, 'reply_text', None) if isinstance(value, (JsonObject, JsonArray)) else None
    if not isinstance(text, str):
        return None
    try:
        return text if strict_json(text) == value else None
    except ValueError:
        return None


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
