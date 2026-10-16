def test_kipy_modules_we_depend_on_import() -> None:
    # kicad-python 0.8.0 from PyPI fails here (kipy.schematic imports a missing
    # PageSettings; board_jobs_pb2 references an undefined kiapi.common.types.Units).
    # The pinned git revision in pyproject.toml must pass.
    import kipy
    import kipy.board
    import kipy.board_jobs
    import kipy.schematic

    assert hasattr(kipy, "KiCad")
    assert hasattr(kipy.schematic, "Schematic")
    assert hasattr(kipy.board, "Board")
