"""CAROUSEL: personalized scientific literature discovery and research intelligence."""

__version__ = "0.1.0.dev0"

from carousel.core.config import ConfigError, RadarConfig, load_radar  # noqa: E402

__all__ = ["ConfigError", "RadarConfig", "load_radar", "__version__"]
