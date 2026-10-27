from __future__ import annotations

import random
import tkinter as tk
from tkinter import ttk
from typing import Any

from simon_common import (
    COLOR_ORDER,
    COLOR_SPECS,
    COMMAND_FILE,
    DIALOG_EXPECTED_CHOICE,
    DIALOG_EXPECTED_NUMBER,
    DIALOG_TITLE,
    FIXED_SEQUENCE,
    STATE_FILE,
    TARGET_LOG_FILE,
    TARGET_TITLE,
    append_log,
    ensure_runtime_dir,
    now_iso,
    now_stamp,
    read_json,
    write_json_atomic,
)


class SimonTargetApp:
    def __init__(self) -> None:
        ensure_runtime_dir()
        self.root = tk.Tk()
        self.root.title(TARGET_TITLE)
        self.root.geometry("820x740")
        self.root.minsize(760, 680)

        self.sequence = list(FIXED_SEQUENCE)
        self.index = 0
        self.status = "Ready"
        self.last_input = "-"
        self.click_count = 0

        self.sequence_var = tk.StringVar()
        self.expected_var = tk.StringVar()
        self.progress_var = tk.StringVar()
        self.last_var = tk.StringVar()
        self.status_var = tk.StringVar()
        self.text_value = tk.StringVar()
        self.switch_enabled = tk.BooleanVar(value=False)
        self.switch_var = tk.StringVar(value="Off")
        self.dialog_status_var = tk.StringVar()
        self.dialog_value = tk.StringVar()
        self.dialog_choice = tk.StringVar(value="No")
        self.dialog_message_var = tk.StringVar(value="-")
        self.dialog_status = "not_run"
        self.dialog_window: tk.Toplevel | None = None
        self.dialog_entry: ttk.Entry | None = None

        self._build_ui()
        self._refresh_labels()
        self._write_state()
        self._poll_commands()

    def _build_ui(self) -> None:
        self.root.configure(bg="#f3f4f6")
        root = ttk.Frame(self.root, padding=16)
        root.pack(fill="both", expand=True)

        title = ttk.Label(root, text="Simon Target", font=("Segoe UI", 22, "bold"))
        title.pack(anchor="w")

        info = ttk.Frame(root)
        info.pack(fill="x", pady=(10, 14))
        for col in range(2):
            info.columnconfigure(col, weight=1)

        self._add_info(info, 0, 0, "Current sequence", self.sequence_var)
        self._add_info(info, 0, 1, "Current expected color", self.expected_var)
        self._add_info(info, 1, 0, "Progress", self.progress_var)
        self._add_info(info, 1, 1, "Last input", self.last_var)
        self._add_info(info, 2, 0, "Status", self.status_var)
        self._add_info(info, 2, 1, "Shortcut switch", self.switch_var)
        self._add_info(info, 3, 0, "Dialog challenge", self.dialog_status_var)

        controls = ttk.Frame(root)
        controls.pack(fill="x", pady=(0, 12))
        ttk.Button(controls, text="Reset Fixed Test", command=self.reset_fixed).pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="Random 20-Step Test", command=self.random_sequence).pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="Open Dialog Test", command=self.open_dialog_test).pack(side="left")

        text_panel = ttk.LabelFrame(root, text="Keyboard / Paste Test", padding=10)
        text_panel.pack(fill="x", pady=(0, 12))
        text_panel.columnconfigure(1, weight=1)
        ttk.Label(text_panel, text="Text field").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.text_entry = ttk.Entry(text_panel, textvariable=self.text_value, font=("Segoe UI", 12))
        self.text_entry.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        self.switch_check = ttk.Checkbutton(
            text_panel,
            text="Shortcut switch",
            variable=self.switch_enabled,
            command=self._on_switch_changed,
        )
        self.switch_check.grid(row=0, column=2, sticky="e")
        ttk.Label(text_panel, text="Focus text: Ctrl+Shift+F | Toggle switch: Ctrl+Shift+S | Open dialog: Ctrl+Shift+D").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )

        buttons = ttk.Frame(root)
        buttons.pack(fill="both", expand=True)
        for row in range(2):
            buttons.rowconfigure(row, weight=1, uniform="button_rows")
        for col in range(3):
            buttons.columnconfigure(col, weight=1, uniform="button_cols")

        for pos, color in enumerate(COLOR_ORDER):
            spec = COLOR_SPECS[color]
            button = tk.Button(
                buttons,
                text=spec["label"],
                bg=spec["hex"],
                fg=spec["fg"],
                activebackground=spec["hex"],
                activeforeground=spec["fg"],
                relief="raised",
                bd=3,
                font=("Segoe UI", 18, "bold"),
                command=lambda c=color: self.register_click(c),
            )
            button.grid(row=pos // 3, column=pos % 3, padx=10, pady=10, sticky="nsew")

        self.root.bind_all("<Control-Shift-F>", self.focus_text_field)
        self.root.bind_all("<Control-Shift-S>", self.toggle_shortcut_switch)
        self.root.bind_all("<Control-Shift-D>", self.open_dialog_test)
        self.text_value.trace_add("write", lambda *_args: self._write_state())
        self.dialog_value.trace_add("write", lambda *_args: self._write_state())
        self.dialog_choice.trace_add("write", lambda *_args: self._write_state())

    def _add_info(self, parent: ttk.Frame, row: int, col: int, label: str, var: tk.StringVar) -> None:
        frame = ttk.Frame(parent, padding=(0, 4))
        frame.grid(row=row, column=col, sticky="ew", padx=(0 if col == 0 else 12, 0))
        ttk.Label(frame, text=label, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=var, font=("Segoe UI", 11), wraplength=340).pack(anchor="w")

    def reset_fixed(self) -> None:
        self.sequence = list(FIXED_SEQUENCE)
        self.index = 0
        self.status = "Ready"
        self.last_input = "-"
        self.click_count = 0
        self.text_value.set("")
        self.switch_enabled.set(False)
        self._on_switch_changed(write_state=False)
        self._reset_dialog_state()
        self._refresh_labels()
        self._write_state()
        self._log_target("reset", "-", "-", "Ready")

    def random_sequence(self) -> None:
        self.sequence = [random.choice(COLOR_ORDER) for _ in range(20)]
        self.index = 0
        self.status = "Ready"
        self.last_input = "-"
        self.click_count = 0
        self.text_value.set("")
        self.switch_enabled.set(False)
        self._on_switch_changed(write_state=False)
        self._reset_dialog_state()
        self._refresh_labels()
        self._write_state()
        self._log_target("random", "-", "-", "Ready")

    def register_click(self, color: str) -> None:
        expected = self.current_expected()
        previous_status = self.status

        if self.status == "Complete":
            result = "ignored_complete"
        elif color == expected:
            self.index += 1
            self.click_count += 1
            self.last_input = color
            self.status = "Complete" if self.index >= len(self.sequence) else "Correct"
            result = self.status
        else:
            self.click_count += 1
            self.last_input = color
            self.status = "Wrong"
            result = "Wrong"

        self._refresh_labels()
        self._write_state()
        self._log_target("click", expected or "-", color, result, previous_status)

    def current_expected(self) -> str | None:
        if self.index >= len(self.sequence):
            return None
        return self.sequence[self.index]

    def focus_text_field(self, _event: tk.Event | None = None) -> None:
        self.text_entry.focus_set()
        self.text_entry.selection_range(0, "end")
        self._log_target("focus_text", "-", "-", "focused")

    def toggle_shortcut_switch(self, _event: tk.Event | None = None) -> None:
        self.switch_enabled.set(not self.switch_enabled.get())
        self._on_switch_changed()
        self._log_target("shortcut_toggle", "-", "-", self.switch_var.get())

    def _on_switch_changed(self, write_state: bool = True) -> None:
        self.switch_var.set("On" if self.switch_enabled.get() else "Off")
        if write_state:
            self._write_state()

    def open_dialog_test(self, _event: tk.Event | None = None) -> None:
        if self._dialog_is_open():
            assert self.dialog_window is not None
            self.dialog_window.lift()
            self.dialog_window.focus_force()
            if self.dialog_entry is not None:
                self.dialog_entry.focus_set()
                self.dialog_entry.selection_range(0, "end")
            return

        self.dialog_status = "open"
        self.dialog_value.set("")
        self.dialog_choice.set("No")
        self.dialog_message_var.set("Enter 10, select Yes, then click OK.")

        dialog = tk.Toplevel(self.root)
        self.dialog_window = dialog
        dialog.title(DIALOG_TITLE)
        dialog.geometry("420x260")
        dialog.minsize(380, 240)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.protocol("WM_DELETE_WINDOW", self.cancel_dialog_test)

        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Dialog Challenge", font=("Segoe UI", 16, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 14)
        )
        ttk.Label(frame, text="Number").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(0, 10))
        self.dialog_entry = ttk.Entry(frame, textvariable=self.dialog_value, font=("Segoe UI", 12))
        self.dialog_entry.grid(row=1, column=1, sticky="ew", pady=(0, 10))

        radio_frame = ttk.Frame(frame)
        radio_frame.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 12))
        ttk.Radiobutton(radio_frame, text="Yes", value="Yes", variable=self.dialog_choice).pack(side="left", padx=(0, 18))
        ttk.Radiobutton(radio_frame, text="No", value="No", variable=self.dialog_choice).pack(side="left")

        ttk.Label(frame, textvariable=self.dialog_message_var, foreground="#4b5563").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(0, 16)
        )

        button_row = ttk.Frame(frame)
        button_row.grid(row=4, column=0, columnspan=2, sticky="e")
        ttk.Button(button_row, text="OK", command=self.submit_dialog_test).pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="Cancel", command=self.cancel_dialog_test).pack(side="left")

        dialog.bind("<Return>", lambda _event: self.submit_dialog_test())
        dialog.after(50, self._focus_dialog_entry)
        self._refresh_labels()
        self._write_state()
        self._log_target("dialog_open", "-", "-", "open")

    def submit_dialog_test(self) -> None:
        number = self.dialog_value.get().strip()
        choice = self.dialog_choice.get()
        if number == DIALOG_EXPECTED_NUMBER and choice == DIALOG_EXPECTED_CHOICE:
            self.dialog_status = "passed"
            self.dialog_message_var.set("Passed.")
            self._close_dialog_window()
            result = "passed"
        else:
            self.dialog_status = "failed"
            self.dialog_message_var.set("Expected number 10 and Yes selected.")
            result = "failed"
        self._refresh_labels()
        self._write_state()
        self._log_target("dialog_ok", DIALOG_EXPECTED_NUMBER, f"{number}/{choice}", result)

    def cancel_dialog_test(self) -> None:
        self.dialog_status = "cancelled"
        self.dialog_message_var.set("Cancelled.")
        self._close_dialog_window()
        self._refresh_labels()
        self._write_state()
        self._log_target("dialog_cancel", DIALOG_EXPECTED_NUMBER, "-", "cancelled")

    def _reset_dialog_state(self) -> None:
        self._close_dialog_window()
        self.dialog_status = "not_run"
        self.dialog_value.set("")
        self.dialog_choice.set("No")
        self.dialog_message_var.set("-")

    def _focus_dialog_entry(self) -> None:
        if self.dialog_entry is not None and self._dialog_is_open():
            self.dialog_entry.focus_set()
            self.dialog_entry.selection_range(0, "end")

    def _close_dialog_window(self) -> None:
        if self.dialog_window is not None:
            try:
                if self.dialog_window.winfo_exists():
                    self.dialog_window.destroy()
            except tk.TclError:
                pass
        self.dialog_window = None
        self.dialog_entry = None

    def _dialog_is_open(self) -> bool:
        if self.dialog_window is None:
            return False
        try:
            return bool(self.dialog_window.winfo_exists())
        except tk.TclError:
            return False

    def _state_payload(self) -> dict[str, Any]:
        dialog_open = self._dialog_is_open()
        return {
            "app": "Simon Target",
            "updated_at": now_iso(),
            "sequence": self.sequence,
            "index": self.index,
            "expected_color": self.current_expected(),
            "progress_text": f"{self.index} / {len(self.sequence)}",
            "last_input": self.last_input,
            "status": self.status,
            "click_count": self.click_count,
            "complete": self.status == "Complete",
            "text_value": self.text_value.get(),
            "switch_enabled": self.switch_enabled.get(),
            "switch_text": self.switch_var.get(),
            "dialog_open": dialog_open,
            "dialog_status": self.dialog_status,
            "dialog_number_value": self.dialog_value.get(),
            "dialog_choice": self.dialog_choice.get(),
            "dialog_passed": self.dialog_status == "passed" and not dialog_open,
            "dialog_expected_number": DIALOG_EXPECTED_NUMBER,
            "dialog_expected_choice": DIALOG_EXPECTED_CHOICE,
            "shortcuts": {
                "focus_text": "ctrl+shift+f",
                "toggle_switch": "ctrl+shift+s",
                "open_dialog": "ctrl+shift+d",
            },
        }

    def _write_state(self) -> None:
        write_json_atomic(STATE_FILE, self._state_payload())

    def _refresh_labels(self) -> None:
        expected = self.current_expected()
        self.sequence_var.set(", ".join(self.sequence))
        self.expected_var.set(expected.title() if expected else "-")
        self.progress_var.set(f"{self.index} / {len(self.sequence)}")
        self.last_var.set(self.last_input.title() if self.last_input != "-" else "-")
        self.status_var.set(self.status)
        if self._dialog_is_open():
            number = self.dialog_value.get() or "-"
            choice = self.dialog_choice.get() or "-"
            self.dialog_status_var.set(f"Open ({number}, {choice})")
        else:
            self.dialog_status_var.set(self.dialog_status.replace("_", " ").title())

    def _log_target(
        self,
        action: str,
        expected: str,
        received: str,
        result: str,
        previous_status: str | None = None,
    ) -> None:
        line = (
            f"{now_stamp()} | {action} | expected={expected} | received={received} | "
            f"progress={self.index}/{len(self.sequence)} | status={self.status} | result={result}"
        )
        if previous_status:
            line += f" | previous_status={previous_status}"
        append_log(TARGET_LOG_FILE, line)

    def _poll_commands(self) -> None:
        command = read_json(COMMAND_FILE)
        if command and command.get("command") == "reset":
            try:
                COMMAND_FILE.unlink()
            except OSError:
                pass
            self.reset_fixed()
        self.root.after(200, self._poll_commands)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    SimonTargetApp().run()
