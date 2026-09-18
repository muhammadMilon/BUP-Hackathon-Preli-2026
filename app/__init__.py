"""Package init.

Loads a local .env before any submodule reads os.environ, so development runs
pick up API keys the same way systemd supplies them in production.
"""

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is optional at runtime
    pass
