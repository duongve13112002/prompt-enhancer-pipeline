"""Load configured credentials only at runtime."""

from pathlib import Path, PurePosixPath
import os
import stat


def load_api_key(project_root, configured_path):
    """Read one key file within the project's protected credential directory."""
    spelling = PurePosixPath(configured_path)
    if spelling.is_absolute() or len(spelling.parts) != 2 or spelling.parts[0] != '.secrets':
        raise ValueError('api key path must be .secrets/<filename>')
    if '\\' in configured_path or spelling.parts[1] in ('.', '..'):
        raise ValueError('invalid api key path')
    root = Path(project_root).resolve()
    secret_root = (root / '.secrets').resolve()
    if secret_root != root / '.secrets':
        raise ValueError('credential directory must not be a link')
    candidate = secret_root / spelling.parts[1]
    try:
        info = os.lstat(candidate)
    except OSError as error:
        raise ValueError('api_key_file is missing or unreadable') from error
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
        raise ValueError('api_key_file must not be a link')
    if info.st_nlink > 1:
        raise ValueError('api_key_file must not have multiple hard links')
    try:
        value = candidate.read_text(encoding='utf-8').strip()
    except OSError as error:
        raise ValueError('api_key_file is missing or unreadable') from error
    if not value or '\n' in value or '\r' in value:
        raise ValueError('credential must be one nonempty line')
    return value
