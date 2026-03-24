"""Command handler: reads commands from calila-to-gridbot.txt."""

import os
import sys


class CommandHandler:
    def __init__(self, config, grid, risk, notifier, reporter):
        self.command_file = config.command_file
        self.grid = grid
        self.risk = risk
        self.notifier = notifier
        self.reporter = reporter
        self.config = config
        self.last_size = 0

        # Initialize to current file size to avoid processing old commands
        if os.path.exists(self.command_file):
            self.last_size = os.path.getsize(self.command_file)

    async def poll(self):
        try:
            if not os.path.exists(self.command_file):
                return
            with open(self.command_file, "r") as f:
                content = f.read()
            if len(content) <= self.last_size:
                return
            new_content = content[self.last_size:]
            self.last_size = len(content)
            for line in new_content.strip().split("\n"):
                line = line.strip()
                if line:
                    await self._execute(line)
        except Exception as e:
            self.notifier.send(f"Command error: {e}")

    async def _execute(self, command: str):
        parts = command.lower().split()
        cmd = parts[0] if parts else ""

        if cmd == "status":
            self.notifier.send(self.grid.get_status_text())

        elif cmd == "pause":
            self.grid.paused = True
            self.notifier.send("Bot paused.")

        elif cmd == "resume":
            self.grid.paused = False
            self.notifier.send("Bot resumed.")

        elif cmd == "reset":
            await self.grid.reset()
            self.notifier.send("Grid reset with new base price.")

        elif cmd == "pnl":
            self.notifier.send(self.reporter.get_pnl_text())

        elif cmd == "stop":
            self.notifier.send("Bot stopping.")
            sys.exit(0)

        elif cmd == "config" and len(parts) >= 3:
            key, value = parts[1], parts[2]
            self._update_config(key, value)

        else:
            self.notifier.send(f"Unknown command: {command}")

    def _update_config(self, key: str, value: str):
        valid_keys = {
            "grid_levels": int,
            "grid_spacing_pct": float,
            "order_size_usdt": float,
            "stop_loss_pct": float,
            "trailing_profit_pct": float,
            "trailing_callback_pct": float,
            "tick_interval": int,
        }
        if key not in valid_keys:
            self.notifier.send(f"Cannot change '{key}'. Allowed: {', '.join(valid_keys)}")
            return
        try:
            typed_value = valid_keys[key](value)
            setattr(self.config, key, typed_value)
            self.notifier.send(f"Config updated: {key} = {typed_value}")
        except ValueError:
            self.notifier.send(f"Invalid value for {key}: {value}")
