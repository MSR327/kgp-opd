import keyring
import pytest
from keyring.backend import KeyringBackend


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.items = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.items:
            raise keyring.errors.PasswordDeleteError("missing")
        del self.items[(service, username)]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Logs go to a temp folder and logins to an in-memory store, never the real ones."""
    monkeypatch.setenv("BCROY_BOOKER_HOME", str(tmp_path / "home"))
    old = keyring.get_keyring()
    keyring.set_keyring(MemoryKeyring())
    yield
    keyring.set_keyring(old)
