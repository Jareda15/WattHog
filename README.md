# 🐗 WattHog

A modern, fast, and lightweight TUI (Terminal User Interface) utility for monitoring system power consumption and identifying battery-draining processes on Linux.

## Features
* **Real-time Power Reading:** Displays total desktop power consumption (reads CPU via Intel RAPL and GPU via AMD hwmon / Nvidia-smi).
* **Smart Process Scoring:** Instead of just showing CPU percentage, WattHog calculates an **"Impact"** score based on CPU usage, Context Switches (wakeups), and Disk I/O.
* **Interactive TUI:** Built with `textual` for a modern, responsive, and clickable terminal interface. Sort by any column, pause monitoring, or kill rogue processes directly.

## Installation

### Arch Linux / EndeavourOS (AUR)
The easiest way to install WattHog is via the AUR:
```bash
yay -S watthog-git

Manual Installation (Any Linux)

Clone the repository and run the installation script:
Bash

git clone [https://github.com/Jareda15/WattHog.git](https://github.com/Jareda15/WattHog.git)
cd WattHog
sudo ./install.sh

Note: A system restart might be required after manual installation to apply udev rules for RAPL sensors.
Usage

Simply launch the application from your desktop menu or run the following command in your terminal:
Bash

watthog

Controls:

    Click on table headers to sort processes.

    Press q to quit.

    Press p to pause/resume monitoring.

    Press k to kill (SIGTERM) the currently selected process.
