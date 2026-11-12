"""Keep tests isolated from any real caller-memory database."""
import os
import tempfile

os.environ.pop("DATABASE_URL", None)
os.environ["MEMORY_DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="sop-test-"), "memory.db")
