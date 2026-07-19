from __future__ import annotations

import argparse
from mini_claude.tui.app import MiniClaudeTuiApp
from mini_claude.core.config import get_config

def main():
    parser = argparse.ArgumentParser(prog="ClaudeTui", description="Mini-Claude TUI")
    parser.add_argument("--replay",metavar="RUN_ID", help="Replay event from past on connect")

    args = parser.parse_args()

    config = get_config()

    app = MiniClaudeTuiApp(config.host, config.port, replay_run_id=args.replay)
    app.run()

if __name__== "__main__":
    main()
