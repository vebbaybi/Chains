import json
import yaml
from pathlib import Path
from threading import Lock

# === Custom Exception ===
class ConfigError(Exception):
    pass

# === ChainCrawlr Configuration Loader ===
class ChainCrawlrConfig:
    def __init__(self, chains_path: str = "config/chains.json", settings_path: str = "config/settings.yaml"):
        self._chains_path = Path(chains_path)
        self._settings_path = Path(settings_path)
        self._chains_data = self._load_chains()
        self._settings_data = self._load_settings()

        self.chains = self._validate_chains()
        self.settings = self._validate_settings()

    def _load_chains(self):
        if not self._chains_path.exists():
            raise ConfigError(f"Missing file: {self._chains_path}")
        try:
            with open(self._chains_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(f"Invalid JSON in {self._chains_path}: {e}")

    def _load_settings(self):
        if not self._settings_path.exists():
            raise ConfigError(f"Missing file: {self._settings_path}")
        try:
            with open(self._settings_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML in {self._settings_path}: {e}")

    def _validate_chains(self):
        chains = self._chains_data.get("chains", [])
        if not isinstance(chains, list) or len(chains) == 0:
            raise ConfigError("Missing or invalid 'chains' array in chains.json")

        for chain in chains:
            required = ["name", "rpc_urls", "native_currency", "gas_settings", "dexes", "block_explorer"]
            for key in required:
                if key not in chain:
                    raise ConfigError(f"Missing '{key}' in chain: {chain.get('name', 'UNKNOWN')}")
        return chains

    def _validate_settings(self):
        if not isinstance(self._settings_data, dict):
            raise ConfigError("settings.yaml must contain a top-level dictionary")
        required = ["logging", "alerts", "wallet", "trading"]
        for key in required:
            if key not in self._settings_data:
                raise ConfigError(f"Missing required section in settings.yaml: '{key}'")
        return self._settings_data

    def get_chain(self, name: str):
        for chain in self.chains:
            if chain["name"].lower() == name.lower():
                return chain
        raise ConfigError(f"Chain '{name}' not found in chains.json")

    def get_setting(self, key: str):
        return self.settings.get(key)

    def dump_summary(self):
        print(f"[Chains] Loaded {len(self.chains)} chain(s):")
        for c in self.chains:
            print(f"  - {c['name']} (ID: {c['chain_id']})")
        print(f"[Settings] Sections available: {list(self.settings.keys())}")

# === Singleton Loader ===
_config_instance = None
_config_lock = Lock()

def load_config(force_reload: bool = False) -> ChainCrawlrConfig:
    global _config_instance
    with _config_lock:
        if _config_instance is None or force_reload:
            _config_instance = ChainCrawlrConfig()
        return _config_instance
