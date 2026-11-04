"""Small, single-host encrypted file store. Lock + atomic replace prevent lost writes."""
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from cryptography.fernet import Fernet

class Store:
    def __init__(self, directory):
        self.path = Path(directory)
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = os.getenv('ORACLE_DATA_KEY')
        keyfile = self.path / '.data-key'
        if not key:
            if os.getenv('VERCEL'):
                raise RuntimeError('Persistent file storage and ORACLE_DATA_KEY must be configured before deployment.')
            try:
                fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as out:
                    out.write(Fernet.generate_key())
            except FileExistsError:
                pass
            key = keyfile.read_bytes()
        self.cipher = Fernet(key)

    @contextmanager
    def transaction(self):
        with open(self.path / '.lock', 'a+b') as lock:
            os.chmod(self.path / '.lock', 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            file = self.path / 'accounts.enc'
            data = json.loads(self.cipher.decrypt(file.read_bytes())) if file.exists() else {'users': {}, 'orders': {}, 'payments': {}, 'attempts': {}}
            yield data
            encrypted = self.cipher.encrypt(json.dumps(data).encode())
            fd, temporary = tempfile.mkstemp(dir=self.path)
            try:
                with os.fdopen(fd, 'wb') as out:
                    out.write(encrypted)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temporary, file)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
