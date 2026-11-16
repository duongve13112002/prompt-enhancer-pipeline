"""Platform capability checks over `os.supports_dir_fd`, `os.supports_fd` and `os.supports_follow_symlinks`.

CPython fills those sets at import of `os` with the function objects of the native module (`Lib/os.py` `_add`:
`_set.add(_globals[fn])`). A membership test of the live `os.<name>` therefore fails as soon as anything replaced
that attribute (a test double, an instrumentation wrapper), and a capture at import holds whatever was installed at
that moment (S42-ci3 L1, review P3-3). The functions' own names survive both and an `importlib.reload(os)`, which
rebuilds the sets from the same native functions. A primitive the platform lacks is still absent from the set, so the
check still refuses there.
"""


def os_supports(capability, *names):
    """True when the `os.supports_*` set `capability` (read by the caller at call time) holds a function under each of
    `names` (the `os` attribute names, for example 'open', 'link')."""
    present = {getattr(function, '__name__', None) for function in capability}
    return all(name in present for name in names)
