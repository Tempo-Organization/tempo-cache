from __future__ import annotations
import os
import sys
import stat
import socket
import shutil
import typing
import zipfile
import tarfile
import tempfile
import platform
from pathlib import Path
from urllib.parse import urlparse
from dataclasses import dataclass, field

import requests
import platformdirs
from tomlkit.toml_document import TOMLDocument
from tomlkit import table, document, dumps, loads

from tempo_settings.tempo_settings import SettingsInformation


SCRIPT_DIR = (
    Path(sys.executable).parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)

# FIXME
settings_information: SettingsInformation


def is_windows() -> bool:
    return platform.system() == "Windows"


def is_linux() -> bool:
    return platform.system() == "Linux"


def _env_true(value: str | None) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def is_archive(filename: Path) -> bool:
    return str(filename).endswith((
        '.zip',
        '.tar.gz',
        '.tgz',
        '.tar',
        '.tar.xz',
        '.txz',
    ))


def is_within_directory(base_dir: Path, target_path: Path) -> bool:
    base_dir = base_dir.resolve()
    target_path = target_path.resolve()
    return str(target_path).startswith(str(base_dir))


def unpack_archive(archive_path: Path, extract_to: Path) -> list[Path]:
    if str(archive_path).lower().endswith(".zip"):
        with zipfile.ZipFile(archive_path, 'r') as zip_ref:
            extracted_files = []

            for member in zip_ref.infolist():
                filename = member.filename
                member_path = extract_to / filename

                if Path(filename).is_absolute():
                    raise RuntimeError(f"Absolute path not allowed: {filename}")

                is_symlink = stat.S_ISLNK(member.external_attr >> 16)
                if is_symlink:
                    raise RuntimeError(f"Symlinks not allowed in zip: {filename}")

                if not is_within_directory(extract_to, member_path):
                    raise RuntimeError(f"Unsafe path detected in zip: {filename}")

                zip_ref.extract(member, extract_to)
                extracted_files.append(member_path)

    elif str(archive_path).endswith((".tar.gz", ".tgz", ".tar", ".tar.xz", ".txz")):
        with tarfile.open(archive_path, 'r:*') as tar_ref:
            extracted_files = []

            for member in tar_ref.getmembers():
                member_path = extract_to / member.name

                if Path(member.name).is_absolute():
                    raise RuntimeError("Absolute paths not allowed")

                if member.issym() or member.islnk():
                    raise RuntimeError("Symlinks not allowed in archive")

                if not is_within_directory(extract_to, member_path):
                    raise RuntimeError(f"Unsafe path detected in tar: {member.name}")

                tar_ref.extract(member, extract_to)

                if member.isfile():
                    extracted_files.append(member_path)

    else:
        raise ValueError(f"Unsupported archive format: {archive_path}")

    return extracted_files


def was_no_cache_parameter_in_args() -> bool:
    return '--no-cache' in sys.argv


def was_cache_dir_parameter_in_args() -> bool:
    return '--cache-dir' in sys.argv


def get_cache_dir_param_in_args() -> Path | None:
    if '--cache-dir' in sys.argv:
        idx = sys.argv.index('--cache-dir')
        if idx + 1 < len(sys.argv):
            return Path(sys.argv[idx + 1])
    return None


@dataclass
class ToolInfo:
    tool_name: str
    repo_name: str
    repo_owner: str
    cache: ToolsCache
    file_paths: list[Path] = field(default_factory=list)


    def ensure_tool_installed(self) -> None:
        if not self.is_current_preferred_tool_version_installed():
            self.cache.install_tool_to_cache(self)


    def get_file_to_download(self) -> str:
        if is_windows():
            return f'{self.tool_name}-x86_64-pc-windows-msvc.zip'
        elif is_linux():
            return f'{self.tool_name}-x86_64-unknown-linux-gnu.tar.xz'
        else:
            raise ValueError('Unsupported OS')


    def get_download_url(self) -> str:
        return f'https://github.com/{self.repo_owner}/{self.repo_name}/releases/download/{self.get_current_preferred_release_tag()}/{self.get_file_to_download()}'


    def get_executable_name(self) -> str:
        if is_windows():
            return f'{self.tool_name}.exe'
        elif is_linux():
            return f'{self.tool_name}'
        else:
            raise ValueError('Unsupported OS')


    def get_executable_path(self) -> Path:
        return Path(self.get_tool_directory() / self.get_executable_name())


    def get_current_preferred_release_tag(self) -> str:
        global settings_information
        default_value = "latest"
        config_value = None

        if settings_information.settings:
            config_value = settings_information.settings.get(f'{self.tool_name.lower()}_info', {}).get(f'{self.tool_name.lower()}_release_tag')

        env_value = os.environ.get(f'{self.cache.main_tool_name.upper()}_{self.tool_name.upper()}_RELEASE_TAG')

        cli_value = None
        if f'--{self.tool_name.lower()}-release-tag' in sys.argv:
            idx = sys.argv.index(f'--{self.tool_name.lower()}-release-tag')
            if idx + 1 < len(sys.argv):
                cli_value = sys.argv[idx + 1]
            else:
                raise RuntimeError(f'You passed --{self.tool_name.lower()}-release-tag without a tag after it.')

        prioritized_value = next(
            v for v in [cli_value, env_value, config_value, default_value]
            if v not in (None, "")
        )

        if not prioritized_value:
            raise RuntimeError('get tool directory could not find a prioritized value')

        if prioritized_value == "latest":
            url = f"https://api.github.com/repos/{self.repo_owner}/{self.repo_name}/releases/latest"

            response = requests.get(url, timeout=5)

            if response.status_code == 404:
                # fallback: include prereleases
                fallback_url = f"https://api.github.com/repos/{self.repo_owner}/{self.repo_name}/releases"
                fallback = requests.get(fallback_url, timeout=5)
                fallback.raise_for_status()

                releases = fallback.json()
                if not releases:
                    return "latest"

                return releases[0].get("tag_name", "latest")

            response.raise_for_status()
            return response.json().get("tag_name", "latest")
        return prioritized_value


    def get_tool_directory(self) -> Path:
        global settings_information
        default_value = self.cache.get_tool_install_dir(
            self.repo_name.lower(),
            self.tool_name.lower(),
            self.get_current_preferred_release_tag(),
        )

        config_value = None
        if settings_information.settings:
            config_value = settings_information.settings.get(f"{self.tool_name.lower()}_info", {}).get(
                f"{self.tool_name.lower()}_dir", None,
            )

        env_value = os.environ.get(f"{self.cache.main_tool_name.upper()}_{self.tool_name.upper()}_DIR")

        cli_value = None
        if f"--{self.tool_name.lower()}-dir" in sys.argv:
            idx = sys.argv.index(f"--{self.tool_name.lower()}-dir")
            if idx + 1 < len(sys.argv):
                cli_value = sys.argv[idx + 1]
            else:
                raise RuntimeError(f"you passed --{self.tool_name.lower()}-dir without a tag after")

        prioritized_value = next(
            v for v in [cli_value, env_value, config_value, default_value]
            if v not in (None, "")
        )

        if not prioritized_value:
            raise RuntimeError('get tool directory could not find a prioritized value')

        if isinstance(prioritized_value, str):
            prioritized_value = Path(prioritized_value)

        if not prioritized_value.is_absolute():
            return Path(str(settings_information.settings_json_dir.path), prioritized_value).resolve()
        else:
            return Path(prioritized_value).resolve()


    def is_current_preferred_tool_version_installed(self) -> bool:
        for tool in self.cache.tools.tool_entries:
            if tool.get_repo_name().lower() == self.repo_name.lower():
                for entry in tool.cache_entries:
                    if entry.release_tag == self.get_current_preferred_release_tag():
                        if entry.is_cache_valid():
                            return True
        return False


@dataclass
class CacheEntry:
    tool_name: str
    release_tag: str
    installed_files: list[Path]
    executable_path: Path
    file_to_download: str
    download_url: str

    def is_cache_valid(self) -> bool:
        return all(file.is_file() for file in self.installed_files)


@dataclass
class Tool:
    tool_repo_url: str
    cache_entries: list[CacheEntry]

    def get_repo_author(self) -> str:
        path = urlparse(self.tool_repo_url).path.strip('/')
        return path.split('/')[0] if '/' in path else ''

    def get_repo_name(self) -> str:
        path = urlparse(self.tool_repo_url).path.strip('/')
        return path.split('/')[1] if '/' in path else ''


@dataclass
class Tools:
    tool_entries: list[Tool]

    def to_toml_dict(self) -> dict:
            return {
                "tool_entries": [
                    {
                        "tool_repo_url": tool.tool_repo_url,
                        "cache_entries": [
                            {
                                "release_tag": entry.release_tag,
                                "installed_files": entry.installed_files,
                                "executable_path": entry.executable_path,
                                "download_url": entry.download_url,
                                "file_to_download": entry.file_to_download,
                            } for entry in tool.cache_entries
                        ],
                    } for tool in self.tool_entries
                ],
            }

    @staticmethod
    def from_toml_dict(data: dict) -> Tools:
        tools = []
        for tool_data in data.get("tool_entries", []):
            entries = [
                CacheEntry(
                    tool_name=entry['tool_name'],
                    release_tag=entry["release_tag"],
                    installed_files=entry["installed_files"],
                    executable_path=entry["executable_path"],
                    download_url=entry["download_url"],
                    file_to_download=entry["file_to_download"],
                )
                for entry in tool_data.get("cache_entries", [])
            ]
            tools.append(Tool(tool_repo_url=tool_data["tool_repo_url"], cache_entries=entries))
        return Tools(tool_entries=tools)


@dataclass
class ToolsCache:
    main_tool_name: str
    main_tool_author: str
    logging_function: typing.Callable = print
    is_online: bool | None = None
    cache_path: Path | None = None
    tools: Tools = field(default_factory=lambda: Tools(tool_entries=[]))


    def __post_init__(self) -> None:
       self.init_cache()


    def init_cache(self) -> None:
        cache_dir = self.get_cache_dir()
        self.logging_function(f'cache_directory: "{cache_dir}"')
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_settings_path = self.get_main_cache_settings_path()
        self.logging_function(f'cache_settings_file: "{cache_settings_path}"')
        if not cache_settings_path.is_file():
            with Path.open(cache_settings_path, 'w') as file:
                file.write('')
        self.clean_download_dir()
        self.tools = self.load_tools_from_toml_file()
        if self.is_online is None:
            self.init_is_online()


    def clean_download_dir(self) -> None:
        download_dir = self.get_download_dir()
        for file in download_dir.iterdir():
            if file.is_file():
                file.unlink()


    def get_download_dir(self) -> Path:
        download_dir = self.get_cache_dir() / "downloads"
        download_dir.mkdir(parents=True, exist_ok=True)
        return download_dir


    def get_cache_dir(self) -> Path:
        # check .env file here later for the value
        if self.get_no_cache_env_var_value() or was_no_cache_parameter_in_args():
            return self.get_local_cache_dir_path()

        if was_cache_dir_parameter_in_args():
            param_dir = get_cache_dir_param_in_args()
            if param_dir:
                return Path(param_dir)

        env_dir = self.get_cache_dir_env_var_value()
        if env_dir:
            return Path(env_dir)

        # check .env file here later for the value

        config_dir = self.cache_path
        if config_dir:
            return Path(config_dir)

        return self.get_default_cache_dir()


    def get_main_cache_settings_path(self) -> Path:
        return Path(self.get_cache_dir()) / 'cache.toml'


    def clean_cache(self) -> None:
        shutil.rmtree(self.get_cache_dir())
        self.init_cache()


    def get_tool_install_dir(self, repo_name: str, tool_name: str, version_tag: str) -> Path:
        if is_windows():
            platform_name = 'windows'
        elif is_linux():
            platform_name = 'linux'
        else:
            raise RuntimeError('You are on an unsupported os')
        return Path(self.get_cache_dir() / "tools" / repo_name / tool_name / platform_name / version_tag)


    def install_tool_to_cache(
            self,
            tool_info: ToolInfo,
        ) -> None:
        if not self.is_online:
            raise RuntimeError('You are not able to download tools to install to the cache when not connected to the web.')

        download_url = tool_info.get_download_url()
        download_dir = self.get_download_dir()
        file_to_download = download_dir / tool_info.get_file_to_download()
        executable_path = tool_info.get_executable_path()
        version_tag = tool_info.get_current_preferred_release_tag()

        # Download if missing
        # FIXME seems to not give back a full Path and just a str that is not a full path either
        if not file_to_download.is_file():
            self.logging_function(f"Downloading {download_url} to {file_to_download}...")
            response = requests.get(download_url, stream=True, timeout=15)
            response.raise_for_status()
            with Path.open(file_to_download, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            self.logging_function("  Download complete.")

        # Determine install directory
        install_dir = self.get_tool_install_dir(tool_info.repo_name, tool_info.tool_name, version_tag)
        install_dir.mkdir(parents=True, exist_ok=True)

        # Extract if needed
        unpacked_files = []
        if is_archive(file_to_download):
            unpacked_files = unpack_archive(file_to_download, install_dir)

            # this will need to check if the only thing in the zip root is a dir and unfolder it
            root_contents = list(install_dir.iterdir())
            if len(root_contents) == 1:
                single_item = Path(install_dir / root_contents[0])
                if single_item.is_dir():
                    self.logging_function(f"  Flattening {single_item} into {install_dir}...")
                    for item in single_item.iterdir():
                        shutil.move(Path(single_item / item), Path(install_dir / item))
                    shutil.rmtree(single_item)
                    unpacked_files = [Path(install_dir / f) for f in install_dir.iterdir()]

            self.logging_function(f"  Removed archive: {file_to_download}")
        else:
            # Direct file, not archive — just move to install_dir
            for path in tool_info.file_paths:
                dest = Path(install_dir / path.name)
                shutil.copy2(path, dest)
                unpacked_files.append(dest)
        file_to_download.unlink(missing_ok=True)

        # Register in cache
        tool = next(
            (t for t in self.tools.tool_entries if t.get_repo_name().lower() == tool_info.repo_name.lower()),
            None,
        )

        if tool is None:
            self.logging_function(f"Registering new tool '{tool_info.tool_name}' in cache")
            tool = Tool(
                tool_repo_url=f"https://github.com/{tool_info.repo_owner}/{tool_info.repo_name}",
                cache_entries=[],
            )
            self.tools.tool_entries.append(tool)

        # Prevent duplicate installs
        for existing in tool.cache_entries:
            if existing.release_tag == version_tag and existing.is_cache_valid():
                self.logging_function(f"{tool_info.tool_name} {version_tag} already installed")
                return

        self.logging_function(f"Installing {tool_info.tool_name} version {version_tag}...")

        entry = CacheEntry(
            tool_name=tool_info.tool_name,
            release_tag=version_tag,
            installed_files=unpacked_files,
            executable_path=executable_path,
            # file_to_download=str(file_to_download),
            file_to_download=file_to_download.name,
            download_url=download_url,
        )

        tool.cache_entries.append(entry)

        self.logging_function(f"  Installed to: {install_dir}")
        self.logging_function(f"  Total files installed: {len(unpacked_files)}")
        self.save_tools_to_toml_file()


    # FIXME probably doesn't work from a glance
    # needs to save after removing the entries as well
    # self.save_tools_to_toml_file()
    def uninstall_tool_from_cache(
        self,
        repo_name: str,
        tool_name: str,
        version_tag: str,
    ) -> None:
        for tool in self.tools.tool_entries:
            if tool.get_repo_name().lower() == repo_name.lower():

                found = False

                for entry in list(tool.cache_entries):  # safe iteration
                    if (
                        entry.tool_name.lower() == tool_name.lower()
                        and entry.release_tag == version_tag
                    ):
                        found = True
                        self.logging_function(
                            f"Uninstalling {tool_name} version {version_tag}...",
                        )

                        for file in entry.installed_files:
                            try:
                                file.unlink()
                                self.logging_function(f"  Removed: {file}")
                            except FileNotFoundError:
                                self.logging_function(f"  Not found: {file}")

                        tool.cache_entries.remove(entry)

                if not found:
                    self.logging_function(
                        f"[Warning] Version '{version_tag}' not found for '{tool_name}'.",
                    )
                else:
                    self.save_tools_to_toml_file()

                return

        self.logging_function(f"[Warning] Repo '{repo_name}' not found.")


    def prune_cache(self) -> None:
        self.logging_function("Pruning entire cache...")
        self.prune_all_tools()
        self.logging_function("Pruning complete.")


    def list_tools(self) -> None:
        self.logging_function("Available tools in cache:")
        for tool in self.tools.tool_entries:
            self.logging_function(f"- {tool.get_repo_name()} ({tool.tool_repo_url})")
            for entry in tool.cache_entries:
                self.logging_function(f"  └─ version: {entry.release_tag}")


    def get_no_cache_env_var_value(self) -> bool:
        return os.getenv(f'{self.main_tool_name.upper()}_NO_CACHE', '').lower() in ['1', 'true', 'yes']


    def get_cache_dir_env_var_value(self) -> Path | None:
        cache_dir = os.getenv(f'{self.main_tool_name.upper()}_CACHE_DIR')
        if cache_dir:
            return Path(cache_dir)
        return None


    def get_default_cache_dir(self) -> Path:
        return Path(platformdirs.user_cache_dir(appname=self.main_tool_name.lower(), appauthor=self.main_tool_author))


    def get_local_cache_dir_path(self) -> Path:
        return Path(SCRIPT_DIR / f'{self.main_tool_name.lower()}_cache')


    def get_tool_entry(self, tool_name: str) -> Tool:
        for tool in self.tools.tool_entries:
            if tool.get_repo_name().lower() == tool_name.lower():
                return tool
        self.logging_function(f"{tool_name} tool not found in cache. Please install it first.")
        raise RuntimeError('was unable to get the tool entry')


    def get_cache_entry(self, tool_name: str, tag: str) -> CacheEntry:
        tool = self.get_tool_entry(tool_name)
        if not tool:
            raise RuntimeError(f'invalid {tool_name} tool entry')
        for entry in tool.cache_entries:
            if entry.release_tag == tag:
                return entry
        raise RuntimeError(f"{tool_name} cache entry with tag '{tag}' not found.")


    def prune_all_tools(self) -> None:
        tool_entries = self.tools.tool_entries
        for tool in tool_entries:
            tool_name = tool.get_repo_name()
            repo_name = tool.get_repo_name()

            tool_cache_dir = self.get_cache_dir() / tool_name

            if tool_cache_dir.exists():
                self.prune_tool(tool_name, repo_name)
            else:
                self.logging_function(f"[Warning] Cache directory does not exist: {tool_cache_dir}")
        if len(tool_entries) > 0:
            self.save_tools_to_toml_file()


    def prune_single_tool(self, tool_name: str, repo_name: str) -> None:
        tool_entries = self.tools.tool_entries
        for tool in tool_entries:
            if tool.get_repo_name().lower() == tool_name.lower():
                tool_cache_dir = Path(self.get_cache_dir() / tool_name)
                if tool_cache_dir.exists():
                    self.prune_tool(tool_name, repo_name)
                else:
                    self.logging_function(f"[Warning] Cache directory does not exist: {tool_cache_dir}")
                return
        self.logging_function(f"[Warning] Tool '{tool_name}' not found in entries.")
        if len(tool_entries) > 0:
            self.save_tools_to_toml_file()


    def prune_multiple_tools(self, tool_names_to_repo_names: dict[str, str]) -> None:
        for tool_name in tool_names_to_repo_names:
            self.prune_single_tool(tool_name, tool_names_to_repo_names[tool_name])
        self.save_tools_to_toml_file()


    def prune_tool(self, tool_name: str, repo_name: str) -> None:
        valid_files = {f.resolve() for entry in self.get_tool_entry(tool_name).cache_entries for f in entry.installed_files}

        tool_dir = self.get_cache_dir() / "tools" / repo_name / tool_name
        for full_path in tool_dir.rglob("*"):
            if full_path.is_file() and full_path not in valid_files:
                full_path.unlink()
                self.logging_function(f"[Pruned] {full_path}")
        self.save_tools_to_toml_file()


    def log_online_status(self) -> None:
        if self.is_online:
            self.logging_function('Web Connectivity Status: Online')
        else:
            self.logging_function('Web Connectivity Status: Offline')


    def init_is_online(self, timeout: float = 1) -> None:
        force_online = _env_true(os.getenv(f"{self.main_tool_name.upper()}_CACHE_FORCE_ONLINE"))
        force_offline = _env_true(os.getenv(f"{self.main_tool_name.upper()}_CACHE_FORCE_OFFLINE"))

        if force_online:
            self.is_online = True
            self.log_online_status()
            return

        if force_offline:
            self.is_online = False
            self.log_online_status()
            return

        try:
            socket.create_connection(("8.8.8.8", 53), timeout=timeout)
            self.is_online = True
        except (socket.timeout, OSError):
            self.is_online = False

        self.log_online_status()


    def save_tools_to_toml_file(self) -> None:
        doc = document()
        entries = []

        for tool in self.tools.tool_entries:
            tool_table = table()
            tool_table["tool_repo_url"] = tool.tool_repo_url

            cache_entries = []
            for entry in tool.cache_entries:
                entry_table = table()
                entry_table["tool_name"] = entry.tool_name
                entry_table["release_tag"] = entry.release_tag
                entry_table["installed_files"] = [str(p) for p in entry.installed_files]
                entry_table["download_url"] = entry.download_url
                entry_table["executable_path"] = str(entry.executable_path)
                entry_table["file_to_download"] = entry.file_to_download
                cache_entries.append(entry_table)

            tool_table["cache_entries"] = cache_entries
            entries.append(tool_table)

        doc["tool_entries"] = entries

        with Path.open(self.get_main_cache_settings_path(), "w", encoding="utf-8") as f:
            f.write(dumps(doc))


    def load_tools_from_toml_file(self) -> Tools:
        with Path.open(self.get_main_cache_settings_path(), "r", encoding="utf-8") as f:
            data: TOMLDocument = loads(f.read())

        tool_entries = []
        for tool_data in data.get("tool_entries", []):
            cache_entries = [
                CacheEntry(
                    tool_name=entry['tool_name'],
                    release_tag=str(entry["release_tag"]),
                    installed_files=[Path(str(p)) for p in entry["installed_files"]],
                    executable_path=Path(str(entry["executable_path"])),
                    download_url=str(entry["download_url"]),
                    file_to_download=str(entry["file_to_download"]),
                )
                for entry in tool_data.get("cache_entries", [])
            ]
            tool = Tool(
                tool_repo_url=str(tool_data["tool_repo_url"]),
                cache_entries=cache_entries,
            )
            tool_entries.append(tool)

        return Tools(tool_entries=tool_entries)
