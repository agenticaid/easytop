def test_package_layout():
    import easytop.adapters, easytop.agent, easytop.connector, easytop.family, easytop.store  # noqa: F401,E401,I001

    assert easytop.__name__ == "easytop"
