#!/usr/bin/env python3
"""Simple TUI for UniFi Access - built from scratch."""

import threading
from datetime import datetime

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Footer, Header, Static, RichLog
from textual.logging import TextualHandler
import json
import os
import logging

from unifi_access_api import UnifiAccessAPI
from unifi_native_api import UniFiNativeAPI
from schedule_manager import ScheduleManager

# Configure TUI logging
logging.basicConfig(
    filename='tui.log',
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("tui")


class SimpleTUI(App):
    """Simple TUI for UniFi Access."""

    CSS = """
    Screen {
        background: $surface;
    }

    #refresh-status {
        height: 1;
        background: $panel;
        color: $text;
        padding: 0 1;
        text-align: center;
        margin: 0;
    }

    #status {
        height: 1;
        background: $primary;
        color: $text;
        padding: 0 1;
        margin: 0;
    }

    Vertical {
        height: 100%;
        width: 100%;
    }

    DataTable {
        height: 1fr;
    }
    
    RichLog {
        height: 10;
        border-top: solid $secondary;
        background: $surface;
        color: $text;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("u", "unlock", "Unlock Door"),
        ("h", "hold_open", "Hold Open (Day)"),
        ("f", "hold_open_forever", "Hold Forever"),
        ("H", "undo_hold_open", "Stop Hold"),
    ]

    def __init__(self):
        super().__init__()
        self.api = UnifiAccessAPI()
        self.doors = []
        self.last_updated = None
        self.is_refreshing = False
        
        # Initialize Native API for Schedule Injection
        # Load credentials for native api
        creds_file = "credentials_native.json"
        if not os.path.exists(creds_file):
             creds_file = "credentials.json"
             
        native_creds = {}
        if os.path.exists(creds_file):
            with open(creds_file, 'r') as f:
                native_creds = json.load(f)

        self.native_api = UniFiNativeAPI(
            host=f"https://{native_creds.get('host', '')}",
            username=native_creds.get("username", "admin"),
            password=native_creds.get("password", native_creds.get("token", "")),
        )
        # Try to load session, but don't block on interactive login here
        # If session is invalid, actions requiring it will fail or we could prompt (tricky in TUI)
        self.native_api._load_session()
        self.native_api._validate_session()
        
        self.schedule_manager = ScheduleManager(self.native_api)

    def compose(self) -> ComposeResult:
        """Create the UI."""
        yield Header()
        with Vertical():
            yield Static("Last Updated: Never | Status: Ready", id="refresh-status")
            yield Static("", id="status")
            yield DataTable()
            yield RichLog(highlight=True, markup=True, id="log_console")
        yield Footer()

    def on_mount(self) -> None:
        """Initialize when mounted."""
        # Setup Logging to Console
        log_console = self.query_one("#log_console", RichLog)
        
        class WidgetHandler(logging.Handler):
            def emit(self, record):
                log_entry = self.format(record)
                log_console.write(log_entry)
        
        widget_handler = WidgetHandler()
        widget_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S'))
        
        # Attach to root logger to capture everything
        root_logger = logging.getLogger()
        root_logger.addHandler(widget_handler)
        root_logger.setLevel(logging.INFO)

        # Set up the table
        table = self.query_one(DataTable)
        table.add_columns("Door Name", "Status")
        table.cursor_type = "row"

        # Load initial data
        self.action_refresh()
        self.set_interval(5, self.action_refresh)
        self.set_interval(0.5, self.update_refresh_status)

    def update_refresh_status(self) -> None:
        """Update the refresh status display."""
        refresh_status = self.query_one("#refresh-status", Static)

        # Calculate time since last update
        if self.last_updated:
            elapsed = datetime.now() - self.last_updated
            seconds = int(elapsed.total_seconds())
            if seconds < 2:
                time_str = "just now"
            elif seconds < 60:
                time_str = f"{seconds}s ago"
            else:
                minutes = seconds // 60
                time_str = f"{minutes}m ago"
        else:
            time_str = "Never"

        # Show status
        if self.is_refreshing:
            status_text = "🔄 Refreshing..."
        else:
            status_text = "✓ Ready"

        refresh_status.update(f"Last Updated: {time_str} | Status: {status_text}")

    def fetch_data_thread(self) -> None:
        """Fetch data in background thread."""
        self.is_refreshing = True
        try:
            doors = self.api.get_doors()
            
            # Check for scheduled hold opens
            # Note: This adds N requests (one per door), might be slow for many doors
            for door in doors:
                try:
                    status_text = self.schedule_manager.get_hold_status_text(door.id)
                    if status_text:
                        setattr(door, "_custom_status", status_text)
                except Exception:
                    pass # Ignore errors checking schedule to keep UI responsive
            
            self.call_from_thread(self.update_table, doors)
        except Exception as e:
            self.call_from_thread(self.show_error, str(e))
        finally:
            self.is_refreshing = False

    def update_table(self, doors) -> None:
        """Update table with door data (on main thread)."""
        self.doors = doors
        self.last_updated = datetime.now()

        table = self.query_one(DataTable)
        status = self.query_one("#status", Static)

        # Save cursor position before update
        saved_cursor_row = None
        if table.cursor_coordinate:
            saved_cursor_row = table.cursor_coordinate.row

        # Only rebuild if number of doors changed
        if len(doors) != table.row_count:
            # Doors added/removed - must rebuild
            table.clear()

            for door in doors:
                display_status = getattr(door, "_custom_status", door.display_status)
                table.add_row(door.name, display_status, key=door.id)

            # Re-enable cursor after clear
            if table.row_count > 0:
                table.cursor_type = "row"

                # Restore cursor position if valid
                if saved_cursor_row is not None and saved_cursor_row < table.row_count:
                    table.move_cursor(row=saved_cursor_row)
        else:
            # Same number of doors - just update status column
            for idx, door in enumerate(doors):
                display_status = getattr(door, "_custom_status", door.display_status)
                table.update_cell_at(Coordinate(idx, 1), display_status)

        status.update(f"{len(doors)} doors loaded")

    def show_error(self, error_msg: str) -> None:
        """Show error message."""
        status = self.query_one("#status", Static)
        status.update(f"Error: {error_msg}")

    def action_refresh(self) -> None:
        """Start refresh in background."""
        # Don't start another refresh if one is already running
        if not self.is_refreshing:
            # Run API call in background thread
            thread = threading.Thread(target=self.fetch_data_thread, daemon=True)
            thread.start()

    def get_selected_door_id(self) -> str | None:
        """Get the ID of the currently selected door."""
        table = self.query_one(DataTable)

        # Get cursor position
        if table.cursor_coordinate:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            if row_key:
                return str(row_key.value)
        return None

    def action_unlock(self) -> None:
        """Unlock the selected door."""
        door_id = self.get_selected_door_id()
        if door_id:
            status = self.query_one("#status", Static)
            if self.api.unlock_door(door_id):
                status.update(f"Unlocked door {door_id}")
                self.action_refresh()
            else:
                status.update(f"Failed to unlock door {door_id}")

    def action_hold_open(self) -> None:
        """Hold open the selected door via Schedule Injection."""
        door_id = self.get_selected_door_id()
        if door_id:
            status = self.query_one("#status", Static)
            status.update(f"Injecting hold open schedule for {door_id}...")
            
            # Run in thread to avoid blocking
            def run_inject():
                if self.schedule_manager.inject_hold_open(door_id):
                    self.call_from_thread(lambda: status.update(f"Scheduled hold open active for {door_id}"))
                    self.action_refresh()
                else:
                    self.call_from_thread(lambda: status.update(f"Failed to inject schedule for {door_id}"))
            
            threading.Thread(target=run_inject, daemon=True).start()

    def action_hold_open_forever(self) -> None:
        """Hold open FOREVER via Schedule Injection."""
        door_id = self.get_selected_door_id()
        if door_id:
            status = self.query_one("#status", Static)
            status.update(f"Injecting FOREVER hold open for {door_id}...")
            
            def run_inject():
                if self.schedule_manager.inject_hold_open_forever(door_id):
                    self.call_from_thread(lambda: status.update(f"Forever hold open active for {door_id}"))
                    self.action_refresh()
                else:
                    self.call_from_thread(lambda: status.update(f"Failed to inject forever schedule"))
            
            threading.Thread(target=run_inject, daemon=True).start()

    def action_undo_hold_open(self) -> None:
        """Remove hold open schedule."""
        door_id = self.get_selected_door_id()
        if door_id:
            status = self.query_one("#status", Static)
            status.update(f"Removing hold open schedule for {door_id}...")
            
            def run_remove():
                if self.schedule_manager.remove_hold_open(door_id):
                    self.call_from_thread(lambda: status.update(f"Removed hold open schedule for {door_id}"))
                    self.action_refresh()
                else:
                    self.call_from_thread(lambda: status.update(f"Failed to remove schedule for {door_id}"))
            
            threading.Thread(target=run_remove, daemon=True).start()


if __name__ == "__main__":
    app = SimpleTUI()
    app.run()