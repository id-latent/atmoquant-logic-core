[pytest]
# ==============================================================================
# pytest.ini — Konfigurasi pytest untuk AQL mock tests
# ==============================================================================

# Direktori test
testpaths = .

# Pattern file test
python_files  = test_*.py
python_classes = Test*
python_functions = test_*

# asyncio mode: auto = semua test async otomatis memakai event loop
asyncio_mode = auto

# Verbose output
addopts =
    -v
    --tb=short
    --strict-markers
    --no-header

# Custom markers
markers =
    asyncio: marks test sebagai async (otomatis oleh pytest-asyncio)
    slow: marks test yang lambat (skip dengan -m "not slow")
    integration: marks test yang butuh network nyata

# Minimum pytest version
minversion = 7.0
