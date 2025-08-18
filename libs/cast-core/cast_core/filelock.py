"""File locking for preventing concurrent Cast operations."""

import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

# Import fcntl only on Unix-like systems
if sys.platform != "win32":
    import fcntl


class FileLock:
    """Simple file-based lock for preventing concurrent operations."""

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self.lock_file: int | None = None

    def acquire(self, timeout: float = 0) -> bool:
        """
        Try to acquire the lock.

        Args:
            timeout: Timeout in seconds (0 = non-blocking)

        Returns:
            True if lock acquired, False otherwise
        """
        # Create lock file if it doesn't exist
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        # Open lock file
        if sys.platform == "win32":
            # Windows doesn't have fcntl, use a simpler approach
            try:
                # Try to create lock file exclusively
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                self.lock_file = fd
                return True
            except FileExistsError:
                # Check if lock file is stale (older than 5 minutes)
                if self.lock_path.exists():
                    import time

                    age = time.time() - self.lock_path.stat().st_mtime
                    if age > 300:  # 5 minutes
                        # Stale lock, remove and retry
                        self.lock_path.unlink(missing_ok=True)
                        return self.acquire(timeout)
                return False
        else:
            # Unix-like systems with fcntl
            self.lock_file = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR)

            try:
                # Try to acquire exclusive lock
                fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                os.close(self.lock_file)
                self.lock_file = None
                return False

    def release(self) -> None:
        """Release the lock."""
        if self.lock_file is not None:
            if sys.platform == "win32":
                os.close(self.lock_file)
                self.lock_path.unlink(missing_ok=True)
            else:
                fcntl.flock(self.lock_file, fcntl.LOCK_UN)
                os.close(self.lock_file)
            self.lock_file = None

    def __enter__(self):
        """Context manager entry."""
        if not self.acquire():
            raise RuntimeError(f"Could not acquire lock: {self.lock_path}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.release()


@contextmanager
def cast_lock(root_path: Path) -> Generator[None, None, None]:
    """
    Context manager for Cast operations lock.

    Args:
        root_path: Cast root directory

    Raises:
        RuntimeError: If lock cannot be acquired
    """
    lock_path = root_path / ".cast" / ".lock"
    lock = FileLock(lock_path)

    if not lock.acquire():
        raise RuntimeError(
            "Another Cast operation is running. If this is incorrect, remove .cast/.lock"
        )

    try:
        yield
    finally:
        lock.release()
