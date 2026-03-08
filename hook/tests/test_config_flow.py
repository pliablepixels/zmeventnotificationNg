"""Tests for config flow from objectconfig.yml through zm_detect.py into pyzm.

Covers monitor overrides, monitor_id injection, remote gateway injection,
import_zm_zones pattern passthrough, and config variant combinations.
"""
import os
import ssl
import tempfile
from unittest.mock import MagicMock

import pytest
import yaml

import zmes_hook_helpers.common_params as g
from zmes_hook_helpers.utils import process_config, import_zm_zones


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fixtures_dir():
    return os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def config_path(fixtures_dir):
    return os.path.join(fixtures_dir, "test_objectconfig.yml")


@pytest.fixture
def secrets_path(fixtures_dir):
    return os.path.join(fixtures_dir, "test_secrets.yml")


@pytest.fixture
def patched_config(config_path, secrets_path):
    """Return a temp config file with the secrets path rewritten."""
    with open(config_path) as f:
        data = yaml.safe_load(f)
    data["general"]["secrets"] = secrets_path
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False)
    yaml.dump(data, tmp)
    tmp.close()
    yield tmp.name
    os.unlink(tmp.name)


@pytest.fixture
def ctx():
    return ssl.create_default_context()


def _make_config_file(tmp_path, cfg, secrets=None):
    """Write a config (and optional secrets) to tmp_path, return config path."""
    if secrets is not None:
        secrets_path = str(tmp_path / "secrets.yml")
        with open(secrets_path, "w") as f:
            yaml.dump(secrets, f)
        cfg.setdefault("general", {})["secrets"] = secrets_path
    cfg_path = str(tmp_path / "objectconfig.yml")
    with open(cfg_path, "w") as f:
        yaml.dump(cfg, f)
    return cfg_path


def _minimal_general(**overrides):
    """Return a minimal general section with required fields."""
    base = {
        "base_data_path": "/var/lib/zmeventnotification",
        "allow_self_signed": "yes",
        "portal": "https://zm.example.com/zm",
        "api_portal": "https://zm.example.com/zm/api",
        "user": "u",
        "password": "p",
        "write_image_to_zm": "no",
        "write_debug_image": "no",
        "show_percent": "no",
        "import_zm_zones": "no",
        "only_triggered_zm_zones": "no",
    }
    base.update(overrides)
    return base


# ===========================================================================
# 1. TestMonitorOverrides
# ===========================================================================

class TestMonitorOverrides:
    """Tests that monitor-specific overrides work correctly."""

    def test_zone_coords_parsed_and_pattern_becomes_pattern(self, patched_config, ctx):
        """Coords string is parsed to tuples, detection_pattern becomes pattern."""
        process_config({"config": patched_config, "monitorid": "1"}, ctx)
        front = [p for p in g.polygons if p["name"] == "front_yard"][0]
        assert front["value"] == [(0, 0), (640, 0), (640, 480), (0, 480)]
        assert front["pattern"] == "person"

    def test_zone_ignore_pattern_passed_through(self, patched_config, ctx):
        """ignore_pattern from zone config is passed through to polygon dict."""
        process_config({"config": patched_config, "monitorid": "2"}, ctx)
        driveway = [p for p in g.polygons if p["name"] == "driveway"][0]
        assert driveway["pattern"] == "(person|car)"
        assert driveway["ignore_pattern"] == "(car|truck)"

    def test_scalar_override(self, patched_config, ctx):
        """Monitor override of scalar config: poly_thickness 4 overrides global 2."""
        process_config({"config": patched_config, "monitorid": "1"}, ctx)
        assert g.config["poly_thickness"] == 4

    def test_only_triggered_zm_zones_skips_polygons(self, tmp_path, ctx):
        """only_triggered_zm_zones=yes (global) skips zone polygons and sets import_zm_zones."""
        cfg = {
            "general": _minimal_general(only_triggered_zm_zones="yes"),
            "ml": {
                "ml_sequence": {"general": {"model_sequence": "object"}},
                "stream_sequence": {"resize": 800},
            },
            "monitors": {
                1: {
                    "zones": {
                        "backyard": {
                            "coords": "0,0 100,0 100,100 0,100",
                            "detection_pattern": "dog",
                        }
                    },
                }
            },
        }
        cfg_path = _make_config_file(tmp_path, cfg)
        process_config({"config": cfg_path, "monitorid": "1"}, ctx)
        # Zone polygons should be skipped when only_triggered_zm_zones is yes
        assert g.polygons == []
        # import_zm_zones should be set to yes
        assert g.config["import_zm_zones"] == "yes"

    def test_dict_override_deep_merged(self, tmp_path, ctx):
        """Monitor with dict override (ml_sequence) is deep-merged."""
        cfg = {
            "general": _minimal_general(),
            "ml": {
                "ml_sequence": {
                    "general": {"model_sequence": "object"},
                    "object": {
                        "general": {"pattern": "(person|car)"},
                        "sequence": [{"name": "yolo"}],
                    },
                },
                "stream_sequence": {"resize": 800},
            },
            "monitors": {
                5: {
                    "ml_sequence": {
                        "object": {
                            "general": {"pattern": "(dog)"},
                        },
                    },
                }
            },
        }
        cfg_path = _make_config_file(tmp_path, cfg)
        process_config({"config": cfg_path, "monitorid": "5"}, ctx)
        ml = g.config["ml_sequence"]
        # Deep-merged: object.general.pattern overridden
        assert ml["object"]["general"]["pattern"] == "(dog)"
        # Deep-merged: general.model_sequence preserved from base
        assert ml["general"]["model_sequence"] == "object"
        # Deep-merged: object.sequence preserved from base
        assert ml["object"]["sequence"] == [{"name": "yolo"}]

    def test_no_monitorid_no_zones_no_overrides(self, patched_config, ctx):
        """No monitorid: no zones, no overrides applied."""
        process_config({"config": patched_config}, ctx)
        assert g.polygons == []
        # Global wait remains default (not overridden by any monitor)
        assert g.config["wait"] == 0


# ===========================================================================
# 2. TestMonitorIdInjection
# ===========================================================================

class TestMonitorIdInjection:
    """Test that zm_detect.py injects monitor_id and image_path into ml_options."""

    def test_monitor_id_and_image_path_injected(self, patched_config, ctx):
        """Simulate zm_detect.py's injection of monitor_id and image_path."""
        process_config({"config": patched_config, "monitorid": "5"}, ctx)

        # Simulate what zm_detect.py does after process_config
        ml_options = g.config["ml_sequence"]
        ml_options.setdefault("general", {})["monitor_id"] = "5"
        ml_options.setdefault("general", {})["image_path"] = g.config.get(
            "image_path", "/var/lib/zmeventnotification/images"
        )

        assert ml_options["general"]["monitor_id"] == "5"
        assert ml_options["general"]["image_path"] == "/var/lib/zmeventnotification/images"

    def test_image_path_uses_config_value(self, tmp_path, ctx):
        """image_path comes from g.config after path substitution."""
        cfg = {
            "general": _minimal_general(image_path="${base_data_path}/custom_images"),
            "ml": {
                "ml_sequence": {"general": {"model_sequence": "object"}},
                "stream_sequence": {"resize": 800},
            },
        }
        cfg_path = _make_config_file(tmp_path, cfg)
        process_config({"config": cfg_path, "monitorid": "7"}, ctx)

        ml_options = g.config["ml_sequence"]
        ml_options.setdefault("general", {})["monitor_id"] = "7"
        ml_options.setdefault("general", {})["image_path"] = g.config.get(
            "image_path", "/var/lib/zmeventnotification/images"
        )

        assert ml_options["general"]["monitor_id"] == "7"
        assert ml_options["general"]["image_path"] == "/var/lib/zmeventnotification/custom_images"


# ===========================================================================
# 3. TestRemoteConfigInjection
# ===========================================================================

class TestRemoteConfigInjection:
    """Test that remote gateway settings are injected into ml_options."""

    def test_gateway_settings_injected(self, tmp_path, ctx):
        """Simulate zm_detect.py's gateway injection into ml_options."""
        cfg = {
            "general": _minimal_general(),
            "remote": {
                "ml_gateway": "http://gpu:5000",
                "ml_user": "admin",
                "ml_password": "secret",
            },
            "ml": {
                "ml_sequence": {"general": {"model_sequence": "object"}},
                "stream_sequence": {"resize": 800},
            },
        }
        cfg_path = _make_config_file(tmp_path, cfg)
        process_config({"config": cfg_path}, ctx)

        # Verify remote config was loaded
        assert g.config["ml_gateway"] == "http://gpu:5000"
        assert g.config["ml_user"] == "admin"
        assert g.config["ml_password"] == "secret"

        # Simulate what zm_detect.py does for gateway injection
        ml_options = g.config["ml_sequence"]
        if g.config.get("ml_gateway"):
            ml_options.setdefault("general", {})["ml_gateway"] = g.config["ml_gateway"]
            ml_options["general"]["ml_user"] = g.config.get("ml_user")
            ml_options["general"]["ml_password"] = g.config.get("ml_password")
            ml_options["general"]["ml_timeout"] = g.config.get("ml_timeout", 60)
            ml_options["general"]["ml_gateway_mode"] = g.config.get("ml_gateway_mode", "url")

        assert ml_options["general"]["ml_gateway"] == "http://gpu:5000"
        assert ml_options["general"]["ml_user"] == "admin"
        assert ml_options["general"]["ml_password"] == "secret"
        assert ml_options["general"]["ml_gateway_mode"] == "url"

    def test_no_gateway_no_injection(self, tmp_path, ctx):
        """Without ml_gateway, no gateway keys are injected."""
        cfg = {
            "general": _minimal_general(),
            "remote": {
                "ml_gateway": None,
            },
            "ml": {
                "ml_sequence": {"general": {"model_sequence": "object"}},
                "stream_sequence": {"resize": 800},
            },
        }
        cfg_path = _make_config_file(tmp_path, cfg)
        process_config({"config": cfg_path}, ctx)

        ml_options = g.config["ml_sequence"]
        if g.config.get("ml_gateway"):
            ml_options.setdefault("general", {})["ml_gateway"] = g.config["ml_gateway"]

        # No gateway injection should have happened
        assert "ml_gateway" not in ml_options.get("general", {})


# ===========================================================================
# 4. TestImportZmZonesPatternPassthrough
# ===========================================================================

class TestImportZmZonesPatternPassthrough:
    """Test that import_zm_zones() passes pattern and ignore_pattern through."""

    def _make_zm_client(self, zones):
        """Build a mock zm_client whose monitor().get_zones() returns zones."""
        monitor_mock = MagicMock()
        monitor_mock.get_zones.return_value = zones
        client = MagicMock()
        client.monitor.return_value = monitor_mock
        return client

    def _make_zone(self, name, points, zone_type="Active", pattern=None, ignore_pattern=None):
        """Build a stub Zone object."""
        from pyzm.models.zm import Zone as _StubZone
        return _StubZone(
            name=name,
            points=points,
            pattern=pattern,
            ignore_pattern=ignore_pattern,
            _raw={"Zone": {"Type": zone_type}},
        )

    def test_pattern_and_ignore_pattern_passed_through(self):
        """Zone pattern and ignore_pattern appear in g.polygons."""
        g.config["only_triggered_zm_zones"] = "no"
        zones = [
            self._make_zone("Front Door", [(0, 0), (100, 0), (100, 100)],
                            pattern="(person)", ignore_pattern="(cat)"),
        ]
        client = self._make_zm_client(zones)
        import_zm_zones("1", None, client)

        assert len(g.polygons) == 1
        poly = g.polygons[0]
        assert poly["name"] == "front_door"
        assert poly["pattern"] == "(person)"
        assert poly["ignore_pattern"] == "(cat)"

    def test_none_pattern_when_zone_has_no_pattern(self):
        """Zone without pattern gets None for both pattern and ignore_pattern."""
        g.config["only_triggered_zm_zones"] = "no"
        zones = [
            self._make_zone("Garage", [(0, 0), (50, 0), (50, 50)]),
        ]
        client = self._make_zm_client(zones)
        import_zm_zones("2", None, client)

        assert len(g.polygons) == 1
        poly = g.polygons[0]
        assert poly["name"] == "garage"
        assert poly["pattern"] is None
        assert poly["ignore_pattern"] is None

    def test_inactive_zones_skipped(self):
        """Zones with Type=Inactive are skipped."""
        g.config["only_triggered_zm_zones"] = "no"
        zones = [
            self._make_zone("Active Zone", [(0, 0), (10, 0), (10, 10)],
                            zone_type="Active", pattern="person"),
            self._make_zone("Dead Zone", [(0, 0), (10, 0), (10, 10)],
                            zone_type="Inactive", pattern="car"),
        ]
        client = self._make_zm_client(zones)
        import_zm_zones("1", None, client)

        assert len(g.polygons) == 1
        assert g.polygons[0]["name"] == "active_zone"

    def test_only_triggered_filters_by_reason(self):
        """With only_triggered_zm_zones=yes, zones not in reason are dropped."""
        g.config["only_triggered_zm_zones"] = "yes"
        zones = [
            self._make_zone("Driveway", [(0, 0), (10, 0), (10, 10)],
                            pattern="car"),
            self._make_zone("Porch", [(0, 0), (10, 0), (10, 10)],
                            pattern="person"),
        ]
        client = self._make_zm_client(zones)
        # reason mentions only Driveway
        import_zm_zones("1", "Motion: Driveway", client)

        assert len(g.polygons) == 1
        assert g.polygons[0]["name"] == "driveway"
        assert g.polygons[0]["pattern"] == "car"


# ===========================================================================
# 5. TestConfigVariantCombinations
# ===========================================================================

class TestConfigVariantCombinations:
    """Test various combinations of config settings."""

    def test_allow_self_signed_yes(self, tmp_path):
        """allow_self_signed=yes disables hostname checking."""
        ctx = ssl.create_default_context()
        cfg = {
            "general": _minimal_general(allow_self_signed="yes"),
            "ml": {
                "ml_sequence": {"general": {"model_sequence": "object"}},
                "stream_sequence": {"resize": 800},
            },
        }
        cfg_path = _make_config_file(tmp_path, cfg)
        process_config({"config": cfg_path}, ctx)
        assert ctx.check_hostname is False
        assert ctx.verify_mode == ssl.CERT_NONE

    def test_allow_self_signed_no(self, tmp_path):
        """allow_self_signed=no leaves SSL context unchanged."""
        ctx = ssl.create_default_context()
        original_check = ctx.check_hostname
        original_mode = ctx.verify_mode
        cfg = {
            "general": _minimal_general(allow_self_signed="no"),
            "ml": {
                "ml_sequence": {"general": {"model_sequence": "object"}},
                "stream_sequence": {"resize": 800},
            },
        }
        cfg_path = _make_config_file(tmp_path, cfg)
        process_config({"config": cfg_path}, ctx)
        assert ctx.check_hostname == original_check
        assert ctx.verify_mode == original_mode

    def test_path_substitution(self, tmp_path):
        """${base_data_path} is replaced in string config values."""
        ctx = ssl.create_default_context()
        cfg = {
            "general": _minimal_general(
                base_data_path="/opt/zmdata",
                image_path="${base_data_path}/images",
            ),
            "ml": {
                "ml_sequence": {"general": {"model_sequence": "object"}},
                "stream_sequence": {"resize": 800},
            },
        }
        cfg_path = _make_config_file(tmp_path, cfg)
        process_config({"config": cfg_path}, ctx)
        assert g.config["image_path"] == "/opt/zmdata/images"
        assert "${base_data_path}" not in g.config["image_path"]

    def test_ml_sequence_is_native_dict(self, tmp_path):
        """ml_sequence is a native dict with secrets resolved recursively."""
        ctx = ssl.create_default_context()
        secrets = {"secrets": {"API_KEY": "resolved-key-123"}}
        cfg = {
            "general": _minimal_general(),
            "ml": {
                "ml_sequence": {
                    "general": {"model_sequence": "object"},
                    "object": {
                        "sequence": [{"api_key": "!API_KEY", "name": "yolo"}],
                    },
                },
                "stream_sequence": {"resize": 800},
            },
        }
        cfg_path = _make_config_file(tmp_path, cfg, secrets=secrets)
        process_config({"config": cfg_path}, ctx)

        ml = g.config["ml_sequence"]
        assert isinstance(ml, dict)
        assert ml["object"]["sequence"][0]["api_key"] == "resolved-key-123"
        assert ml["object"]["sequence"][0]["name"] == "yolo"

    def test_push_config_loaded_as_nested_dict(self, tmp_path):
        """Push section is loaded as a nested dict."""
        ctx = ssl.create_default_context()
        cfg = {
            "general": _minimal_general(),
            "ml": {
                "ml_sequence": {"general": {"model_sequence": "object"}},
                "stream_sequence": {"resize": 800},
            },
            "push": {
                "use_fcm": "yes",
                "fcm_tokens": ["token_a", "token_b"],
                "channels": {
                    "default": {"title": "Alert"},
                },
            },
        }
        cfg_path = _make_config_file(tmp_path, cfg)
        process_config({"config": cfg_path}, ctx)

        push = g.config["push"]
        assert isinstance(push, dict)
        assert push["use_fcm"] == "yes"
        assert push["fcm_tokens"] == ["token_a", "token_b"]
        assert push["channels"]["default"]["title"] == "Alert"
