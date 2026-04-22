from __future__ import annotations
import os
import sys
import socket
import shutil
import typing
import zipfile
import tarfile
import platform
from pathlib import Path
from urllib.parse import urlparse
from dataclasses import dataclass, field

import requests
import platformdirs
from tomlkit.toml_document import TOMLDocument
from tomlkit import table, document, dumps, loads

from tempo_settings.tempo_settings import SettingsInformation


# override this with another script directory if desired
SCRIPT_DIR = (
    Path(sys.executable).parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)

# override this for when you want to have it specifiable via config
_cache_dir: Path | None = None
# override this with a function that takes in a string to replace the print out messages
logging_function: typing.Callable = print
has_inited: bool = False
is_online: bool = False
# set this to an instance of yours, assume is only read at program start, and not modified mid run
# it is mainly for config settings overrides
settings_information: SettingsInformation


def set_cache_dir_from_tempo_config_file(path: Path | None) -> None:
    global _cache_dir
    _cache_dir = path

def get_cache_dir_from_tempo_config_file() -> Path | None:
    return _cache_dir


def is_windows() -> bool:
    return platform.system() == "Windows"


def is_linux() -> bool:
    return platform.system() == "Linux"


def log_online_status() -> None:
    if is_online:
        logging_function('Web Connectivity Status: Online')
    else:
        logging_function('Web Connectivity Status: Offline')


def _env_true(value: str | None) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def init_is_online(timeout: float = 1) -> bool:
    """
    Determine online status with the following priority:
    1. TEMPO_CACHE_FORCE_ONLINE=true  -> always online
    2. TEMPO_CACHE_FORCE_OFFLINE=true -> always offline
    3. Otherwise, attempt socket connection
    """
    global has_inited
    global is_online

    if has_inited:
        return is_online

    force_online = _env_true(os.getenv("TEMPO_CACHE_FORCE_ONLINE"))
    force_offline = _env_true(os.getenv("TEMPO_CACHE_FORCE_OFFLINE"))

    if force_online:
        is_online = True
        log_online_status()
        has_inited = True
        return is_online

    if force_offline:
        is_online = False
        log_online_status()
        has_inited = True
        return is_online

    try:
        socket.create_connection(("8.8.8.8", 53), timeout=timeout)
        is_online = True
    except (socket.timeout, OSError):
        is_online = False

    log_online_status()

    has_inited = True
    return is_online


def is_current_preferred_tool_version_installed(tool_name: str) -> bool:
    if not isinstance(TempoCache, Tools):
        raise RuntimeError('Tempo cache seems to not have been inited, is not an instance of Tools')
    for tool in TempoCache.tool_entries:
        if tool.get_repo_name().lower() == tool_name:
            for entry in tool.cache_entries:
                if entry.is_cache_valid():
                    return True
    return False


@dataclass
class ToolInfo:
    tool_name: str
    repo_name: str
    repo_owner: str
    file_paths: list[Path] = field(default_factory=list)
    is_online: bool = field(default_factory=lambda: init_is_online())

    def __post_init__(self) -> None:
        global TempoCache

        if not isinstance(TempoCache, Tools):
            raise RuntimeError('Tempo cache seems to not have been inited, is not an instance of Tools')

        if not is_current_preferred_tool_version_installed(self.tool_name):
            install_tool_to_cache(TempoCache, self)


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

        env_value = os.environ.get(f'TEMPO_{self.tool_name.upper()}_RELEASE_TAG')

        cli_value = None
        if f'--{self.tool_name.lower()}-release-tag' in sys.argv:
            idx = sys.argv.index(f'--{self.tool_name.lower()}-release-tag')
            if idx + 1 < len(sys.argv):
                cli_value = sys.argv[idx + 1]
            else:
                raise RuntimeError(f'You passed --{self.tool_name.lower()}-release-tag without a tag after it.')

        prioritized_value = cli_value or env_value or config_value or default_value

        if prioritized_value == "latest":

            response = requests.get(f"https://api.github.com/repos/{self.repo_owner}/{self.repo_name}/releases/latest", timeout=5)
            response.raise_for_status()
            return response.json().get("tag_name", "latest")

        return prioritized_value


    def get_tool_directory(self) -> Path | None:
        global settings_information
        default_value = get_tool_install_dir(self.tool_name.lower(), self.get_current_preferred_release_tag())

        config_value = None
        if settings_information.settings:
            config_value = settings_information.settings.get(f"{self.tool_name.lower()}_info", {}).get(
                f"{self.tool_name.lower()}_dir", None,
            )

        env_value = os.environ.get(f"TEMPO_{self.tool_name.upper()}_DIR")

        cli_value = None
        if f"--{self.tool_name.lower()}-dir" in sys.argv:
            idx = sys.argv.index(f"--{self.tool_name.lower()}-dir")
            if idx + 1 < len(sys.argv):
                cli_value = sys.argv[idx + 1]
            else:
                raise RuntimeError(f"you passed --{self.tool_name.lower()}-dir without a tag after")

        prioritized_value = cli_value or env_value or config_value or default_value

        if not prioritized_value.is_absolute():
            return Path(str(settings_information.settings_json_dir.path), prioritized_value).resolve()
        else:
            return Path(prioritized_value).resolve()


@dataclass
class CacheEntry:
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

    def prune_tool(self, cache_directory: Path) -> None:
        valid_files = {f.resolve() for entry in self.cache_entries for f in entry.installed_files}

        for full_path in cache_directory.rglob("*"):
            if full_path.is_file() and full_path not in valid_files:
                full_path.unlink()
                logging_function(f"[Pruned] {full_path}")


@dataclass
class Tools:
    tool_entries: list[Tool]

    def prune_all_tools(self, cache_root: Path) -> None:
        for tool in self.tool_entries:
            repo_name = tool.get_repo_name()
            tool_cache_dir = Path(cache_root / repo_name)
            if tool_cache_dir.exists():
                tool.prune_tool(tool_cache_dir)

    def prune_single_tool(self, tool_name: str, cache_root: Path) -> None:
        for tool in self.tool_entries:
            if tool.get_repo_name() == tool_name:
                tool_cache_dir = Path(cache_root / tool_name)
                if tool_cache_dir.exists():
                    tool.prune_tool(tool_cache_dir)
                else:
                    logging_function(f"[Warning] Cache directory does not exist: {tool_cache_dir}")
                return
        logging_function(f"[Warning] Tool '{tool_name}' not found in entries.")


    def prune_multiple_tools(self, tool_names: list[str], cache_root: Path) -> None:
        for name in tool_names:
            self.prune_single_tool(name, cache_root)


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


def list_tools(tools: Tools) -> None:
    logging_function("Available tools in cache:")
    for tool in tools.tool_entries:
        logging_function(f"- {tool.get_repo_name()} ({tool.tool_repo_url})")
        for entry in tool.cache_entries:
            logging_function(f"  └─ version: {entry.release_tag}")


def prune_cache(tools: Tools, cache_root: Path) -> None:
    logging_function("Pruning entire cache...")
    tools.prune_all_tools(cache_root)
    logging_function("Pruning complete.")


def uninstall_tool_from_cache(tools: Tools, tool_name: str, version_tag: str, cache_root: Path) -> None:
    for tool in tools.tool_entries:
        if tool.get_repo_name() == tool_name:
            for entry in tool.cache_entries:
                if entry.release_tag == version_tag:
                    logging_function(f"Uninstalling {tool_name} version {version_tag}...")
                    for file in entry.installed_files:
                        try:
                            file.unlink()
                            logging_function(f"  Removed: {file}")
                        except FileNotFoundError:
                            logging_function(f"  Not found: {file}")
                    tool.cache_entries.remove(entry)
                    persist_cache()
                    return
            logging_function(f"[Warning] Version '{version_tag}' not found for '{tool_name}'.")
            return
    logging_function(f"[Warning] Tool '{tool_name}' not found.")


def is_archive(filename: Path) -> bool:
    return filename.suffix in ((
        '.zip',
        '.tar.gz',
        '.tgz',
        '.tar',
        '.tar.xz',
        '.txz',
    ))


def unpack_archive(archive_path: Path, extract_to: Path) -> list[Path]:
    extracted_files = []

    if archive_path.suffix == ".zip":
        with zipfile.ZipFile(archive_path, 'r') as zip_ref:
            zip_ref.extractall(extract_to)
            extracted_files = [Path(extract_to / f) for f in zip_ref.namelist()]

    elif archive_path.suffix in ((".tar.gz", ".tgz", ".tar", ".tar.xz", ".txz")):
        with tarfile.open(archive_path, 'r:*') as tar_ref:
            tar_ref.extractall(extract_to)
            extracted_files = [
                Path(extract_to / member.name)
                for member in tar_ref.getmembers() if member.isfile()
            ]

    else:
        raise ValueError(f"Unsupported archive format: {archive_path}")

    return extracted_files


def get_tool_install_dir(tool_name: str, version_tag: str) -> Path:
    if is_windows():
        platform_name = 'windows'
    elif is_linux():
        platform_name = 'linux'
    else:
        raise RuntimeError('You are on an unsupported os')
    return Path(get_cache_dir() / "tools" / tool_name / platform_name / version_tag)


def install_tool_to_cache(
        tools: Tools,
        tool_info: ToolInfo,
    ) -> None:
    if not is_online:
        raise RuntimeError('You are not able to download tools to install to the cache when not connected to the web.')

    download_url = tool_info.get_download_url()
    file_to_download = Path(tool_info.get_file_to_download())
    executable_path = tool_info.get_executable_path()
    version_tag = tool_info.get_current_preferred_release_tag()

    # Download if missing
    # FIXME seems to not give back a full Path and just a str that is not a full path either
    if not file_to_download.is_file():
        logging_function(f"Downloading {download_url} to {file_to_download}...")
        response = requests.get(download_url, stream=True, timeout=15)
        response.raise_for_status()
        with Path.open(file_to_download, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        logging_function("  Download complete.")

    # Determine install directory
    install_dir = get_tool_install_dir(tool_info.tool_name, version_tag)
    install_dir.mkdir(parents=True)

    # Extract if needed
    unpacked_files = []
    if is_archive(file_to_download):
        unpacked_files = unpack_archive(file_to_download, install_dir)

        # this will need to check if the only thing in the zip root is a dir and unfolder it
        root_contents = list(install_dir.iterdir())
        if len(root_contents) == 1:
            single_item = Path(install_dir / root_contents[0])
            if single_item.is_dir():
                logging_function(f"  Flattening {single_item} into {install_dir}...")
                for item in single_item.iterdir():
                    shutil.move(Path(single_item / item), Path(install_dir / item))
                shutil.rmtree(single_item)
                unpacked_files = [Path(install_dir / f) for f in install_dir.iterdir()]

        file_to_download.unlink()
        logging_function(f"  Removed archive: {file_to_download}")
    else:
        # Direct file, not archive — just move to install_dir
        for path in tool_info.file_paths:
            dest = Path(install_dir / path.name)
            shutil.copy2(path, dest)
            unpacked_files.append(dest)

    # Register in cache
    tool = next(
        (t for t in tools.tool_entries if t.get_repo_name().lower() == tool_info.tool_name.lower()),
        None,
    )

    if tool is None:
        logging_function(f"Registering new tool '{tool_info.tool_name}' in cache")
        tool = Tool(
            tool_repo_url=f"https://github.com/Tempo-Organization/{tool_info.tool_name}",
            cache_entries=[],
        )
        tools.tool_entries.append(tool)

    # Prevent duplicate installs
    for existing in tool.cache_entries:
        if existing.release_tag == version_tag and existing.is_cache_valid():
            logging_function(f"{tool_info.tool_name} {version_tag} already installed")
            return

    logging_function(f"Installing {tool_info.tool_name} version {version_tag}...")

    entry = CacheEntry(
        release_tag=version_tag,
        installed_files=unpacked_files,
        executable_path=executable_path,
        file_to_download=str(file_to_download),
        download_url=download_url,
    )

    tool.cache_entries.append(entry)
    persist_cache()

    logging_function(f"  Installed to: {install_dir}")
    logging_function(f"  Total files installed: {len(unpacked_files)}")


def save_tools_to_toml_file(tools: Tools, filepath: Path) -> None:
    doc = document()
    entries = []

    for tool in tools.tool_entries:
        tool_table = table()
        tool_table["tool_repo_url"] = tool.tool_repo_url

        cache_entries = []
        for entry in tool.cache_entries:
            entry_table = table()
            entry_table["release_tag"] = entry.release_tag
            entry_table["installed_files"] = entry.installed_files
            entry_table["download_url"] = entry.download_url
            entry_table["executable_path"] = entry.executable_path
            entry_table["file_to_download"] = entry.file_to_download
            cache_entries.append(entry_table)

        tool_table["cache_entries"] = cache_entries
        entries.append(tool_table)

    doc["tool_entries"] = entries

    with Path.open(filepath, "w", encoding="utf-8") as f:
        f.write(dumps(doc))

def load_tools_from_toml_file(filepath: Path) -> Tools:
    with Path.open(filepath, "r", encoding="utf-8") as f:
        data: TOMLDocument = loads(f.read())

    tool_entries = []
    for tool_data in data.get("tool_entries", []):
        cache_entries = [
            CacheEntry(
                release_tag=entry["release_tag"],
                installed_files=entry["installed_files"],
                executable_path=entry["executable_path"],
                download_url=entry["download_url"],
                file_to_download=entry["file_to_download"],
            )
            for entry in tool_data.get("cache_entries", [])
        ]
        tool = Tool(
            tool_repo_url=tool_data["tool_repo_url"],
            cache_entries=cache_entries,
        )
        tool_entries.append(tool)

    return Tools(tool_entries=tool_entries)


def clean_cache() -> None:
    shutil.rmtree(get_cache_dir())
    init_cache()


def get_tempo_no_cache_env_var_value() -> bool:
    return os.getenv('TEMPO_NO_CACHE', '').lower() in ['1', 'true', 'yes']


def get_tempo_cache_dir_env_var_value() -> Path | None:
    cache_dir = os.getenv('TEMPO_CACHE_DIR')
    if cache_dir:
        return Path(cache_dir)
    return None


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


def get_default_cache_dir() -> Path:
    return Path(platformdirs.user_cache_dir(appname='tempo', appauthor='Tempo-Organization'))


def get_cache_dir() -> Path:
    # check .env file here later for the value
    if get_tempo_no_cache_env_var_value() or was_no_cache_parameter_in_args():
        return get_local_cache_dir_path()

    if was_cache_dir_parameter_in_args():
        param_dir = get_cache_dir_param_in_args()
        if param_dir:
            return Path(param_dir)

    env_dir = get_tempo_cache_dir_env_var_value()
    if env_dir:
        return Path(env_dir)

    # check .env file here later for the value

    config_dir = get_cache_dir_from_tempo_config_file()
    if config_dir:
        return Path(config_dir)

    return get_default_cache_dir()


def get_main_cache_settings_file() -> Path:
    return Path(get_cache_dir() / 'cache.toml')


def get_local_cache_dir_path() -> Path:
    # maybe make the script dir an arg later?
    return Path(SCRIPT_DIR / 'tempo_cache')


class _UninitializedCache:
    def __getattr__(self, name: str) -> None:
        raise NotImplementedError("ToolsCache is not initialized. Call init_cache() first.")

    def __getitem__(self, key: str) -> None:
        raise NotImplementedError("ToolsCache is not initialized. Call init_cache() first.")

    def __bool__(self) -> None:
        raise NotImplementedError("ToolsCache is not initialized. Call init_cache() first.")


TempoCache = _UninitializedCache()

def init_cache() -> None:
    cache_dir = get_cache_dir()
    logging_function(f'cache_directory: "{get_cache_dir()}"')
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = get_main_cache_settings_file()
    logging_function(f'cache_settings_file: "{cache}"')
    if not cache.is_file():
        with Path.open(cache, 'w') as file:
            file.write('')
    global TempoCache
    TempoCache = load_tools_from_toml_file(cache)


def get_tool_entry(tool_name: str) -> Tool | None:
    if not isinstance(TempoCache, Tools):
        raise RuntimeError("Cache not initialized")
    for tool in TempoCache.tool_entries:
        if tool.get_repo_name().lower() == tool_name:
            return tool
    logging_function(f"{tool_name} tool not found in cache. Please install it first.")
    return None


def get_cache_entry(tool_name: str, tag: str) -> CacheEntry:
    tool = get_tool_entry(tool_name)
    if not tool:
        raise RuntimeError(f'invalid {tool_name} tool entry')
    for entry in tool.cache_entries:
        if entry.release_tag == tag:
            return entry
    raise RuntimeError(f"{tool_name} cache entry with tag '{tag}' not found.")


def persist_cache() -> None:
    if not isinstance(TempoCache, Tools):
        raise RuntimeError("Cache not initialized")
    save_tools_to_toml_file(TempoCache, get_main_cache_settings_file())
