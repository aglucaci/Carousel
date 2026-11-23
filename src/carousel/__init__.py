"""CAROUSEL: personalized scientific literature discovery and research intelligence."""

__version__ = "0.1.0.dev0"

from carousel.core.config import ConfigError, RadarConfig, load_radar  # noqa: E402


def __getattr__(name: str):
    # Imported lazily so `import carousel` (and the CLI's config commands) stay light.
    if name in ("Radar", "ScanResults"):
        from carousel import radar

        return getattr(radar, name)
    raise AttributeError(f"module 'carousel' has no attribute {name!r}")


__all__ = ["ConfigError", "Radar", "RadarConfig", "ScanResults", "load_radar", "__version__"]
