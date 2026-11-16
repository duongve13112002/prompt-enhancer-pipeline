"""Tiny labelled synthetic K5 tables/settings, no model or source KB access."""

from types import SimpleNamespace

from prompt_enhancer.k5_index import build_index
from prompt_enhancer.storage import SPECS
from storage_fixtures import dataset, make_store


def config(*, block=2, n=2, metric='inner_product'):
    return {'index': {'engine': 'numpy_exact', 'build': {'block_rows': block}, 'metric': metric,
                      'lexical': {'kind': 'char_ngram', 'ngram_size': n}}}


def write_snapshot(writer, rows=None, dim=4):
    builder = writer.begin_snapshot()
    for name, data in (dataset() if rows is None else rows).items():
        builder.write_table(name, data, dim=dim if SPECS[name].vector else None)
    return builder


def published(project_root, tmp_path, *, rows=None, dim=4, settings=None, **store_settings):
    settings = config() if settings is None else settings
    store = make_store(project_root, tmp_path / 'store', **store_settings)
    with store.writer() as writer:
        builder = write_snapshot(writer, rows, dim)
        result = build_index(builder, settings)
        sid = builder.finish()
        writer.publish(sid)
    return SimpleNamespace(store=store, config=settings, result=result)
