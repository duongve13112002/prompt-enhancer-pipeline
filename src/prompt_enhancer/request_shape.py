"""The shape of a request, shared by the command line parser and S0 (MS-3, decision 179).

`request.v0` carries one optional `source_image`; `request.v1` carries `source_images`, a list in the order the caller
means. The two are told apart by which field is present, a structural fact. Nothing here reads a request's text, and
every refusal reason is built from field names and integers only, never from a request value (R-026).
"""

V0 = 'request.v0'  # artifact-bound schema ids (contracts/schemas)
V1 = 'request.v1'
SOURCE = 'source_image'  # artifact-bound field names of the two request versions
SOURCES = 'source_images'

CONFLICT = 'SOURCE_IMAGES_CONFLICT'  # protocol-bound refusal codes (docs/stages/S0-intake.md)
REQUIRED = 'SOURCE_IMAGE_REQUIRED'
UNEXPECTED = 'SOURCE_IMAGE_UNEXPECTED'


def request_version(request):
    """`request.v1` exactly when the request holds `source_images`; anything else, also a non-object, is `request.v0`
    (its schema then refuses what is wrong)."""
    return V1 if isinstance(request, dict) and SOURCES in request else V0


def source_images(request):
    """The base64 strings of a validated request in caller order: `[]` for none, one item for `source_image`."""
    if request_version(request) == V1:
        return list(request[SOURCES])
    value = request.get(SOURCE)
    return [] if value is None else [value]


def structure_refusal(request):
    """None, or `(code, field, reason)` for the first structural rule a request breaks before its schema is read.

    Both fields sent is refused for every request. For `request.v1` the `edit` and `generate` rules are checked here too,
    so a `request.v1` is refused with the same codes as a `request.v0` (its schema holds the same rules as a second net).
    `request.v0` keeps its own order: schema first, then `v0_refusal`. Reasons name fields only."""
    if not isinstance(request, dict):
        return None
    if SOURCE in request and SOURCES in request:
        return CONFLICT, 'request', f'{SOURCE} and {SOURCES} are both set; send one of them'
    if request_version(request) == V1:
        task, sources = request.get('task'), request[SOURCES]
        if task == 'edit' and isinstance(sources, list) and not sources:
            return REQUIRED, f'request.{SOURCES}', 'required by task edit, with at least one image'
        if task == 'generate':
            return UNEXPECTED, f'request.{SOURCES}', 'not accepted by task generate'
    return None


def v0_refusal(request):
    """The `request.v0` rules the schema leaves to code, with today's wording: `(code, reason)` or None."""
    if request['task'] == 'edit' and request.get(SOURCE) is None:
        return REQUIRED, 'required by task edit'
    if request['task'] == 'generate' and request.get(SOURCE) is not None:
        return UNEXPECTED, 'not accepted by task generate'
    return None
