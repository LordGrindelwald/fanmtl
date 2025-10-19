# -*- coding: utf-8 -*-
import logging
import os
from typing import Optional

from selenium.webdriver.remote.remote_connection import RemoteConnection

from .job_queue import JobQueue
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
        # *** THE ACTUAL FIX ***
        # The parent class is not initialized, so we manually add the
        # attribute that Selenium's RemoteWebDriver is expecting.
        # We set it to None, as it's not used by this custom connection class.
        self._client_config = None
        # *** END FIX ***

        self.job_queue = JobQueue(remote_server_addr)
        self._browser_name = browser_name
        self._browser_version = browser_version
        self.keep_alive = keep_alive
        self.ignore_proxy = ignore_proxy

    def execute(self, command, params):
        """Executes a command against the remote server"""
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
            self.job_queue.stop_client()
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
        fp = open(filename, "rb")
        content = fp.read()
        fp.close()
        return self.job_queue.upload_file(content)
