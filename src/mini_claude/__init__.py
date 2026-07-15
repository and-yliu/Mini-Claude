from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("Mini-Claude")
except PackageNotFoundError:
    __version__ = "0.0.1"

