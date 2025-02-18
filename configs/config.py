
import argparse, yaml, json
import gspread
from loguru import logger
from selenium import webdriver
from dataclasses import asdict
from selenium.common.exceptions import TimeoutException

from utils import GWorksheet, getattr_or
from .wayback_config import WaybackConfig
from .telethon_config import TelethonConfig
from .selenium_config import SeleniumConfig
from .vk_config import VkConfig
from .twitter_api_config import TwitterApiConfig
from storages import S3Config, S3Storage, GDStorage, GDConfig, LocalStorage, LocalConfig


class Config:
    """
    Controls the current execution parameters and manages API configurations
    Usage:
      c = Config() # initializes the argument parser
      c.parse() # parses the values and initializes the Services and API clients
      # you can then access the Services and APIs like 'c.s3_config'
    All the configurations available as cmd line options, when included, will 
    override the configurations in the config.yaml file.
    Configurations are split between:
    1. "secrets" containing API keys for generating services - not kept in memory
    2. "execution" containing specific execution configurations
    """
    AVAILABLE_STORAGES = {"s3", "gd", "local"}

    def __init__(self):
        self.parser = self.get_argument_parser()
        self.folder = ""

    def parse(self):
        self.args = self.parser.parse_args()
        logger.success(f'Command line arguments parsed successfully')
        self.config_file = self.args.config
        self.read_config_yaml()
        logger.info(f'APIs and Services initialized:\n{self}')

    def read_config_yaml(self):
        with open(self.config_file, "r", encoding="utf-8") as inf:
            self.config = yaml.safe_load(inf)

        # ----------------------EXECUTION - execution configurations
        execution = self.config.get("execution", {})

        self.sheet = getattr_or(self.args, "sheet", execution.get("sheet"))
        assert self.sheet is not None, "'sheet' must be provided either through command line or configuration file"
        self.header = int(getattr_or(self.args, "header", execution.get("header", 1)))
        self.storage = getattr_or(self.args, "storage", execution.get("storage", "s3"))
        self.save_logs = getattr(self.args, "save_logs") or execution.get("save_logs", False)
        if self.save_logs:
            self.set_log_files()
        self.check_if_exists = getattr(self.args, "check_if_exists") or execution.get("check_if_exists", False)

        # Column names come from config and can be overwritten by CMD
        # in the end all are considered as lower case
        config_column_names = execution.get("column_names", {})
        self.column_names = {}
        for k in GWorksheet.COLUMN_NAMES.keys():
            self.column_names[k] = getattr_or(self.args, k, config_column_names.get(k, GWorksheet.COLUMN_NAMES[k])).lower()

        # selenium driver
        selenium_configs = execution.get("selenium", {})
        self.selenium_config = SeleniumConfig(
            timeout_seconds=int(selenium_configs.get("timeout_seconds", SeleniumConfig.timeout_seconds)),
            window_width=int(selenium_configs.get("window_width", SeleniumConfig.window_width)),
            window_height=int(selenium_configs.get("window_height", SeleniumConfig.window_height))
        )
        self.webdriver = "not initialized"

        # ---------------------- SECRETS - APIs and service configurations
        secrets = self.config.get("secrets", {})

        # assert selected storage credentials exist
        for key, name in [("s3", "s3"), ("gd", "google_drive"), ("local", "local")]:
            assert self.storage != key or name in secrets, f"selected storage '{key}' requires secrets.'{name}' in {self.config_file}"

        # google sheets config
        self.gsheets_client = gspread.service_account(
            filename=secrets.get("google_sheets", {}).get("service_account", 'service_account.json')
        )

        # facebook config
        self.facebook_cookie = secrets.get("facebook", {}).get("cookie", None)

        # s3 config
        if "s3" in secrets:
            s3 = secrets["s3"]
            self.s3_config = S3Config(
                bucket=s3["bucket"],
                region=s3["region"],
                key=s3["key"],
                secret=s3["secret"],
                endpoint_url=s3.get("endpoint_url", S3Config.endpoint_url),
                cdn_url=s3.get("cdn_url", S3Config.cdn_url),
                key_path=s3.get("key_path", S3Config.key_path),
                private=getattr_or(self.args, "s3-private", s3.get("private", S3Config.private))
            )

        # GDrive config
        if "google_drive" in secrets:
            gd = secrets["google_drive"]
            self.gd_config = GDConfig(
                root_folder_id=gd.get("root_folder_id"),
                service_account=gd.get("service_account", GDConfig.service_account)
            )

        if "local" in secrets:
            self.local_config = LocalConfig(
                save_to=secrets["local"].get("save_to", LocalConfig.save_to),
            )

        # wayback machine config
        if "wayback" in secrets:
            self.wayback_config = WaybackConfig(
                key=secrets["wayback"]["key"],
                secret=secrets["wayback"]["secret"],
            )
        else:
            self.wayback_config = None
            logger.debug(f"'wayback' key not present in the {self.config_file=}")

        # telethon config
        if "telegram" in secrets:
            self.telegram_config = TelethonConfig(
                api_id=secrets["telegram"]["api_id"],
                api_hash=secrets["telegram"]["api_hash"],
                bot_token=secrets["telegram"].get("bot_token", None)
            )
        else:
            self.telegram_config = None
            logger.debug(f"'telegram' key not present in the {self.config_file=}")

        # twitter config
        if "twitter" in secrets:
            self.twitter_config = TwitterApiConfig(
                bearer_token=secrets["twitter"].get("bearer_token"),
                consumer_key=secrets["twitter"].get("consumer_key"),
                consumer_secret=secrets["twitter"].get("consumer_secret"),
                access_token=secrets["twitter"].get("access_token"),
                access_secret=secrets["twitter"].get("access_secret"),
            )
        else:
            self.twitter_config = None
            logger.debug(f"'twitter' key not present in the {self.config_file=}")

        # vk config
        if "vk" in secrets:
            self.vk_config = VkConfig(
                username=secrets["vk"]["username"],
                password=secrets["vk"]["password"]
            )
        else:
            self.vk_config = None
            logger.debug(f"'vk' key not present in the {self.config_file=}")

        del self.config["secrets"]  # delete to prevent leaks

    def set_log_files(self):
        # called only when config.execution.save_logs=true
        logger.add("logs/1trace.log", level="TRACE")
        logger.add("logs/2info.log", level="INFO")
        logger.add("logs/3success.log", level="SUCCESS")
        logger.add("logs/4warning.log", level="WARNING")
        logger.add("logs/5error.log", level="ERROR")

    def get_argument_parser(self):
        """
        Creates the CMD line arguments. 'python auto_archive.py --help'
        """
        parser = argparse.ArgumentParser(description='Automatically archive social media posts, videos, and images from a Google Sheets document. The command line arguments will always override the configurations in the provided YAML config file (--config), only some high-level options are allowed via the command line and the YAML configuration file is the preferred method. The sheet must have the "url" and "status" for the archiver to work. ')

        parser.add_argument('--config', action='store', dest='config', help='the filename of the YAML configuration file (defaults to \'config.yaml\')', default='config.yaml')
        parser.add_argument('--storage', action='store', dest='storage', help='which storage to use [execution.storage in config.yaml]', choices=Config.AVAILABLE_STORAGES)
        parser.add_argument('--sheet', action='store', dest='sheet', help='the name of the google sheets document [execution.sheet in config.yaml]')
        parser.add_argument('--header', action='store', dest='header', help='1-based index for the header row [execution.header in config.yaml]')
        parser.add_argument('--check-if-exists', action='store_true', dest='check_if_exists', help='when possible checks if the URL has been archived before and does not archive the same URL twice [exceution.check_if_exists]')
        parser.add_argument('--save-logs', action='store_true', dest='save_logs', help='creates or appends execution logs to files logs/LEVEL.log [exceution.save_logs]')
        parser.add_argument('--s3-private', action='store_true', help='Store content without public access permission (only for storage=s3) [secrets.s3.private in config.yaml]')

        for k, v in GWorksheet.COLUMN_NAMES.items():
            help = f"the name of the column to FILL WITH {k} (default='{v}')"
            if k in ["url", "folder"]:
                help = f"the name of the column to READ {k} FROM (default='{v}')"
            parser.add_argument(f'--col-{k}', action='store', dest=k, help=help)

        return parser

    def set_folder(self, folder):
        """
        update the folder in each of the storages
        """
        self.folder = folder
        # s3
        if hasattr(self, "s3_config"): self.s3_config.folder = folder
        if hasattr(self, "s3_storage"): self.s3_storage.folder = folder
        # gdrive
        if hasattr(self, "gd_config"): self.gd_config.folder = folder
        if hasattr(self, "gd_storage"): self.gd_storage.folder = folder
        # local
        if hasattr(self, "local_config"): self.local_config.folder = folder
        if hasattr(self, "local_storage"): self.local_storage.folder = folder

    def get_storage(self):
        """
        returns the configured type of storage, creating if needed
        """
        if self.storage == "s3":
            self.s3_storage = getattr_or(self, "s3_storage", S3Storage(self.s3_config))
            return self.s3_storage
        elif self.storage == "gd":
            self.gd_storage = getattr_or(self, "gd_storage", GDStorage(self.gd_config))
            return self.gd_storage
        elif self.storage == "local":
            self.local_storage = getattr_or(self, "local_storage", LocalStorage(self.local_config))
            return self.local_storage
        raise f"storage {self.storage} not implemented, available: {Config.AVAILABLE_STORAGES}"

    def destroy_webdriver(self):
        if self.webdriver is not None and type(self.webdriver) != str:
            self.webdriver.close()
            self.webdriver.quit()
            del self.webdriver

    def recreate_webdriver(self):
        options = webdriver.FirefoxOptions()
        options.headless = True
        options.set_preference('network.protocol-handler.external.tg', False)
        try:
            new_webdriver = webdriver.Firefox(options=options)
            # only destroy if creation is successful
            self.destroy_webdriver()
            self.webdriver = new_webdriver
            self.webdriver.set_window_size(self.selenium_config.window_width,
                                           self.selenium_config.window_height)
            self.webdriver.set_page_load_timeout(self.selenium_config.timeout_seconds)
        except TimeoutException as e:
            logger.error(f"failed to get new webdriver, possibly due to insufficient system resources or timeout settings: {e}")

    def __str__(self) -> str:
        return json.dumps({
            "config_file": self.config_file,
            "sheet": self.sheet,
            "storage": self.storage,
            "header": self.header,
            "check_if_exists": self.check_if_exists,
            "save_logs": self.save_logs,
            "selenium_config": asdict(self.selenium_config),
            "selenium_webdriver": self.webdriver != None,
            "s3_config": hasattr(self, "s3_config"),
            "s3_private": getattr_or(getattr(self, "s3_config", {}), "private", None),
            "gd_config": hasattr(self, "gd_config"),
            "local_config": hasattr(self, "local_config"),
            "wayback_config": self.wayback_config != None,
            "telegram_config": self.telegram_config != None,
            "twitter_config": self.twitter_config != None,
            "vk_config": self.vk_config != None,
            "gsheets_client": self.gsheets_client != None,
            "column_names": self.column_names,
        }, ensure_ascii=False, indent=4)
