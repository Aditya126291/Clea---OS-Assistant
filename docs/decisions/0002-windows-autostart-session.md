# ADR 0002: Windows Auto-Start via Interactive Task Scheduler

- **Date**: 2026-11-12
- **Status**: Accepted

## Context

Clea needs both its Python daemon (`clea daemon --background`) and desktop shell to start automatically when the user logs in to Windows, with no console window and no administrator prompt.

## Decision

1. **Use Windows Task Scheduler (`InteractiveToken`, `LeastPrivilege`) — not a Windows Service**:
   - Windows Services run in **Session 0**, which is strictly isolated from the user's interactive desktop session.
   - Future Clea tools (volume, brightness, media, window/app launching, and accessibility-tree automation) require access to the user's interactive desktop session, audio endpoints, and user profile (`%LOCALAPPDATA%`).
   - Running the scheduled task with `<LogonTrigger>` and `<LogonType>InteractiveToken</LogonType>` (`<RunLevel>LeastPrivilege</RunLevel>`) starts the daemon inside the user's own interactive logon session without requiring UAC elevation.
2. **Launch `.venv\Scripts\pythonw.exe` directly**:
   - `pythonw.exe` runs with the Windows GUI subsystem so no console window flashes at login, avoids `uv` wrapper indirection, and writes logs to `<user_data_dir>/logs/daemon.log` via a `RotatingFileHandler`.
3. **Electron login item (`--hidden`)**:
   - Controlled from Settings (`app.setLoginItemSettings`) and off by default. Passing `--hidden` starts the Electron shell directly in the system tray without opening the main window.
