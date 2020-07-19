"""Main Ambianic server module."""
import logging
import logging.handlers
import os
import pathlib
import time

from ambianic.pipeline import timeline
from ambianic.pipeline.interpreter import PipelineServer
from ambianic.util import ServiceExit
from ambianic.config_mgm import ConfigChangedEvent
from ambianic import config_manager
from ambianic.webapp.flaskr import FlaskServer

log = logging.getLogger(__name__)


AI_MODELS_DIR = "ai_models"
DEFAULT_LOG_LEVEL = logging.INFO
MANAGED_SERVICE_HEARTBEAT_THRESHOLD = 180  # seconds
MAIN_HEARTBEAT_LOG_INTERVAL = 5
ROOT_SERVERS = {
    'pipelines': PipelineServer,
    'web': FlaskServer,
}


def _configure_logging(config=None):
    default_log_level = DEFAULT_LOG_LEVEL
    if config is None:
        config = {}
    log_level = config.get("level", None)
    numeric_level = default_log_level
    if log_level:
        try:
            numeric_level = getattr(logging, log_level.upper(),
                                    DEFAULT_LOG_LEVEL)
        except AttributeError as e:
            log.warning("Invalid log level: %s . Error: %s", log_level, e)
            log.warning('Defaulting log level to %s', default_log_level)
    fmt = None
    if numeric_level <= logging.INFO:
        format_cfg = '%(asctime)s %(levelname)-4s ' \
            '%(pathname)s.%(funcName)s(%(lineno)d): %(message)s'
        datefmt_cfg = '%Y-%m-%d %H:%M:%S'
        fmt = logging.Formatter(fmt=format_cfg,
                                datefmt=datefmt_cfg, style='%')
    else:
        fmt = logging.Formatter()
    root_logger = logging.getLogger()
    # remove any other handlers that may be assigned previously
    # and could cause unexpected log collisions
    root_logger.handlers = []
    # add a console handler that only shows errors and warnings
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    # add formatter to ch
    ch.setFormatter(fmt)
    # add ch to logger
    root_logger.addHandler(ch)
    # add a file handler if configured
    log_filename = config.get('file', None)
    if log_filename:
        log_directory = os.path.dirname(log_filename)
        with pathlib.Path(log_directory) as log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)
            print("Log messages directed to {}".format(log_filename))
        handler = logging.handlers.RotatingFileHandler(
            log_filename,
            # each log file will be up to 10MB in size
            maxBytes=100*1024*1024,
            # 20 backup files will be kept. Older will be erased.
            backupCount=20
        )
        handler.setFormatter(fmt)
        root_logger.addHandler(handler)
    root_logger.setLevel(numeric_level)
    effective_level = log.getEffectiveLevel()
    assert numeric_level == effective_level
    log.info('Logging configured with level %s',
             logging.getLevelName(effective_level))
    if effective_level <= logging.DEBUG:
        log.debug('Configuration yaml dump:')
        log.debug(config)


def _configure(env_work_dir=None):
    """Load configuration settings.

    :returns config dict if configuration was loaded without issues.
            None or a specific exception otherwise.
    """
    assert env_work_dir, 'Working directory required.'
    assert os.path.exists(env_work_dir), \
        'working directory invalid: {}'.format(env_work_dir)

    config_manager.stop()

    config = config_manager.load(env_work_dir)
    if config is None:
        return None

    def logging_config_handler(event: ConfigChangedEvent):
        # configure logging
        log.info("Reconfiguring logging")
        _configure_logging(config.get("logging"))

    def timeline_config_handler(event: ConfigChangedEvent):
        # configure pipeline timeline event log
        log.info("Reconfiguring pipeline timeline event log")
        timeline.configure_timeline(config.get("timeline"))

    # set callback to react to specific configuration changes
    if config.get("logging", None) is not None:
        config.get("logging").add_callback(logging_config_handler)

    if config.get("timeline", None) is not None:
        config.get("timeline").add_callback(timeline_config_handler)

    # initialize logging
    logging_config_handler(None)
    timeline_config_handler(None)

    return config


class AmbianicServer:
    """Ambianic main server."""

    def __init__(self, work_dir=None):
        """Inititalize server from working directory files.

        :Parameters:
        ----------
        work_dir : string
            The working directory where config and data reside.

        """
        assert work_dir
        self._env_work_dir = work_dir
        # array of managed specialized servers
        self._servers = {}
        self._service_exit_requested = False
        self._latest_heartbeat = time.monotonic()

    def _stop_servers(self, servers):
        log.debug('Stopping servers...')
        for srv in servers.values():
            srv.stop()
        config_manager.stop()

    def _healthcheck(self, servers):
        """Check the health of managed servers."""
        for s in servers.values():
            latest_heartbeat, status = s.healthcheck()
            now = time.monotonic()
            lapse = now - latest_heartbeat
            if lapse > 1:
                # log only if lapse is over 1 second long.
                # otherwise things are OK and we don't want
                # unnecessary log noise
                log.debug('lapse for %s is %f', s.__class__.__name__, lapse)
            if lapse > MANAGED_SERVICE_HEARTBEAT_THRESHOLD:
                log.warning('Server "%s" is not responsive. '
                            'Latest heart beat was %f seconds ago. '
                            'Will send heal signal.',
                            s.__class__.__name__, lapse)
                s.heal()

    def _log_heartbeat(self):
        log.info("Main thread alive.")

    def _heartbeat(self):
        new_time = time.monotonic()
        # print a heartbeat message every so many seconds
        if new_time - self._latest_heartbeat > MAIN_HEARTBEAT_LOG_INTERVAL:
            self._log_heartbeat()
            # this is where hooks to external
            # monitoring services will come in
        self._latest_heartbeat = new_time
        if self._service_exit_requested:
            raise ServiceExit

    def on_config_change(self, event: ConfigChangedEvent):

        root = event.get_root()

        if not root or not root.get_context():
            return

        log.info("Config change: %s", event)

        section_name = root.get_context().get_name()

        if section_name in ["data_dir"]:
            self.stop()
            self.start()

    def start(self):
        """Programmatic start of the main service."""
        log.info('Configuring Ambianic server...')
        config = _configure(self._env_work_dir)
        if not config:
            log.info('No startup configuration provided. '
                     'Proceeding with defaults.')
        else:
            config.add_callback(self.on_config_change)

        log.info('Starting Ambianic server...')

        # Register the signal handlers
        servers = {}
        # Start the job threads
        try:
            for s_name, s_class in ROOT_SERVERS.items():
                srv = s_class(config=config)
                srv.start()
                servers[s_name] = srv

            self._latest_heartbeat = time.monotonic()

            self._servers = servers
            # Keep the main thread running, otherwise signals are ignored.
            while True:
                time.sleep(0.5)
                self._healthcheck(servers)
                self._heartbeat()
                log.debug('Watchdog')

        except ServiceExit:
            log.info('Service exit requested.')
            log.debug('Cleaning up before exit...')
            self._stop_servers(servers)

        log.info('Exiting Ambianic server.')
        return True

    def stop(self):
        """Programmatic stop of the main service."""
        print("Stopping server...")
        log.info("Stopping server...")
        self._service_exit_requested = True
