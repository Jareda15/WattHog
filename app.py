#!/usr/bin/env python3
"""
WattHog TUI Application
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Optional

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Static
from textual.timer import Timer

from backend import (
    BatteryInfo,
    RaplInfo,
    GpuInfo,
    ProcessScore,
    get_power_source,
    get_top_processes,
    find_rapl_domains,
    setup_logging,
)


class PowerHeader(Static):
    DEFAULT_CSS = """
    PowerHeader {
        dock: top;
        height: 5;
        padding: 1 2;
        background: $surface;
        color: $text;
        text-style: bold;
        border-bottom: solid $primary;
    }
    """

    battery: reactive[Optional[BatteryInfo]] = reactive(None)
    rapl: reactive[Optional[RaplInfo]] = reactive(None)
    gpu: reactive[Optional[GpuInfo]] = reactive(None)

    def render(self) -> str:
        gpu_str = ""
        gpu_power = 0.0
        if self.gpu is not None:
            gpu_str = f"   [GPU: {self.gpu.name} - {self.gpu.power_watts:.1f} W]"
            gpu_power = self.gpu.power_watts

        if self.battery is not None:
            b = self.battery
            bar_len = 20
            filled = int(b.percent / 100 * bar_len)
            bar = "█" * filled + "░" * (bar_len - filled)

            if b.percent > 60:
                status_icon = "🟢"
            elif b.percent > 25:
                status_icon = "🟡"
            else:
                status_icon = "🔴"

            time_str = ""
            if b.time_remaining_h is not None:
                h = int(b.time_remaining_h)
                m = int((b.time_remaining_h - h) * 60)
                time_str = f"  ⏱ {h}h {m:02d}min"

            return (
                f"🐗 WattHog                                   "
                f"{b.status}\n"
                f"🔋 {status_icon} [{bar}] {b.percent:.0f}%     "
                f"⚡ {b.power_now_watts:.1f} W{time_str}{gpu_str}"
            )

        elif self.rapl is not None:
            r = self.rapl
            if not r.has_permission:
                return (
                    f" WattHog                         Desktop Mode\n"
                    f"⚠️  Missing CPU power permissions. Run: sudo ./install.sh{gpu_str}"
                )
            else:
                total_power = r.power_watts + gpu_power
                return (
                    f" WattHog                         Desktop Mode\n"
                    f"⚡ Desktop Power: {total_power:.1f} W (CPU: {r.power_watts:.1f} W + GPU: {gpu_power:.1f} W)"
                )

        else:
            return (
                f" WattHog\n"
                f"⚠️  No power source found. Displaying processes only.{gpu_str}"
            )


class StatusBar(Static):
    DEFAULT_CSS = """
    StatusBar {
        dock: top;
        height: 1;
        padding: 0 2;
        background: $primary;
        color: $text;
    }
    """

    paused: reactive[bool] = reactive(False)
    process_count: reactive[int] = reactive(0)

    def render(self) -> str:
        status = "⏸ PAUSED" if self.paused else "▶ RUNNING"
        return (
            f" {status}  │  "
            f"Processes listed: {self.process_count}  │  "
            f"Update interval: 1.5s"
        )


class WattHogApp(App):
    TITLE = "WattHog"
    SUB_TITLE = "Power and process monitor"

    CSS = """
    Screen {
        background: $surface;
    }

    DataTable {
        height: 1fr;
    }

    DataTable > .datatable--header {
        text-style: bold;
        background: $primary;
        color: $text;
    }

    DataTable > .datatable--cursor {
        background: $secondary;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("p", "toggle_pause", "Pause/Resume"),
        Binding("r", "force_refresh", "Refresh"),
        Binding("k", "kill_process", "Kill (SIGTERM)"),
    ]

    paused: reactive[bool] = reactive(False)
    sort_key: str = "score"

    def __init__(self, demo: bool = False, **kwargs):
        super().__init__(**kwargs)
        self._demo = demo
        self._update_timer: Optional[Timer] = None
        self._current_processes: list[ProcessScore] = []

    def compose(self) -> ComposeResult:
        yield PowerHeader(id="power-header")
        yield StatusBar(id="status-bar")
        yield DataTable(id="process-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#process-table", DataTable)
        table.cursor_type = "row"

        table.add_column("PID", key="pid", width=8)
        table.add_column("Process", key="name", width=25)
        table.add_column("User", key="user", width=14)
        table.add_column("CPU %", key="cpu", width=8)
        table.add_column("CtxSw/s", key="ctx", width=10)
        table.add_column("I/O MB/s", key="io", width=10)
        table.add_column("⚡ Impact", key="score", width=10)

        self.refresh_data()
        self._update_timer = self.set_interval(1.5, self._tick)

    def _tick(self) -> None:
        if not self.paused:
            self.refresh_data()

    @work(thread=True, exclusive=True)
    def refresh_data(self) -> None:
        battery, rapl, gpu = get_power_source(demo=self._demo)
        processes = get_top_processes(n=20)

        if self.sort_key == "score":
            processes.sort(key=lambda p: p.power_score, reverse=True)
        elif self.sort_key == "cpu":
            processes.sort(key=lambda p: p.cpu_percent, reverse=True)
        elif self.sort_key == "ctx":
            processes.sort(key=lambda p: p.ctx_switches, reverse=True)
        elif self.sort_key == "io":
            processes.sort(key=lambda p: (p.io_read_bytes + p.io_write_bytes), reverse=True)
        elif self.sort_key == "pid":
            processes.sort(key=lambda p: p.pid)
        elif self.sort_key == "name":
            processes.sort(key=lambda p: p.name.lower())
        elif self.sort_key == "user":
            processes.sort(key=lambda p: p.username.lower())

        self.call_from_thread(self._apply_data, battery, rapl, gpu, processes)

    def _apply_data(
        self,
        battery: Optional[BatteryInfo],
        rapl: Optional[RaplInfo],
        gpu: Optional[GpuInfo],
        processes: list[ProcessScore],
    ) -> None:
        header = self.query_one("#power-header", PowerHeader)
        header.battery = battery
        header.rapl = rapl
        header.gpu = gpu

        status = self.query_one("#status-bar", StatusBar)
        status.process_count = len(processes)
        status.paused = self.paused

        table = self.query_one("#process-table", DataTable)
        table.clear()

        self._current_processes = processes

        for proc in processes:
            io_mbs = (proc.io_read_bytes + proc.io_write_bytes) / 1_000_000
            
            name_str = proc.name[:24]
            score_str = f"{proc.power_score:.1f}"
            
            if proc.power_score > 20.0:
                name_str = f"[red]{name_str}[/red]"
                score_str = f"[red]{score_str}[/red]"
            elif proc.power_score > 10.0:
                name_str = f"[yellow]{name_str}[/yellow]"
                score_str = f"[yellow]{score_str}[/yellow]"

            table.add_row(
                str(proc.pid),
                name_str,
                proc.username[:13],
                f"{proc.cpu_percent:.1f}",
                str(proc.ctx_switches),
                f"{io_mbs:.1f}",
                score_str,
                key=str(proc.pid),
            )

    def on_data_table_column_selected(self, event: DataTable.ColumnSelected) -> None:
        if event.column_key.value in ["score", "cpu", "ctx", "io", "pid", "name", "user"]:
            self.sort_key = event.column_key.value
            self.notify(f"Sorted by: {event.column_key.value}", timeout=1)
            self.refresh_data()

    def action_toggle_pause(self) -> None:
        self.paused = not self.paused
        status = self.query_one("#status-bar", StatusBar)
        status.paused = self.paused
        state = "paused" if self.paused else "resumed"
        self.notify(f"Monitoring {state}", timeout=2)

    def action_force_refresh(self) -> None:
        self.refresh_data()
        self.notify("Refreshing...", timeout=1)

    def action_kill_process(self) -> None:
        import signal
        table = self.query_one("#process-table", DataTable)

        if table.cursor_row is None or table.cursor_row < 0:
            self.notify("Select a process first", severity="warning")
            return

        if table.cursor_row >= len(self._current_processes):
            self.notify("Invalid cursor position", severity="warning")
            return

        proc = self._current_processes[table.cursor_row]

        try:
            os.kill(proc.pid, signal.SIGTERM)
            self.notify(
                f"SIGTERM sent to {proc.name} (PID {proc.pid})",
                severity="information",
                timeout=3,
            )
        except ProcessLookupError:
            self.notify(
                f"Process {proc.pid} no longer exists",
                severity="warning",
                timeout=3,
            )
        except PermissionError:
            self.notify(
                f"Permission denied to kill PID {proc.pid}",
                severity="error",
                timeout=3,
            )


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="WattHog TUI Application")
    parser.add_argument("--demo", action="store_true", help="Use simulated data")
    parser.add_argument("--debug", action="store_true", help="Enable verbose hardware trace logging")
    args = parser.parse_args()

    setup_logging(args.debug)

    app = WattHogApp(demo=args.demo)
    app.run()


if __name__ == "__main__":
    main()
