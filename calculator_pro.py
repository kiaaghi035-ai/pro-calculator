"""
🧮 Professional Calculator
Requirements: Python 3.14+
Features:
  - OOP architecture
  - ThreadPoolExecutor for heavy ops
  - multiprocessing for isolated safe evaluation
  - Tkinter UI with dark modern theme
  - Keyboard support, history, memory, scientific functions
  - Thread-safe UI updates via root.after()
"""

from __future__ import annotations

import math
import multiprocessing as mp
import operator
import queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import tkinter as tk
from tkinter import ttk, font as tkfont


# ─────────────────────────────────────────────────────────────────────────────
# 1) DATA MODEL
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(slots=True, frozen=True)
class HistoryEntry:
    expression: str
    result: str
    timestamp: datetime = field(default_factory=datetime.now)

    def __str__(self) -> str:
        return f"[{self.timestamp:%H:%M:%S}] {self.expression} = {self.result}"


# ─────────────────────────────────────────────────────────────────────────────
# 2) SAFE EXPRESSION EVALUATOR (runs in a separate PROCESS)
# ─────────────────────────────────────────────────────────────────────────────

_ALLOWED_NAMES: dict[str, Callable] = {
    "abs": abs, "round": round, "min": min, "max": max,
    "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "log": math.log, "log10": math.log10, "exp": math.exp,
    "floor": math.floor, "ceil": math.ceil, "pi": math.pi, "e": math.e,
    "pow": pow,
}

_TOKEN_RE = re.compile(r"[A-Za-z_]\w*")


def _safe_eval(expr: str) -> float:
    """Evaluate a math expression with a restricted namespace."""
    expr = expr.strip()
    if not expr:
        raise ValueError("Empty expression")

    # Block dangerous dunder / attribute access
    if "__" in expr or ";" in expr:
        raise ValueError("Forbidden tokens in expression")

    # Validate identifiers
    for name in _TOKEN_RE.findall(expr):
        if name not in _ALLOWED_NAMES:
            raise ValueError(f"Unknown identifier: {name}")

    # Translate ^ to ** for convenience
    expr = expr.replace("^", "**")

    return float(eval(expr, {"__builtins__": {}}, _ALLOWED_NAMES))  # noqa: S307


def _process_worker(expr: str, result_queue: "mp.Queue[str]") -> None:
    """Entry point for the child process."""
    try:
        val = _safe_eval(expr)
        result_queue.put(f"OK:{val!r}")
    except ZeroDivisionError:
        result_queue.put("ERR:Division by zero")
    except Exception as e:  # noqa: BLE001
        result_queue.put(f"ERR:{type(e).__name__}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# 3) THREAD-POOL WRAPPER
# ─────────────────────────────────────────────────────────────────────────────

class WorkerPool:
    """Shared ThreadPoolExecutor for non-blocking computations."""

    def __init__(self, max_workers: int = 4) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="calc")
        self._lock = threading.Lock()

    def submit(self, fn: Callable, *args) -> Future:
        with self._lock:
            return self._pool.submit(fn, *args)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


# ─────────────────────────────────────────────────────────────────────────────
# 4) CALCULATOR ENGINE (pure logic, UI-agnostic)
# ─────────────────────────────────────────────────────────────────────────────

class CalculatorEngine:
    """Core calculator logic. Thread-safe."""

    _OPS: dict[str, Callable[[float, float], float]] = {
        "+": operator.add,
        "-": operator.sub,
        "*": operator.mul,
        "/": operator.truediv,
        "%": operator.mod,
        "**": operator.pow,
    }

    def __init__(self, pool: WorkerPool) -> None:
        self._pool = pool
        self._history: list[HistoryEntry] = []
        self._history_lock = threading.Lock()
        self.memory: float = 0.0

    # ---------- basic ops ----------
    def calculate(self, a: float, op: str, b: float) -> float:
        if op not in self._OPS:
            raise ValueError(f"Unsupported operator: {op}")
        if op in ("/", "%") and b == 0:
            raise ZeroDivisionError("Division by zero")
        return self._OPS[op](a, b)

    # ---------- heavy ops (run in thread pool) ----------
    def factorial(self, n: int) -> int:
        if n < 0 or n > 1000:
            raise ValueError("n must be 0..1000")
        return math.factorial(n)

    def safe_division(self, a: float, b: float) -> float:
        if b == 0:
            raise ZeroDivisionError("Division by zero")
        return a / b

    # ---------- history ----------
    def add_history(self, expr: str, result: str) -> None:
        with self._history_lock:
            self._history.append(HistoryEntry(expr, result))
            if len(self._history) > 200:
                self._history.pop(0)

    def get_history(self) -> list[HistoryEntry]:
        with self._history_lock:
            return list(self._history)

    def clear_history(self) -> None:
        with self._history_lock:
            self._history.clear()


# ─────────────────────────────────────────────────────────────────────────────
# 5) EXPRESSION DISPATCHER (bridges UI ↔ multiprocessing)
# ─────────────────────────────────────────────────────────────────────────────

class ProcessEvaluator:
    """
    Evaluates expressions in a fresh `multiprocessing.Process` so a crash
    can never take down the UI.
    """

    def __init__(self) -> None:
        self._ctx = mp.get_context("spawn")  # safe on all platforms
        self._q: mp.Queue | None = None
        self._proc: mp.Process | None = None
        self._busy = threading.Lock()

    def evaluate(self, expr: str, on_done: Callable[[str, str | None], None]) -> None:
        if not self._busy.acquire(blocking=False):
            on_done("", "Busy: another evaluation is running")
            return

        self._q = self._ctx.Queue()
        self._proc = self._ctx.Process(
            target=_process_worker, args=(expr, self._q), daemon=True
        )
        self._proc.start()

        def _watch() -> None:
            try:
                msg = self._q.get(timeout=5.0)
            except queue.Empty:
                msg = "ERR:Timeout"
            finally:
                try:
                    self._proc.join(timeout=0.5)
                    if self._proc.is_alive():
                        self._proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
                self._busy.release()

            if msg.startswith("OK:"):
                on_done(expr, msg[3:])
            else:
                on_done(expr, None)  # signal caller to show msg[4:]

        threading.Thread(target=_watch, daemon=True).start()

# ─────────────────────────────────────────────────────────────────────────────
# 6) TKINTER UI  (Dark Modern Theme)
# ─────────────────────────────────────────────────────────────────────────────

# ── Color palette (Catppuccin Mocha inspired) ────────────────────────────────
class Theme:
    BG        = "#1e1e2e"
    BG_ALT    = "#181825"
    PANEL     = "#313244"
    PANEL_HOV = "#45475a"
    DISPLAY   = "#11111b"
    TEXT      = "#cdd6f4"
    TEXT_DIM  = "#6c7086"
    ACCENT    = "#89b4fa"   # blue
    ACCENT_H  = "#b4befe"
    GREEN     = "#a6e3a1"
    RED       = "#f38ba8"
    YELLOW    = "#f9e2af"
    MAUVE     = "#cba6f7"
    PEACH     = "#fab387"


class CalculatorApp(tk.Tk):
    """
    Professional calculator UI.

    Threading model:
      * Tkinter runs on the MAIN thread only.
      * Heavy work goes through `WorkerPool` (threads) or
        `ProcessEvaluator` (separate process).
      * Results are marshalled back with `self.after(0, ...)` → thread-safe.
    """

    # Button layout: (label, row, col, kind)
    # kind: 'num' | 'op' | 'eq' | 'fn' | 'mem' | 'sci'
    _BUTTONS: list[tuple[str, int, int, str]] = [
        ("MC", 2, 0, "mem"), ("MR", 2, 1, "mem"), ("M+", 2, 2, "mem"), ("M-", 2, 3, "mem"),
        ("C",  3, 0, "fn"),  ("⌫",  3, 1, "fn"),  ("%",  3, 2, "op"),  ("÷",  3, 3, "op"),
        ("7",  4, 0, "num"), ("8",  4, 1, "num"), ("9",  4, 2, "num"), ("×",  4, 3, "op"),
        ("4",  5, 0, "num"), ("5",  5, 1, "num"), ("6",  5, 2, "num"), ("−",  5, 3, "op"),
        ("1",  6, 0, "num"), ("2",  6, 1, "num"), ("3",  6, 2, "num"), ("+",  6, 3, "op"),
        ("±",  7, 0, "fn"),  ("0",  7, 1, "num"), (".",  7, 2, "num"), ("=",  7, 3, "eq"),
    ]

    _SCI_BUTTONS: list[tuple[str, str]] = [
        ("sin", "sin("), ("cos", "cos("), ("tan", "tan("),
        ("√",   "sqrt("),("log", "log10("),("ln", "log("),
        ("x²",  "**2"),  ("xʸ",  "^"),     ("x!",  "!"),
        ("π",   "pi"),   ("e",   "e"),     ("1/x", "1/"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.title("🧮 Pro Calculator — OOP · Threads · Multiprocess")
        self.geometry("980x720")
        self.minsize(820, 640)
        self.configure(bg=Theme.BG)

        # ── core services ────────────────────────────────────────────────
        self.pool = WorkerPool(max_workers=4)
        self.engine = CalculatorEngine(self.pool)
        self.evaluator = ProcessEvaluator()

        # ── UI state ─────────────────────────────────────────────────────
        self._expr: str = ""
        self._last_result: str = ""
        self._evaluating: bool = False
        self._mem: float = 0.0
        self._buttons: dict[str, tk.Button] = {}

        # ── fonts ────────────────────────────────────────────────────────
        self._f_display   = tkfont.Font(family="Segoe UI", size=30, weight="bold")
        self._f_sub       = tkfont.Font(family="Segoe UI", size=14)
        self._f_btn       = tkfont.Font(family="Segoe UI", size=15, weight="bold")
        self._f_sci       = tkfont.Font(family="Segoe UI", size=11)
        self._f_hist      = tkfont.Font(family="Consolas", size=10)
        self._f_hist_head = tkfont.Font(family="Segoe UI", size=12, weight="bold")

        # ── build UI ─────────────────────────────────────────────────────
        self._build_layout()
        self._build_display()
        self._build_keypad()
        self._build_scientific()
        self._build_history()
        self._bind_keys()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── layout skeleton ─────────────────────────────────────────────────
    def _build_layout(self) -> None:
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=2)

        self.left = tk.Frame(self, bg=Theme.BG)
        self.left.grid(row=0, column=0, sticky="nsew", padx=(14, 7), pady=14)

        self.right = tk.Frame(self, bg=Theme.BG_ALT)
        self.right.grid(row=0, column=1, sticky="nsew", padx=(7, 14), pady=14)

        for i in range(4):
            self.left.grid_rowconfigure(i, weight=0)
        self.left.grid_rowconfigure(2, weight=1)
        self.left.grid_columnconfigure(0, weight=1)

    # ── display ─────────────────────────────────────────────────────────
    def _build_display(self) -> None:
        frame = tk.Frame(self.left, bg=Theme.DISPLAY, padx=14, pady=12)
        frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        self.lbl_history = tk.Label(
            frame, text="", anchor="e", bg=Theme.DISPLAY,
            fg=Theme.TEXT_DIM, font=self._f_sub,
        )
        self.lbl_history.pack(fill="x")

        self.lbl_main = tk.Label(
            frame, text="0", anchor="e", bg=Theme.DISPLAY,
            fg=Theme.TEXT, font=self._f_display, height=1,
        )
        self.lbl_main.pack(fill="x", pady=(2, 0))

        self.lbl_status = tk.Label(
            self.left, text="Ready", anchor="w", bg=Theme.BG,
            fg=Theme.TEXT_DIM, font=("Segoe UI", 9),
        )
        self.lbl_status.grid(row=1, column=0, sticky="ew", pady=(0, 6))

    # ── keypad ──────────────────────────────────────────────────────────
    def _build_keypad(self) -> None:
        grid = tk.Frame(self.left, bg=Theme.BG)
        grid.grid(row=2, column=0, sticky="nsew")
        for c in range(4):
            grid.grid_columnconfigure(c, weight=1, uniform="key")
        for r in range(2, 8):
            grid.grid_rowconfigure(r, weight=1)

        for label, r, c, kind in self._BUTTONS:
            btn = self._make_button(grid, label, kind)
            btn.grid(row=r, column=c, sticky="nsew", padx=3, pady=3)
            self._buttons[label] = btn

    # ── scientific panel ────────────────────────────────────────────────
    def _build_scientific(self) -> None:
        frame = tk.Frame(self.left, bg=Theme.BG)
        frame.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        for c in range(6):
            frame.grid_columnconfigure(c, weight=1, uniform="sci")

        for i, (label, token) in enumerate(self._SCI_BUTTONS):
            r, c = divmod(i, 6)
            b = self._make_button(
                frame, label, "sci",
                command=lambda t=token, l=label: self._on_sci(t, l),
            )
            b.grid(row=r, column=c, sticky="nsew", padx=2, pady=2)

    # ── history panel ───────────────────────────────────────────────────
    def _build_history(self) -> None:
        self.right.grid_rowconfigure(1, weight=1)
        self.right.grid_columnconfigure(0, weight=1)

        header = tk.Frame(self.right, bg=Theme.BG_ALT)
        header.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        header.grid_columnconfigure(0, weight=1)

        tk.Label(
            header, text="📜 History", bg=Theme.BG_ALT, fg=Theme.ACCENT,
            font=self._f_hist_head,
        ).grid(row=0, column=0, sticky="w")

        tk.Button(
            header, text="Clear", command=self._clear_history,
            bg=Theme.PANEL, fg=Theme.RED, relief="flat", bd=0,
            font=("Segoe UI", 9), cursor="hand2",
            activebackground=Theme.PANEL_HOV, activeforeground=Theme.RED,
        ).grid(row=0, column=1, sticky="e")

        list_frame = tk.Frame(self.right, bg=Theme.BG_ALT)
        list_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)

        self.hist_list = tk.Listbox(
            list_frame, bg=Theme.DISPLAY, fg=Theme.TEXT,
            selectbackground=Theme.ACCENT, selectforeground=Theme.BG,
            font=self._f_hist, relief="flat", bd=0, highlightthickness=0,
            activestyle="none",
        )
        self.hist_list.grid(row=0, column=0, sticky="nsew")

        sb = ttk.Scrollbar(list_frame, orient="vertical",
                           command=self.hist_list.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.hist_list.configure(yscrollcommand=sb.set)
        self.hist_list.bind("<Double-Button-1>", self._on_history_pick)

    # ── button factory ──────────────────────────────────────────────────
    def _make_button(
        self, parent: tk.Misc, label: str, kind: str,
        command: Callable[[], None] | None = None,
    ) -> tk.Button:
        bg, fg, hover = self._colors_for(kind)
        f = self._f_sci if kind == "sci" else self._f_btn

        btn = tk.Button(
            parent, text=label, font=f, bg=bg, fg=fg,
            relief="flat", bd=0, cursor="hand2",
            activebackground=hover, activeforeground=fg,
            command=command if command else (lambda l=label, k=kind: self._on_button(l, k)),
        )
        btn.bind("<Enter>", lambda _e, b=btn, h=hover: b.configure(bg=h))
        btn.bind("<Leave>", lambda _e, b=btn, o=bg:    b.configure(bg=o))
        return btn

    @staticmethod
    def _colors_for(kind: str) -> tuple[str, str, str]:
        return {
            "num": (Theme.PANEL,     Theme.TEXT,   Theme.PANEL_HOV),
            "op":  (Theme.MAUVE,     Theme.BG,     Theme.ACCENT_H),
            "eq":  (Theme.ACCENT,    Theme.BG,     Theme.ACCENT_H),
            "fn":  (Theme.PEACH,     Theme.BG,     Theme.YELLOW),
            "mem": (Theme.GREEN,     Theme.BG,     Theme.YELLOW),
            "sci": (Theme.BG_ALT,    Theme.ACCENT, Theme.PANEL),
        }[kind]

    # ── keyboard ────────────────────────────────────────────────────────
    def _bind_keys(self) -> None:
        for ch in "0123456789.":
            self.bind(ch, lambda _e, c=ch: self._on_button(c, "num"))
        self.bind("+", lambda _e: self._on_button("+", "op"))
        self.bind("-", lambda _e: self._on_button("−", "op"))
        self.bind("*", lambda _e: self._on_button("×", "op"))
        self.bind("/", lambda _e: self._on_button("÷", "op"))
        self.bind("%", lambda _e: self._on_button("%", "op"))
        self.bind("<Return>",  lambda _e: self._on_button("=", "eq"))
        self.bind("<KP_Enter>", lambda _e: self._on_button("=", "eq"))
        self.bind("<BackSpace>", lambda _e: self._on_button("⌫", "fn"))
        self.bind("<Escape>",  lambda _e: self._on_button("C", "fn"))

    # ── input handling ──────────────────────────────────────────────────
    def _on_button(self, label: str, kind: str) -> None:
        if self._evaluating:
            return

        if kind == "num":
            self._append(label)
        elif kind == "op":
            self._append(self._map_op(label))
        elif kind == "eq":
            self._evaluate()
        elif kind == "fn":
            self._handle_fn(label)
        elif kind == "mem":
            self._handle_mem(label)

    @staticmethod
    def _map_op(label: str) -> str:
        return {"×": "*", "÷": "/", "−": "-"}.get(label, label)

    def _handle_fn(self, label: str) -> None:
        if label == "C":
            self._expr = ""
            self._last_result = ""
            self.lbl_history.configure(text="")
            self._refresh_display()
        elif label == "⌫":
            self._expr = self._expr[:-1]
            self._refresh_display()
        elif label == "±":
            if self._expr and not self._expr.startswith("-"):
                self._expr = "-" + self._expr
            elif self._expr.startswith("-"):
                self._expr = self._expr[1:]
            self._refresh_display()

    def _handle_mem(self, label: str) -> None:
        try:
            current = float(self._expr or "0")
        except ValueError:
            return
        if label == "MC":
            self._mem = 0.0
        elif label == "MR":
            self._expr = self._fmt(self._mem)
            self._refresh_display()
        elif label == "M+":
            self._mem += current
        elif label == "M-":
            self._mem -= current
        self._set_status(f"M = {self._fmt(self._mem)}")

    def _on_sci(self, token: str, label: str) -> None:
        if self._evaluating:
            return
        if label == "x!":
            self._evaluate_factorial()
            return
        if token == "**2":
            self._expr += "**2"
        elif token == "^":
            self._expr += "^"
        elif token == "1/":
            self._expr = "1/(" + (self._expr or "0") + ")"
        else:
            self._expr += token
        self._refresh_display()

    def _append(self, text: str) -> None:
        self._expr += text
        self._refresh_display()

    # ── evaluation ──────────────────────────────────────────────────────
    def _evaluate(self) -> None:
        if not self._expr.strip():
            return
        expr = self._expr
        self._set_status("⏳ Computing…")
        self._evaluating = True

        def done(_e: str, result: str | None) -> None:
            # NOTE: runs on a worker thread → marshal to main thread
            self.after(0, lambda: self._apply_result(expr, result))

        self.evaluator.evaluate(expr, done)

    def _apply_result(self, expr: str, result: str | None) -> None:
        self._evaluating = False
        if result is None:
            self._set_status("❌ Evaluation failed")
            self._refresh_display()
            return

        self._last_result = result
        self.lbl_history.configure(text=f"{expr} =")
        self.lbl_main.configure(text=result, fg=Theme.GREEN)
        self.engine.add_history(expr, result)
        self._push_history()
        self._expr = result
        self._set_status("✅ Done")

    def _evaluate_factorial(self) -> None:
        raw = self._expr.strip()
        try:
            n = int(float(raw))
        except ValueError:
            self._set_status("❌ x! needs an integer")
            return
        self._set_status("⏳ x! in thread pool…")
        fut: Future = self.pool.submit(self.engine.factorial, n)
        fut.add_done_callback(
            lambda f: self.after(0, lambda: self._factorial_done(raw, f))
        )

    def _factorial_done(self, raw: str, fut: Future) -> None:
        try:
            res = fut.result()
            self._expr = str(res)
            self.lbl_history.configure(text=f"{raw}! =")
            self.lbl_main.configure(text=str(res), fg=Theme.GREEN)
            self.engine.add_history(f"{raw}!", str(res))
            self._push_history()
            self._set_status("✅ Done (thread)")
        except Exception as e:  # noqa: BLE001
            self._set_status(f"❌ {e}")
            self._refresh_display()

    # ── history helpers ─────────────────────────────────────────────────
    def _push_history(self) -> None:
        self.hist_list.delete(0, tk.END)
        for entry in reversed(self.engine.get_history()):
            self.hist_list.insert(tk.END, str(entry))

    def _clear_history(self) -> None:
        self.engine.clear_history()
        self._push_history()

    def _on_history_pick(self, _evt) -> None:
        sel = self.hist_list.curselection()
        if not sel:
            return
        text = self.hist_list.get(sel[0])
        if " = " in text:
            self._expr = text.rsplit(" = ", 1)[1]
            self._refresh_display()

    # ── display helpers ─────────────────────────────────────────────────
    def _refresh_display(self) -> None:
        self.lbl_main.configure(text=self._expr or "0", fg=Theme.TEXT)

    def _set_status(self, msg: str) -> None:
        self.lbl_status.configure(text=msg)

    @staticmethod
    def _fmt(x: float) -> str:
        return str(int(x)) if x == int(x) else f"{x:.10g}"

    # ── shutdown ────────────────────────────────────────────────────────
    def _on_close(self) -> None:
        try:
            self.pool.shutdown()
        finally:
            self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# 7) MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    # Required for 'spawn' start method on Windows / macOS
    mp.freeze_support()
    app = CalculatorApp()
    app.mainloop()


if __name__ == "__main__":
    main()