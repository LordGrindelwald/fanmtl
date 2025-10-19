# -*- coding: utf-8 -*-
import logging
import os
from typing import Optional

from selenium.webdriver.remote.remote_connection import RemoteConnection
from selenium.webdriver.remote.client_config import ClientConfig

# from .job_queue import JobQueue  <-- REMOVE THIS LINE
from .scripts import SCRIPTS

logger = logging.getLogger(__name__)


class ChromiumRemoteConnection(RemoteConnection):
    """Executes commands remotely using grpc"""

    def __init__(
        self,
        remote_server_addr,
        browser_name,
        browser_version,
        keep_alive=True,
        ignore_proxy=False,
    ):
        # *** FIX for circular import ***
        from .job_queue import JobQueue # <-- ADD THIS LINE HERE
        
        # FIX for _client_config error
        config = ClientConfig(remote_server_addr="http://127.0.0.1:4444", keep_alive=keep_alive)
        super().__init__(client_config=config)

        # lncrawl's custom logic
        self.job_queue = JobQueue(remote_server_addr)
        self._browser_name = browser_name
        self._browser_version = browser_version
        self.ignore_proxy = ignore_proxy

    # ... (the rest of the file remains the same) ...

    def execute(self, command, params):
        """Executes a command against the remote server via JobQueue"""
        if not self.job_queue:
            raise RuntimeError("ChromiumRemoteConnection has been quit.")
        return self.job_queue.execute(command, params)

    @property
    def browser_name(self):
        return self._browser_name

    @property
    def browser_version(self):
        return self._browser_version

    @property
    def w3c(self):
        return True

    def get_capability(self, key):
        """Returns the capability"""
        return self.capabilities.get(key)

    def quit(self):
        """Closes the browser and shuts down the ChromiumDriver executable"""
        if self.job_queue:
            try:
                self.job_queue.stop_client()
            except Exception as e:
                logger.error(f"Error stopping job queue client: {e}")
            finally:
                self.job_queue = None

    def close(self):
        """No-op"""
        pass

    @property
    def capabilities(self):
        caps = {
            "browserName": self.browser_name,
            "browserVersion": self.browser_version,
            "platformName": "any",
            "pageLoadStrategy": "normal",
            "acceptInsecureCerts": True,
            "timeouts": {"implicit": 0, "pageLoad": 300000, "script": 30000},
            "setWindowRect": True,
            "strictFileInteractability": False,
            "proxy": {},
            "unhandledPromptBehavior": "dismiss and notify",
            "webauthn:virtualAuthenticators": True,
        }
        if self.ignore_proxy:
            caps["proxy"] = {"proxyType": "direct"}
        return caps

    def script_to_execute(self, script) -> Optional[str]:
        """Helper method to determine which script to execute"""
        if script == "executeScript":
            return SCRIPTS["executeScript"]
        elif script == "executeAsyncScript":
            return SCRIPTS["executeAsyncScript"]
        return None

    def upload(self, filename):
        """Uploads a file to the remote server"""
        if not self.job_queue:
            raise RuntimeError("ChromiumRemoteConnection has been quit.")
        with open(filename, "rb") as fp:
            content = fp.read()
        return self.job_queue.upload_file(content)
