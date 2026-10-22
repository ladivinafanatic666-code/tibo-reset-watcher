"""Small local configuration window for Tibo reset alerts (no dependencies)."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
import webbrowser

import watcher as w


SOURCE_LABELS = {
    "Free: community feed": "community",
    "Direct: official X API": "x_api",
}


def pythonw_path() -> Path:
    executable = Path(sys.executable)
    candidate = executable.with_name("pythonw.exe")
    return candidate if candidate.exists() else executable


def startup_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("The current Windows user startup folder could not be found.")
    return Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup/TiboResetWatcher.vbs"


def read_status() -> dict:
    try:
        status = json.loads((w.data_dir() / "status.json").read_text(encoding="utf-8"))
        return status if isinstance(status, dict) else {}
    except (OSError, ValueError):
        return {}


def is_running(status: dict) -> bool:
    if not status.get("running"):
        return False
    try:
        heartbeat = datetime.fromisoformat(status.get("heartbeat_at") or status["started_at"])
        age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
        return -5 <= age < 150
    except (ValueError, TypeError, KeyError):
        return False


def local_time(value: str | None) -> str:
    """Display source and status timestamps in UTC, independent of device locale."""
    if not value:
        return "Not checked yet"
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%m-%d %H:%M:%S UTC")
    except (ValueError, TypeError):
        return str(value)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.busy = False
        self.controls: list[ttk.Widget] = []
        self.root.title("Tibo Reset Alerts")
        self.root.geometry("920x800")
        self.root.minsize(880, 780)
        self.root.configure(background="#f5f7fa")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=(10, 6))
        style.configure("Heading.TLabel", font=("Segoe UI", 19, "bold"))
        style.configure("Note.TLabel", foreground="#526174", font=("Segoe UI", 9))
        style.configure("TNotebook.Tab", padding=(16, 8))
        cfg = w.load_config()
        self.vars = {
            key: tk.StringVar(value=str(cfg[key]))
            for key in ("recipient", "smtp_host", "smtp_port", "smtp_security", "smtp_user")
        }
        self.source = tk.StringVar(value=next(
            (label for label, value in SOURCE_LABELS.items() if value == cfg["source"]),
            next(iter(SOURCE_LABELS)),
        ))
        self.minutes = tk.StringVar(value=str(cfg["interval_seconds"] // 60))
        self.password = tk.StringVar()
        self.token = tk.StringVar()
        self.secret_note = tk.StringVar()
        self.token_note = tk.StringVar()
        self.status_text = tk.StringVar(value="Not running")
        self.operation_text = tk.StringVar(value="Enter an app password for your sender account, then send a test email.")
        self.source_note = tk.StringVar()
        self.auto_text = tk.StringVar()
        outer = ttk.Frame(root, padding=22)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Tibo Reset Alerts", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(outer, text="@thsottiaux · Completed resets by default · Checks every 5 minutes by default · Duplicate alerts filtered",
                  style="Note.TLabel").pack(anchor="w", pady=(5, 16))
        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        basic = ttk.Frame(notebook, padding=18)
        advanced = ttk.Frame(notebook, padding=18)
        notebook.add(basic, text="Settings")
        notebook.add(advanced, text="SMTP / X API")
        basic.columnconfigure(1, weight=1)
        advanced.columnconfigure(1, weight=1)
        ttk.Label(basic, text="Source").grid(row=0, column=0, sticky="w", padx=(0, 16), pady=6)
        self.source_box = ttk.Combobox(basic, textvariable=self.source,
                                       values=list(SOURCE_LABELS), state="readonly", width=32)
        self.source_box.grid(row=0, column=1, sticky="ew", pady=6)
        self.controls.append(self.source_box)
        self.source_box.bind("<<ComboboxSelected>>", lambda _e: self.update_source_note())
        ttk.Label(basic, textvariable=self.source_note, style="Note.TLabel", wraplength=760,
                  justify="left").grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 13))
        self.entry(basic, 2, "Recipient email", self.vars["recipient"])
        self.entry(basic, 3, "Sender email", self.vars["smtp_user"])
        self.entry(basic, 4, "App password", self.password, secret=True)
        ttk.Label(basic, textvariable=self.secret_note, style="Note.TLabel").grid(
            row=5, column=1, sticky="w", pady=(0, 8))
        interval = ttk.Frame(basic)
        interval.grid(row=6, column=1, sticky="w", pady=7)
        ttk.Label(basic, text="Check interval").grid(row=6, column=0, sticky="w", pady=7)
        minutes_box = ttk.Spinbox(interval, from_=1, to=1440, textvariable=self.minutes, width=6)
        minutes_box.pack(side="left")
        self.controls.append(minutes_box)
        ttk.Label(interval, text=" minutes (5 recommended)", style="Note.TLabel").pack(side="left")
        help_row = ttk.Frame(basic)
        help_row.grid(row=7, column=0, columnspan=2, sticky="w", pady=(7, 6))
        self.button(help_row, "Get a Gmail app password", self.open_google).pack(side="left")
        ttk.Label(basic, text="For Gmail, enable 2-Step Verification and create an app password. Other email providers\n"
                  "can be configured on the SMTP / X API tab. Leave saved credentials blank to keep them.\nCredentials are encrypted for your local Windows account.",
                  style="Note.TLabel", wraplength=760, justify="left").grid(
                      row=8, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(basic, text="Your computer must stay awake and online. The monitor continues when this window closes.\n"
                  "The first successful check establishes a baseline; it does not send alerts for old posts.",
                  style="Note.TLabel", wraplength=760, justify="left").grid(
                      row=9, column=0, columnspan=2, sticky="w", pady=5)

        self.entry(advanced, 0, "SMTP server", self.vars["smtp_host"])
        self.entry(advanced, 1, "SMTP port", self.vars["smtp_port"])
        ttk.Label(advanced, text="Encryption").grid(row=2, column=0, sticky="w", padx=(0, 16), pady=7)
        self.security_box = ttk.Combobox(advanced, textvariable=self.vars["smtp_security"],
                                         values=["SSL", "STARTTLS"], state="readonly")
        self.security_box.grid(row=2, column=1, sticky="ew", pady=7)
        self.controls.append(self.security_box)
        ttk.Label(advanced, text="Gmail: smtp.gmail.com / 465 / SSL, or 587 / STARTTLS.",
                  style="Note.TLabel").grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 18))
        ttk.Separator(advanced).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        self.entry(advanced, 5, "X Bearer Token", self.token, secret=True)
        ttk.Label(advanced, textvariable=self.token_note, style="Note.TLabel").grid(
            row=6, column=1, sticky="w")
        ttk.Label(advanced, text="A token is required only for the official X API. Enable API access in the X developer portal\nand check current pricing. "
                  "Direct mode filters new posts and reply context for reset-related content.\n"
                  "Community mode needs no X account or token, but may miss posts or publish them late.",
                  style="Note.TLabel", wraplength=760, justify="left").grid(
                      row=7, column=0, columnspan=2, sticky="w", pady=(14, 0))

        action_row = ttk.Frame(outer)
        action_row.pack(fill="x", pady=(16, 7))
        self.button(action_row, "Save settings", self.save).pack(side="left", padx=(0, 7))
        self.button(action_row, "Check source", self.preview).pack(side="left", padx=(0, 7))
        self.button(action_row, "Send test email", self.test_email).pack(side="left", padx=(0, 7))
        self.button(action_row, "Start monitor", self.start).pack(side="left", padx=(0, 7))
        self.button(action_row, "Stop monitor", self.stop).pack(side="left")
        utility_row = ttk.Frame(outer)
        utility_row.pack(fill="x", pady=(0, 10))
        self.button(utility_row, "View log", self.show_log).pack(side="left", padx=(0, 7))
        self.auto_button = self.button(utility_row, "Start at login", self.toggle_autostart)
        self.auto_button.pack(side="left", padx=(0, 8))
        ttk.Label(utility_row, textvariable=self.auto_text, style="Note.TLabel").pack(side="left")
        ttk.Separator(outer).pack(fill="x", pady=(0, 10))
        ttk.Label(outer, textvariable=self.status_text, wraplength=870).pack(anchor="w")
        ttk.Label(outer, textvariable=self.operation_text, style="Note.TLabel", wraplength=870,
                  justify="left").pack(anchor="w", pady=(7, 0))
        self.update_source_note()
        self.update_secret_notes()
        self.refresh_status()

    def entry(self, parent, row, label, variable, secret=False):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=7)
        field = ttk.Entry(parent, textvariable=variable, show="●" if secret else "")
        field.grid(row=row, column=1, sticky="ew", pady=7)
        self.controls.append(field)
        return field

    def button(self, parent, label, action):
        button = ttk.Button(parent, text=label, command=action)
        self.controls.append(button)
        return button

    def update_source_note(self):
        if SOURCE_LABELS[self.source.get()] == "community":
            self.source_note.set("Community mode reads collected reset announcements. Updates may be delayed or incomplete. Alerts include the original X post.")
        else:
            self.source_note.set("Direct mode reads new posts from Tibo on X and filters reset-related content. Requires an API token and may incur API charges.")

    def update_secret_notes(self):
        try:
            self.secret_note.set("App password saved; leave blank to keep it" if w.get_secret("smtp_password") else "No app password saved yet")
            self.token_note.set("Token saved; leave blank to keep it" if w.get_secret("x_bearer_token") else "Not required for community mode")
        except Exception:
            self.secret_note.set("Saved credentials could not be read. Please enter them again.")
            self.token_note.set("Saved credentials could not be read. Please enter them again.")

    def collect(self):
        cfg = w.load_config()
        cfg.update({key: variable.get().strip() for key, variable in self.vars.items()})
        cfg["source"] = SOURCE_LABELS[self.source.get()]
        try:
            cfg["interval_seconds"] = int(self.minutes.get()) * 60
            cfg["smtp_port"] = int(cfg["smtp_port"])
        except ValueError:
            raise ValueError("The check interval and SMTP port must be whole numbers.") from None
        if self.password.get().strip():
            value = self.password.get().strip()
            if cfg["smtp_host"].lower() == "smtp.gmail.com":
                value = value.replace(" ", "")
            w.set_secret("smtp_password", value)
            self.password.set("")
        if self.token.get().strip():
            w.set_secret("x_bearer_token", self.token.get().strip())
            self.token.set("")
        w.save_config(cfg)
        self.update_secret_notes()
        return cfg

    def launch_operation(self, task, on_success, note):
        if self.busy:
            return
        try:
            cfg = self.collect()
        except Exception as exc:
            self.show_error(exc)
            return
        self.set_busy(True)
        self.operation_text.set(note)

        def worker():
            try:
                result = task(cfg)
            except Exception as exc:
                self.root.after(0, lambda error=exc: self.complete_error(error))
            else:
                self.root.after(0, lambda: self.complete_success(on_success, result))

        threading.Thread(target=worker, daemon=True).start()

    def complete_success(self, callback, result):
        self.set_busy(False)
        callback(result)

    def complete_error(self, exc):
        self.set_busy(False)
        self.show_error(exc)

    def set_busy(self, value):
        self.busy = value
        for control in self.controls:
            control.configure(state="disabled" if value else "normal")
        if not value:
            self.source_box.configure(state="readonly")
            self.security_box.configure(state="readonly")

    def show_error(self, exc):
        text = w.error_text(exc)
        self.operation_text.set(text)
        messagebox.showerror("Action failed", text, parent=self.root)

    def save(self):
        try:
            self.collect()
            self.operation_text.set("Settings saved. The monitor will use them on its next check.")
        except Exception as exc:
            self.show_error(exc)

    def preview(self):
        self.launch_operation(w.preview_source, self.preview_result, "Checking the source. This does not send email or change the monitoring baseline…")

    def preview_result(self, result):
        self.operation_text.set(f"Source available: {result['fetched']} posts read; {result['matched']} relevant posts.")
        window = tk.Toplevel(self.root)
        window.title("Source preview · No email sent")
        window.geometry("730x500")
        output = scrolledtext.ScrolledText(window, wrap="word", font=("Segoe UI", 10), padx=15, pady=15)
        output.pack(fill="both", expand=True)
        output.insert("end", f"{result['fetched']} posts read; {result['matched']} matches\n{result.get('detail', '')}\n\n")
        for post in result.get("posts", [])[:10]:
            output.insert("end", f"{local_time(post.get('created_at'))}\n{post.get('text', '')}\n{post.get('url', '')}\n\n")
        if not result.get("posts"):
            output.insert("end", "No relevant posts were returned by this check.")
        output.configure(state="disabled")

    def test_email(self):
        self.launch_operation(w.send_test_email, self.test_result, "Sending a test email. Please wait…")

    def test_result(self, _result):
        text = "The mail server accepted the test email. Check your inbox and spam folder before starting the monitor."
        self.operation_text.set(text)
        messagebox.showinfo("Test email submitted", text, parent=self.root)

    def start(self):
        def task(cfg):
            w.validate_config(cfg, need_mail=True)
            if is_running(read_status()):
                return "The monitor is already running."
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen([str(pythonw_path()), str(w.ROOT / "watcher.py"), "--loop"],
                             cwd=str(w.ROOT), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags)
            return "Monitor start requested. Its first successful check establishes a baseline for future alerts."
        self.launch_operation(task, lambda text: self.operation_text.set(text), "Starting the monitor…")

    def stop(self):
        try:
            if not is_running(read_status()):
                self.operation_text.set("The monitor is not running.")
                return
            folder = w.data_dir()
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "stop.request").write_text("stop\n", encoding="utf-8")
            self.operation_text.set("Stop requested. The monitor will exit after its current network operation. Login startup settings are unchanged.")
        except Exception as exc:
            self.show_error(exc)

    def toggle_autostart(self):
        try:
            target = startup_path()
            if target.exists():
                if "TiboResetWatcher" not in target.read_text(encoding="utf-16"):
                    raise ValueError("A startup file with this name belongs to another program. Please review it manually.")
                target.unlink()
                self.operation_text.set("Start at Windows login is disabled. The current monitor process is unchanged.")
            else:
                cfg = self.collect()
                w.validate_config(cfg, need_mail=True)
                command = f'"{pythonw_path()}" "{w.ROOT / "watcher.py"}" --loop'
                literal = '"' + command.replace('"', '""') + '"'
                script = "' TiboResetWatcher: current-user startup only\r\n" + f'CreateObject("WScript.Shell").Run {literal}, 0, False\r\n'
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(script, encoding="utf-16")
                self.operation_text.set("Start at Windows login is enabled. The computer must remain awake to check for updates.")
            self.refresh_auto_text()
        except Exception as exc:
            self.show_error(exc)

    def refresh_auto_text(self):
        try:
            enabled = startup_path().exists()
            self.auto_text.set("Enabled" if enabled else "Disabled")
            self.auto_button.configure(text="Disable start at login" if enabled else "Enable start at login")
        except Exception:
            self.auto_text.set("Startup status unavailable")

    def refresh_status(self):
        status = read_status()
        if is_running(status):
            if status.get("error"):
                text = f"Monitor running · Last check failed: {status['error']}"
            else:
                text = f"Monitor running · Last check: {local_time(status.get('checked_at'))} · Next: {local_time(status.get('next_check_at'))}"
        else:
            text = "Monitor stopped · Complete the settings, then select Start monitor."
        self.status_text.set(text)
        self.refresh_auto_text()
        self.root.after(2000, self.refresh_status)

    def show_log(self):
        path = w.data_dir() / "watcher.log"
        if not path.exists():
            self.operation_text.set("No log is available yet. The monitor creates it when it starts.")
            return
        try:
            os.startfile(str(path))
        except OSError as exc:
            self.show_error(exc)

    def open_google(self):
        webbrowser.open("https://myaccount.google.com/apppasswords")

    def close(self):
        if self.busy:
            self.operation_text.set("An operation is still in progress. You can close the window after it finishes.")
            return
        self.root.destroy()


def main():
    self_test = "--self-test" in sys.argv
    w.setup_logging()
    root = tk.Tk()
    if self_test:
        root.withdraw()
    try:
        App(root)
        if self_test:
            root.update_idletasks()
            print("GUI self-test passed")
            root.destroy()
            return 0
        root.mainloop()
    except Exception as exc:
        if self_test:
            print(f"GUI self-test failed: {w.error_text(exc)}", file=sys.stderr)
        else:
            messagebox.showerror("Could not open settings", w.error_text(exc), parent=root)
        root.destroy()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
