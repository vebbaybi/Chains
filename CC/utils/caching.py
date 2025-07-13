# utils/caching.py
"""
File-based JSON caching system with expiration support
"""

import hashlib
import json
import os
import time
from pathlib import Path


class JSONFileCache:
    """Simple file-based JSON cache with expiration support"""
    
    def __init__(self, cache_dir=".cache", max_age=86400):
        """
        Initialize cache
        Args:
            cache_dir: Directory to store cache files
            max_age: Maximum age of cached items in seconds (default 24 hours)
        """
        self.cache_dir = Path(cache_dir)
        self.max_age = max_age
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_cache_path(self, key):
        """Get filesystem path for a cache key"""
        hashed_key = hashlib.sha256(str(key).encode('utf-8')).hexdigest()
        return self.cache_dir / f"{hashed_key}.json"

    def get(self, key):
        """
        Retrieve cached value
        Args:
            key: Cache key to lookup
        Returns:
            Cached value or None if not found/expired
        """
        cache_file = self._get_cache_path(key)
        
        if not cache_file.exists():
            return None
            
        if time.time() - cache_file.stat().st_mtime > self.max_age:
            cache_file.unlink(missing_ok=True)
            return None

        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            cache_file.unlink(missing_ok=True)
            return None

    def set(self, key, value):
        """
        Store value in cache
        Args:
            key: Cache key
            value: Value to store
        Returns:
            True if successful, False otherwise
        """
        cache_file = self._get_cache_path(key)
        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(value, f, indent=2)
            return True
        except (TypeError, IOError):
            return False

    def clear(self, key=None):
        """
        Clear cache entry or entire cache
        Args:
            key: Specific key to clear (None clears all)
        """
        if key:
            cache_file = self._get_cache_path(key)
            cache_file.unlink(missing_ok=True)
        else:
            for file in self.cache_dir.glob("*.json"):
                file.unlink(missing_ok=True)