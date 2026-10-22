def windows_are_shipped() -> bool:
    """Whether this build partitions conversations into windows (docs/SHIPPING.md).

    The alpha leaves the window index out of the package: on 20 recall
    questions the answers were identical with and without it (2026-10-22), and
    building, embedding and reading the windows ran on every sync.
    """
    import importlib.util

    # find_spec, not an import: the module stays unloaded until a sync runs.
    try:
        return importlib.util.find_spec("varyz.pipeline.windows") is not None
    except ModuleNotFoundError as exc:
        if exc.name != "varyz.pipeline.windows":
            raise
        return False
