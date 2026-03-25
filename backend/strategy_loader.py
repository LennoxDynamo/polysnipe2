"""
strategy_loader.py
------------------
Scans the /strategies/ directory and dynamically loads every .py file
that contains a class named Strategy(BaseStrategy).
New strategy files are picked up on restart — no code changes needed.
"""

import importlib.util
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

STRATEGIES_DIR = Path(__file__).parent / "strategies"

# Registry: id → Strategy class
_registry: dict[str, type] = {}


def _load_file(path: Path) -> bool:
    """Load a single strategy file and register it."""
    module_name = f"strategies.{path.stem}"
    try:
        # Add strategies dir to sys.path so __base__ imports work
        strat_dir = str(STRATEGIES_DIR)
        if strat_dir not in sys.path:
            sys.path.insert(0, strat_dir)

        spec   = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        cls = getattr(module, "Strategy", None)
        if cls is None:
            logger.debug(f"No Strategy class in {path.name} — skipping")
            return False

        # Validate it has on_tick
        if not hasattr(cls, "on_tick"):
            logger.warning(f"Strategy in {path.name} missing on_tick() — skipping")
            return False

        strategy_id = path.stem
        _registry[strategy_id] = cls
        logger.info(f"Loaded strategy: {cls.NAME!r} ({strategy_id})")
        return True

    except Exception as e:
        logger.error(f"Failed to load {path.name}: {e}", exc_info=True)
        return False


def load_all() -> int:
    """Scan strategies/ dir and load all valid strategy files."""
    _registry.clear()
    count = 0
    for path in sorted(STRATEGIES_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        if _load_file(path):
            count += 1
    logger.info(f"Strategy loader: {count} strategies registered")
    return count


def get(strategy_id: str, params: dict = {}) -> object:
    """Instantiate a strategy by ID with given params."""
    cls = _registry.get(strategy_id)
    if cls is None:
        raise KeyError(f"Unknown strategy: {strategy_id!r}. Available: {list(_registry.keys())}")
    return cls(params)


def list_all() -> list[dict]:
    """Return metadata for all registered strategies."""
    result = []
    for sid, cls in _registry.items():
        instance = cls()
        meta = instance.meta()
        meta["id"] = sid
        result.append(meta)
    return result


def ids() -> list[str]:
    return list(_registry.keys())
