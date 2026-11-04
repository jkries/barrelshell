"""Exercise document retrieval without starting Ollama or the bot."""
import argparse
import json
from pathlib import Path
import sys

from .service import Documents, Settings
from .transport import FetchError


def main():
    # Windows redirected consoles otherwise default to legacy code pages that
    # cannot print many perfectly valid PDF/HTML characters.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", help='URL, or "read ID | 2-4", "pages ID | 3", "search ID | words"')
    parser.add_argument("--render", action="store_true", help="explicitly enable Chromium for this fetch")
    parser.add_argument("--refresh", action="store_true", help="ignore cached URL content")
    parser.add_argument("--file", type=Path, help="extract a local PDF instead of a URL")
    parser.add_argument("--chat-id", default="cli", help="cache partition (default: cli, separate from bot chats)")
    parser.add_argument("--cache-dir", default=".barrel_cache")
    args = parser.parse_args()
    if bool(args.command) == bool(args.file):
        parser.error("give exactly one command or --file PDF_PATH")
    try:
        config = json.loads(Path("config.json").read_text(encoding="utf-8")) if Path("config.json").exists() else {}
        if not isinstance(config, dict):
            raise FetchError("config.json must contain an object")
        if args.render:
            config["fetch_browser_enabled"] = True
        docs = Documents(Settings.from_config(config), args.cache_dir)
        if args.file:
            result = docs.local(args.file, args.file.name, args.chat_id)
        else:
            command = args.command + (" | render" if args.render else "") + (" | refresh" if args.refresh else "")
            result = docs.command(command, args.chat_id)
        print(result)
        return 0
    except (FetchError, OSError, ValueError) as exc:
        print(f"Fetch failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
