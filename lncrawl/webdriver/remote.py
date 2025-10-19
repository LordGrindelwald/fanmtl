# -*- coding: utf-8 -*-
import logging
import os
from typing import Optional

from selenium.webdriver.remote.remote_connection import RemoteConnection
# Import ClientConfig to properly initialize the parent
from selenium.webdriver.remote.client_config import ClientConfig

from .job_queue import JobQueue
from .scripts import SCRIPTS

logger = logging.getLogger(__name__)


class ChromiumRemoteConnection(RemoteConnection):
    """Executes commands remotely using grpc"""

    def __init__(
        self,
        remote_server_addr, # This is the gRPC address for JobQueue
        browser_name,
        browser_version,
        keep_alive=True,
        ignore_proxy=False,
    ):
        # *** FIX: Initialize the parent RemoteConnection ***
        # Create a basic ClientConfig. The remote_server_addr here is for the standard
        # WebDriver HTTP endpoint, which ChromiumRemoteConnection doesn't use for commands.
        # Passing None might rely on SeleniumManager, or we can pass a dummy value.
        # Let's use a dummy placeholder, as Selenium might check it exists.
        # keep_alive is relevant for the underlying urllib3 pool managed by the parent,
        # even if we don't use its _request method directly.
        config = ClientConfig(remote_server_addr="http://127.0.0.1:4444", # Dummy WebDriver URL
                              keep_alive=keep_alive)

        # Call the parent class's __init__ method
        super().__init__(client_config=config)
        # *** END FIX ***

        # Initialize ChromiumRemoteConnection specific attributes AFTER parent init
        self.job_queue = JobQueue(remote_server_addr) # Use the actual gRPC address here
        self._browser_name = browser_name
        self._browser_version = browser_version
        self.ignore_proxy = ignore_proxy # Used in capabilities property

    def execute(self, command, params):
        """Executes a command against the remote server via JobQueue (overrides parent)"""
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
                # Attempt to gracefully shut down the browser via WebDriver command if possible,
                # BEFORE stopping the gRPC client. This might not work if connection is already broken.
                # super().execute(Command.QUIT, {"sessionId": self._session_id}) # Requires session_id, which isn't stored here
                pass # The original didn't seem to quit the browser via Selenium command
            except Exception as e:
                logger.warning(f"Exception during potential browser quit command: {e}")
            finally:
                try:
                    self.job_queue.stop_client()
                except Exception as e:
                    logger.error(f"Error stopping job queue client: {e}")
                finally:
                    self.job_queue = None
        # Call parent close potentially, though it might be no-op if keep_alive was false
        # super().close() # Seems unnecessary as we manage the connection via job_queue

    def close(self):
        """No-op, consistent with parent if keep_alive is true."""
        pass

    @property
    def capabilities(self):
        """Define capabilities directly, independent of parent's config."""
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
        # Apply proxy setting based on ignore_proxy flag
        if self.ignore_proxy:
            caps["proxy"] = {"proxyType": "direct"}
        elif self.client_config and self.client_config.proxy: # Check if config exists and has proxy
             # If not ignoring proxy, potentially use proxy from ClientConfig if set there
             # This part might need refinement depending on how proxy is intended to be configured
             pass # Currently, default is no proxy unless ignore_proxy sets it to direct.
        return caps

    def script_to_execute(self, script) -> Optional[str]:
        """Helper method to determine which script to execute"""
        # This method doesn't depend on parent state
        if script == "executeScript":
            return SCRIPTS["executeScript"]
        elif script == "executeAsyncScript":
            return SCRIPTS["executeAsyncScript"]
        return None

    def upload(self, filename):
        """Uploads a file to the remote server via JobQueue (overrides parent)"""
        if not self.job_queue:
            raise RuntimeError("ChromiumRemoteConnection has been quit.")
        with open(filename, "rb") as fp:
            content = fp.read()
        return self.job_queue.upload_file(content)
