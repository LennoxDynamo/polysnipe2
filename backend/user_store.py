"""
Minimal settings persistence layer for authenticated users.
Guest settings remain client-side only in Stage 1.
"""

import os
import json
import asyncio
from pathlib import Path
from typing import Dict, Optional


# Use /tmp for Railway; backend/data for local development
DATA_DIR = Path("/tmp/polysnipe_user_data") if os.getenv("RAILWAY_ENVIRONMENT_NAME") else Path(__file__).parent / "data"


async def ensure_data_dir():
    """Create data directory if it doesn't exist."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"Warning: Could not create data directory {DATA_DIR}: {e}")


def _user_settings_path(user_id: str) -> Path:
    """Get file path for a user's settings."""
    # Sanitize user_id to prevent directory traversal
    safe_id = user_id.replace("/", "_").replace("\\", "_").replace("..", "_")
    return DATA_DIR / f"{safe_id}.json"


async def load_settings(user_id: str) -> Dict:
    """
    Load settings for an authenticated user.
    
    Args:
        user_id: unique user identifier (typically email)
    
    Returns:
        Settings dict; empty dict {} if file doesn't exist or can't be read
    """
    await ensure_data_dir()
    path = _user_settings_path(user_id)
    
    try:
        if path.exists():
            with open(path, "r") as f:
                return json.load(f)
    except Exception as e:
        print(f"Warning: Could not load settings for {user_id}: {e}")
    
    return {}


async def save_settings(user_id: str, settings: Dict) -> bool:
    """
    Save settings for an authenticated user.
    
    Args:
        user_id: unique user identifier (typically email)
        settings: settings dict to persist
    
    Returns:
        True if save succeeded; False otherwise
    """
    await ensure_data_dir()
    path = _user_settings_path(user_id)
    
    try:
        with open(path, "w") as f:
            json.dump(settings, f, indent=2)
        return True
    except Exception as e:
        print(f"Warning: Could not save settings for {user_id}: {e}")
        return False


async def delete_settings(user_id: str) -> bool:
    """
    Delete settings for a user (e.g., on account deletion).
    
    Args:
        user_id: unique user identifier
    
    Returns:
        True if delete succeeded; False otherwise
    """
    path = _user_settings_path(user_id)
    try:
        if path.exists():
            path.unlink()
        return True
    except Exception as e:
        print(f"Warning: Could not delete settings for {user_id}: {e}")
        return False
