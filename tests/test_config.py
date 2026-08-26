"""平台配置层测试。"""

from pathlib import Path

from uv_mgr import config


class TestDataPaths:
    def test_posix_default_data_dir(self, monkeypatch):
        monkeypatch.setattr(config.sys, "platform", "linux")
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("UV_MGR_DATA_DIR", raising=False)
        monkeypatch.delenv("UV_MGR_DB_PATH", raising=False)
        assert config.get_data_dir() == Path.home() / ".local" / "share" / "uv-mgr"

    def test_posix_xdg_data_dir(self, monkeypatch):
        monkeypatch.setattr(config.sys, "platform", "linux")
        monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg-data")
        assert config.get_data_dir() == Path("/tmp/xdg-data/uv-mgr")

    def test_windows_local_appdata_then_appdata_then_home(self, monkeypatch):
        monkeypatch.setattr(config.sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", r"C:\\Users\\示例 用户\\AppData\\Local")
        monkeypatch.setenv("APPDATA", r"C:\\Users\\示例 用户\\AppData\\Roaming")
        assert str(config.get_data_dir()).endswith("uv-mgr")
        monkeypatch.delenv("LOCALAPPDATA")
        assert "Roaming" in str(config.get_data_dir())
        monkeypatch.delenv("APPDATA")
        assert str(config.get_data_dir()).replace("\\", "/").endswith("AppData/Local/uv-mgr")

    def test_db_path_override_has_highest_priority(self, monkeypatch):
        monkeypatch.setenv("UV_MGR_DATA_DIR", "/tmp/data")
        monkeypatch.setenv("UV_MGR_DB_PATH", "/tmp/custom/index.sqlite3")
        assert config.get_db_path() == Path("/tmp/custom/index.sqlite3")

    def test_data_dir_override_used_for_default_database(self, monkeypatch):
        monkeypatch.delenv("UV_MGR_DB_PATH", raising=False)
        monkeypatch.setenv("UV_MGR_DATA_DIR", "/tmp/data with spaces")
        assert config.get_db_path() == Path("/tmp/data with spaces/index.db")


class TestCommandsAndPaths:
    def test_uv_command_override(self, monkeypatch):
        monkeypatch.setenv("UV_MGR_UV_BIN", r"C:\\Program Files\\uv\\uv.exe")
        assert config.get_uv_command() == r"C:\\Program Files\\uv\\uv.exe"

    def test_windows_uv_exe_discovery(self, monkeypatch):
        monkeypatch.setattr(config.sys, "platform", "win32")
        monkeypatch.delenv("UV_MGR_UV_BIN", raising=False)
        monkeypatch.setattr(config.shutil, "which", lambda name: r"C:\\Tools\\uv.exe" if name == "uv.exe" else None)
        assert config.get_uv_command() == r"C:\\Tools\\uv.exe"
        assert config.resolve_uv_command(["uv", "--version"])[0] == r"C:\\Tools\\uv.exe"

    def test_uv_command_falls_back_to_logical_name(self, monkeypatch):
        monkeypatch.setattr(config.sys, "platform", "win32")
        monkeypatch.delenv("UV_MGR_UV_BIN", raising=False)
        monkeypatch.setattr(config.shutil, "which", lambda _name: None)
        assert config.get_uv_command() == "uv"

    def test_windows_path_normalization_is_case_insensitive(self, monkeypatch):
        monkeypatch.setattr(config.sys, "platform", "win32")
        assert config.normalize_path(r"C:\\Users\\Name\\项目\\.venv") == config.normalize_path(r"c:\\users\\name\\项目\\.venv")

    def test_windows_interpreter_is_preferred(self, monkeypatch):
        monkeypatch.setattr(config.sys, "platform", "win32")
        candidates = config.venv_python_candidates("environment")
        assert candidates[0] == Path("environment") / "Scripts" / "python.exe"
