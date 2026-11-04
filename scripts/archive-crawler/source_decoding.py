"""Use the same dependency-free decoder in the standalone CLI and containers."""
try:
    from indexer.source_encoding import decode_source
except ModuleNotFoundError as error:
    if error.name!='indexer':raise
    # The operator CLI runs directly from the software checkout, without
    # installing the indexer's document/ML dependency set.
    from pathlib import Path
    import runpy
    decode_source=runpy.run_path(str(Path(__file__).resolve().parents[2]/
        'elastic-indexer-stack/indexer/src/indexer/source_encoding.py'))['decode_source']
