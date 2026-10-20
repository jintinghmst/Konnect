def pytest_configure(config):
    config.addinivalue_line("markers", "slow: closed-loop or SPICE runs taking more than a few seconds")
