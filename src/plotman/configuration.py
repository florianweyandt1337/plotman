import contextlib
import importlib
import os
import stat
import tempfile
import textwrap
from typing import Dict, Generator, List, Mapping, Optional

import appdirs
import attr
import desert
# TODO: should be a desert.ib() but mypy doesn't understand it then, see below
import desert._make
import marshmallow
import pendulum
import yaml

from plotman import resources as plotman_resources


class ConfigurationException(Exception):
    """Raised when plotman.yaml configuration is missing or malformed."""


def get_path() -> str:
    """Return path to where plotman.yaml configuration file should exist."""
    config_dir: str = appdirs.user_config_dir("plotman")
    return config_dir + "/plotman.yaml"


def read_configuration_text(config_path: str) -> str:
    try:
        with open(config_path, "r") as file:
            return file.read()
    except FileNotFoundError as e:
        raise ConfigurationException(
            f"No 'plotman.yaml' file exists at expected location: '{config_path}'. To generate "
            f"default config file, run: 'plotman config generate'"
        ) from e


def get_validated_configs(config_text: str, config_path: str, preset_target_definitions_text: str) -> "PlotmanConfig":
    """Return a validated instance of PlotmanConfig with data from plotman.yaml

    :raises ConfigurationException: Raised when plotman.yaml is either missing or malformed
    """
    schema = desert.schema(PlotmanConfig)
    config_objects = yaml.load(config_text, Loader=yaml.SafeLoader)

    version = config_objects.get('version', (0,))

    expected_major_version = 1

    if version[0] != expected_major_version:
        message = textwrap.dedent(f"""\
            Expected major version {expected_major_version}, found version {version}
                See https://github.com/ericaltendorf/plotman/wiki/Configuration#versions
        """)

        raise Exception(message)

    loaded: PlotmanConfig
    try:
        loaded = schema.load(config_objects)
    except marshmallow.exceptions.ValidationError as e:
        raise ConfigurationException(
            f"Config file at: '{config_path}' is malformed"
        ) from e

    if loaded.archiving is not None:
        preset_target_objects = yaml.safe_load(preset_target_definitions_text)
        preset_target_schema = desert.schema(PresetTargetDefinitions)
        preset_target_definitions = preset_target_schema.load(preset_target_objects)

        loaded.archiving.target_definitions = {
            **preset_target_definitions.target_definitions,
            **loaded.archiving.target_definitions,
        }

    return loaded

class CustomStringField(marshmallow.fields.String):
    def _deserialize(self, value: object, attr: Optional[str], data: Optional[Mapping[str, object]], **kwargs: Dict[str, object]) -> str:
        if isinstance(value, int):
            value = str(value)

        return super()._deserialize(value, attr, data, **kwargs)  # type: ignore[no-any-return]

# Data models used to deserializing/formatting plotman.yaml files.

# TODO: bah, mutable?  bah.
@attr.mutable
class ArchivingTarget:
    transfer_process_name: str
    transfer_process_argument_prefix: str
    # TODO: mutable attribute...
    # TODO: should be a desert.ib() but mypy doesn't understand it then
    env: Dict[str, Optional[str]] = attr.ib(
        factory=dict,
        metadata={
            desert._make._DESERT_SENTINEL: {
                'marshmallow_field': marshmallow.fields.Dict(
                    keys=marshmallow.fields.String(),
                    values=CustomStringField(allow_none=True),
                )
            },
        },
    )
    disk_space_path: Optional[str] = None
    disk_space_script: Optional[str] = None
    transfer_path: Optional[str] = None
    transfer_script: Optional[str] = None

@attr.frozen
class PresetTargetDefinitions:
    target_definitions: Dict[str, ArchivingTarget] = attr.ib(factory=dict)

# TODO: bah, mutable?  bah.
@attr.mutable
class Archiving:
    target: str
    # TODO: mutable attribute...
    # TODO: should be a desert.ib() but mypy doesn't understand it then
    env: Dict[str, str] = attr.ib(
        factory=dict,
        metadata={
            desert._make._DESERT_SENTINEL: {
                'marshmallow_field': marshmallow.fields.Dict(
                    keys=marshmallow.fields.String(),
                    values=CustomStringField(),
                )
            },
        },
    )
    index: int = 0  # If not explicit, "index" will default to 0
    target_definitions: Dict[str, ArchivingTarget] = attr.ib(factory=dict)

    def target_definition(self) -> ArchivingTarget:
        return self.target_definitions[self.target]

    def environment(
            self,
            source: Optional[str] = None,
            destination: Optional[str] = None,
    ) -> Dict[str, str]:
        target = self.target_definition()
        maybe_complete = {**target.env, **self.env}

        complete = {
            key: value
            for key, value in maybe_complete.items()
            if value is not None
        }

        if len(complete) != len(maybe_complete):
            missing_mandatory_keys = sorted(maybe_complete.keys() - complete.keys())
            target_repr = repr(self.target)
            missing = ', '.join(repr(key) for key in missing_mandatory_keys)
            message = f'Missing env options for archival target {target_repr}: {missing}'
            raise Exception(message)

        variables = {**os.environ, **complete}
        complete['process_name'] = target.transfer_process_name.format(**variables)

        if source is not None:
            complete['source'] = source

        if destination is not None:
            complete['destination'] = destination

        return complete

    def maybe_create_scripts(self, temp: str) -> None:
        rwx = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
        target = self.target_definition()

        if target.disk_space_path is None:
            if target.disk_space_script is None:
                raise Exception(f"One of `disk_space_path` or `disk_space_script` must be specified.  Using target {self.target!r}")

            with tempfile.NamedTemporaryFile(
                mode='w',
                encoding='utf-8',
                prefix='plotman-disk-space-script',
                delete=False,
                dir=temp,
            ) as disk_space_script_file:
                disk_space_script_file.write(target.disk_space_script)

            target.disk_space_path = disk_space_script_file.name
            os.chmod(target.disk_space_path, rwx)

        if target.transfer_path is None:
            if target.transfer_script is None:
                raise Exception(f"One of `transfer_path` or `transfer_script` must be specified.  Using target {self.target!r}")

            with tempfile.NamedTemporaryFile(
                mode='w',
                encoding='utf-8',
                prefix='plotman-transfer-script',
                delete=False,
                dir=temp,
            ) as transfer_script_file:
                transfer_script_file.write(target.transfer_script)

            target.transfer_path = transfer_script_file.name
            os.chmod(target.transfer_path, rwx)

@attr.frozen
class TmpOverrides:
    tmpdir_max_jobs: Optional[int] = None

@attr.frozen
class Logging:
    plots: str = os.path.join(appdirs.user_data_dir("plotman"), 'plots')
    transfers: str = os.path.join(appdirs.user_data_dir("plotman"), 'transfers')
    application: str = os.path.join(appdirs.user_log_dir("plotman"), 'plotman.log')

    def setup(self) -> None:
        os.makedirs(self.plots, exist_ok=True)
        os.makedirs(self.transfers, exist_ok=True)
        os.makedirs(os.path.dirname(self.application), exist_ok=True)

    def create_plot_log_path(self, time: pendulum.DateTime) -> str:
        return self._create_log_path(
            time=time,
            directory=self.plots,
            group='plot',
        )

    def create_transfer_log_path(self, time: pendulum.DateTime) -> str:
        return self._create_log_path(
            time=time,
            directory=self.transfers,
            group='transfer',
        )

    def _create_log_path(self, time: pendulum.DateTime, directory: str, group: str) -> str:
        timestamp = time.isoformat(timespec='microseconds').replace(':', '_')
        return os.path.join(directory, f'{timestamp}.{group}.log')

@attr.frozen
class Directories:
    tmp: List[str]
    dst: Optional[List[str]] = None
    tmp2: Optional[str] = None
    tmp_overrides: Optional[Dict[str, TmpOverrides]] = None

    def dst_is_tmp(self) -> bool:
        return self.dst is None and self.tmp2 is None

    def dst_is_tmp2(self) -> bool:
        return self.dst is None and self.tmp2 is not None

    def get_dst_directories(self) -> List[str]:
        """Returns either <Directories.dst> or <Directories.tmp>. If
        Directories.dst is None, Use Directories.tmp as dst directory.
        """
        if self.dst_is_tmp2():
            return [self.tmp2]  # type: ignore[list-item]
        elif self.dst_is_tmp():
            return self.tmp

        return self.dst  # type: ignore[return-value]

@attr.frozen
class Scheduling:
    global_max_jobs: int
    global_stagger_m: int
    polling_time_s: int
    tmpdir_max_jobs: int
    tmpdir_stagger_phase_major: int
    tmpdir_stagger_phase_minor: int
    tmpdir_stagger_phase_limit: int = 1  # If not explicit, "tmpdir_stagger_phase_limit" will default to 1

@attr.frozen
class Plotting:
    k: int
    e: bool
    n_threads: int
    n_buckets: int
    job_buffer: int
    farmer_pk: Optional[str] = None
    pool_pk: Optional[str] = None
    pool_contract_address: Optional[str] = None
    x: bool = False

@attr.frozen
class UserInterface:
    use_stty_size: bool = True

@attr.frozen
class Interactive:
    autostart_plotting: bool = True
    autostart_archiving: bool = True

@attr.frozen
class Commands:
    interactive: Interactive = attr.ib(factory=Interactive)

@attr.frozen
class PlotmanConfig:
    directories: Directories
    scheduling: Scheduling
    plotting: Plotting
    commands: Commands = attr.ib(factory=Commands)
    logging: Logging = Logging()
    archiving: Optional[Archiving] = None
    user_interface: UserInterface = attr.ib(factory=UserInterface)
    version: List[int] = [0]

    @contextlib.contextmanager
    def setup(self) -> Generator[None, None, None]:
        prefix = f'plotman-pid_{os.getpid()}-'

        self.logging.setup()

        with tempfile.TemporaryDirectory(prefix=prefix) as temp:
            if self.archiving is not None:
                self.archiving.maybe_create_scripts(temp=temp)

            yield
