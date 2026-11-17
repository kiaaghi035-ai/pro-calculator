"""
🧮 Pro Calculator v2.1
Features:
  - OOP + ThreadPool + Multiprocessing
  - Tkinter dark UI
  - Authentication (Register / Login) stored in SQLite
  - Scrollable auth form (mouse wheel + scrollbar)
  - Per-user history + persistent logs

Requirements: Python 3.14+   pip install bcrypt (optional)
"""

from __future__ import annotations

import hashlib
import logging
import math
import multiprocessing as mp
import operator
import queue
import re
import secrets
import sqlite3
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk

try:
    import bcrypt  # type: ignore
    _HAS_BCRYPT = True
except ImportError:
    _HAS_BCRYPT = False


# ─────────────────────────────────────────────────────────────────────────────
# 0) LOGGING
# ─────────────────────────────────────────────────────────────────────────────

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("calc")


# ─────────────────────────────────────────────────────────────────────────────
# 1) DATABASE
# ─────────────────────────────────────────────────────────────────────────────

DB_PATH = Path(__file__).resolve().parent / "calculator.db"


class Database:
    def __init__(self, path: Path = DB_PATH) -> None:
        self._path = path
        self._local = threading.local()
        self._init_schema()
        log.info("Database ready at %s", self._path)

    @property
    def conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            c = sqlite3.connect(self._path, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL;")
            c.execute("PRAGMA foreign_keys=ON;")
            self._local.conn = c
        return self._local.conn

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        cur = self.conn.cursor()
        try:
            yield cur
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cur.close()

    def _init_schema(self) -> None:
        with self.cursor() as cur:
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    first_name TEXT NOT NULL,
                    last_name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    username TEXT NOT NULL UNIQUE,
                    pwd_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS history(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    expression TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    action TEXT NOT NULL,
                    detail TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                """
            )

    def create_user(self, first_name, last_name, phone, username, password) -> int:
        pwd_hash = hash_password(password)
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO users(first_name,last_name,phone,username,pwd_hash)
                   VALUES(?,?,?,?,?)""",
                (first_name, last_name, phone, username, pwd_hash),
            )
            uid = cur.lastrowid
        self.audit(uid, "register", f"user={username}")
        log.info("User registered: %s (id=%s)", username, uid)
        return uid

    def verify_user(self, username, password) -> sqlite3.Row | None:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE username=?", (username,))
            row = cur.fetchone()
        if row and verify_password(password, row["pwd_hash"]):
            self.audit(row["id"], "login", f"user={username}")
            log.info("Login OK: %s", username)
            return row
        self.audit(None, "login_fail", f"user={username}")
        log.warning("Login FAILED: %s", username)
        return None

    def add_history(self, user_id, expr, result) -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO history(user_id,expression,result) VALUES(?,?,?)",
                (user_id, expr, result),
            )

    def get_history(self, user_id, limit=100) -> list[sqlite3.Row]:
        with self.cursor() as cur:
            cur.execute(
                """SELECT expression,result,created_at FROM history
                   WHERE user_id=? ORDER BY id DESC LIMIT ?""",
                (user_id, limit),
            )
            return cur.fetchall()

    def clear_history(self, user_id) -> None:
        with self.cursor() as cur:
            cur.execute("DELETE FROM history WHERE user_id=?", (user_id,))
        self.audit(user_id, "history_clear", "")

    def audit(self, user_id, action, detail="") -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO audit(user_id,action,detail) VALUES(?,?,?)",
                (user_id, action, detail),
            )


def hash_password(password: str) -> str:
    if _HAS_BCRYPT:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"pbkdf2${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if stored.startswith("$2"):
        if not _HAS_BCRYPT:
            return False
        return bcrypt.checkpw(password.encode(), stored.encode())
    if stored.startswith("pbkdf2$"):
        _, salt, hexdk = stored.split("$", 2)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
        return secrets.compare_digest(dk.hex(), hexdk)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# 2) MODEL
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(slots=True, frozen=True)
class HistoryEntry:
    expression: str
    result: str
    timestamp: datetime = field(default_factory=datetime.now)

    def __str__(self) -> str:
        return f"[{self.timestamp:%H:%M:%S}] {self.expression} = {self.result}"


@dataclass(slots=True)
class User:
    id: int
    first_name: str
    last_name: str
    phone: str
    username: str

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


# ─────────────────────────────────────────────────────────────────────────────
# 3) SAFE EVAL
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
    expr = expr.strip()
    if not expr:
        raise ValueError("Empty expression")
    if "__" in expr or ";" in expr:
        raise ValueError("Forbidden tokens")
    for name in _TOKEN_RE.findall(expr):
        if name not in _ALLOWED_NAMES:
            raise ValueError(f"Unknown identifier: {name}")
    expr = expr.replace("^", "**")
    return float(eval(expr, {"__builtins__": {}}, _ALLOWED_NAMES))  # noqa: S307


def _process_worker(expr: str, q: "mp.Queue[str]") -> None:
    try:
        q.put(f"OK:{_safe_eval(expr)!r}")
    except ZeroDivisionError:
        q.put("ERR:Division by zero")
    except Exception as e:  # noqa: BLE001
        q.put(f"ERR:{type(e).__name__}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# 4) WORKERS
# ─────────────────────────────────────────────────────────────────────────────

class WorkerPool:
    def __init__(self, max_workers: int = 4) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="calc")

    def submit(self, fn: Callable, *args) -> Future:
        return self._pool.submit(fn, *args)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class ProcessEvaluator:
    def __init__(self) -> None:
        self._ctx = mp.get_context("spawn")
        self._busy = threading.Lock()

    def evaluate(self, expr: str, on_done: Callable[[str, str | None], None]) -> None:
        if not self._busy.acquire(blocking=False):
            on_done(expr, None)
            return
        q = self._ctx.Queue()
        proc = self._ctx.Process(target=_process_worker, args=(expr, q), daemon=True)
        proc.start()

        def _watch() -> None:
            try:
                msg = q.get(timeout=5.0)
            except queue.Empty:
                msg = "ERR:Timeout"
            finally:
                proc.join(timeout=0.5)
                if proc.is_alive():
                    proc.terminate()
                self._busy.release()
            on_done(expr, msg[3:] if msg.startswith("OK:") else None)

        threading.Thread(target=_watch, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
# 5) ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class CalculatorEngine:
    _OPS: dict[str, Callable[[float, float], float]] = {
        "+": operator.add, "-": operator.sub, "*": operator.mul,
        "/": operator.truediv, "%": operator.mod, "**": operator.pow,
    }

    def __init__(self, pool: WorkerPool, db: Database, user: User) -> None:
        self._pool = pool
        self._db = db
        self._user = user
        self.memory: float = 0.0

    def calculate(self, a, op, b):
        if op not in self._OPS:
            raise ValueError(f"Unsupported operator: {op}")
        if op in ("/", "%") and b == 0:
            raise ZeroDivisionError("Division by zero")
        return self._OPS[op](a, b)

    def factorial(self, n: int) -> int:
        if n < 0 or n > 1000:
            raise ValueError("n must be 0..1000")
        return math.factorial(n)

    def record(self, expr, result) -> None:
        self._db.add_history(self._user.id, expr, result)

    def history(self):
        return self._db.get_history(self._user.id)

    def clear_history(self) -> None:
        self._db.clear_history(self._user.id)


# ─────────────────────────────────────────────────────────────────────────────
# 6) THEME
# ─────────────────────────────────────────────────────────────────────────────

class Theme:
    BG        = "#1e1e2e"
    BG_ALT    = "#181825"
    PANEL     = "#313244"
    PANEL_HOV = "#45475a"
    DISPLAY   = "#11111b"
    TEXT      = "#cdd6f4"
    TEXT_DIM  = "#6c7086"
    ACCENT    = "#89b4fa"
    ACCENT_H  = "#b4befe"
    GREEN     = "#a6e3a1"
    RED       = "#f38ba8"
    YELLOW    = "#f9e2af"
    MAUVE     = "#cba6f7"
    PEACH     = "#fab387"


# ─────────────────────────────────────────────────────────────────────────────
# 7) SCROLLABLE FRAME  (★ راه‌حل مشکل اسکرول)
# ─────────────────────────────────────────────────────────────────────────────

class ScrollableFrame(tk.Frame):
    """
    یک Frame قابل اسکرول.
      - چرخ موس (Mouse Wheel) → اسکرول
      - Scrollbar → اسکرول
      - روی هر سه سیستم‌عامل (Linux/Mac/Windows) کار می‌کند
    محتوا را داخل `self.inner` قرار بده.
    """

    def __init__(self, parent: tk.Misc, bg: str = Theme.BG, **kw) -> None:
        super().__init__(parent, bg=bg, **kw)

        self._canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self._canvas.pack(side="left", fill="both", expand=True)

        self._sb = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._sb.pack(side="right", fill="y")
        self._canvas.configure(yscrollcommand=self._sb.set)

        self.inner = tk.Frame(self._canvas, bg=bg)
        self._win = self._canvas.create_window((0, 0), window=self.inner, anchor="nw")

        # وقتی محتوا تغییر اندازه داد → scrollregion رو آپدیت کن
        self.inner.bind(
            "<Configure>",
            lambda _e: self._canvas.configure(scrollregion=self._canvas.bbox("all")),
        )
        # وقتی canvas تغییر اندازه داد → عرض inner رو هم‌عرض canvas کن
        self._canvas.bind(
            "<Configure>",
            lambda e: self._canvas.itemconfigure(self._win, width=e.width),
        )

        # اتصال چرخ موس روی خودِ این ویجت و همه‌ی فرزندان
        self._bind_wheel(self)

    # ---------- wheel ----------
    def _bind_wheel(self, widget: tk.Misc) -> None:
        # Windows / macOS
        widget.bind_all("<MouseWheel>", self._on_wheel, add="+")
        # Linux
        widget.bind_all("<Button-4>", lambda _e: self._canvas.yview_scroll(-1, "units"), add="+")
        widget.bind_all("<Button-5>", lambda _e: self._canvas.yview_scroll(1, "units"), add="+")

    def _on_wheel(self, event: tk.Event) -> None:
        # فقط وقتی محتوا بلندتر از canvas است
        if self.inner.winfo_height() <= self._canvas.winfo_height():
            return
        delta = -1 if event.delta > 0 else 1
        self._canvas.yview_scroll(delta, "units")


# ─────────────────────────────────────────────────────────────────────────────
# 8) AUTH WINDOW (با اسکرول)
# ─────────────────────────────────────────────────────────────────────────────

class AuthWindow(tk.Tk):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self._db = db
        self.user: User | None = None

        self.title("🔐 Pro Calculator — Authentication")
        self.geometry("460x620")
        self.minsize(400, 480)
        self.configure(bg=Theme.BG)

        self._f_title = tkfont.Font(family="Segoe UI", size=20, weight="bold")
        self._f_label = tkfont.Font(family="Segoe UI", size=11)
        self._f_entry = tkfont.Font(family="Segoe UI", size=12)

        self._build()

    def _build(self) -> None:
        # هدر ثابت (بیرون از اسکرول)
        header = tk.Frame(self, bg=Theme.BG)
        header.pack(fill="x", pady=(20, 10))
        tk.Label(header, text="🧮 Pro Calculator", bg=Theme.BG, fg=Theme.ACCENT,
                 font=self._f_title).pack()
        tk.Label(header, text="Sign in to continue", bg=Theme.BG, fg=Theme.TEXT_DIM,
                 font=self._f_label).pack(pady=(4, 0))

        # notebook + تب‌ها
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        login_tab = ScrollableFrame(notebook, bg=Theme.BG)
        register_tab = ScrollableFrame(notebook, bg=Theme.BG)
        notebook.add(login_tab, text="  Login  ")
        notebook.add(register_tab, text="  Register  ")

        self._build_login(login_tab.inner)
        self._build_register(register_tab.inner)

        # status bar ثابت پایین
        self._status = tk.Label(self, text="", bg=Theme.BG, fg=Theme.RED,
                                font=("Segoe UI", 10))
        self._status.pack(pady=(0, 10))

    def _entry(self, parent: tk.Misc, label: str, show: str = "") -> tk.Entry:
        tk.Label(parent, text=label, bg=Theme.BG, fg=Theme.TEXT,
                 font=self._f_label).pack(anchor="w", pady=(10, 2))
        e = tk.Entry(parent, font=self._f_entry, bg=Theme.PANEL, fg=Theme.TEXT,
                     insertbackground=Theme.ACCENT, relief="flat", show=show)
        e.pack(fill="x", ipady=6)
        return e

    def _button(self, parent: tk.Misc, text: str, cmd: Callable) -> tk.Button:
        b = tk.Button(parent, text=text, command=cmd,
                      bg=Theme.ACCENT, fg=Theme.BG, font=("Segoe UI", 12, "bold"),
                      relief="flat", cursor="hand2", activebackground=Theme.ACCENT_H)
        b.pack(fill="x", pady=(18, 10), ipady=8)
        return b

    def _build_login(self, parent: tk.Frame) -> None:
        pad = tk.Frame(parent, bg=Theme.BG)
        pad.pack(fill="both", expand=True, padx=18, pady=14)

        self._login_user = self._entry(pad, "Username")
        self._login_pass = self._entry(pad, "Password", show="•")
        self._button(pad, "Login", self._do_login)
        self.bind("<Return>", lambda _e: self._do_login())

    def _build_register(self, parent: tk.Frame) -> None:
        pad = tk.Frame(parent, bg=Theme.BG)
        pad.pack(fill="both", expand=True, padx=18, pady=14)

        self._reg_first = self._entry(pad, "First name")
        self._reg_last  = self._entry(pad, "Last name")
        self._reg_phone = self._entry(pad, "Phone")
        self._reg_user  = self._entry(pad, "Username")
        self._reg_pass  = self._entry(pad, "Password", show="•")
        self._reg_pass2 = self._entry(pad, "Confirm password", show="•")
        self._button(pad, "Create account", self._do_register)

    def _set_status(self, msg: str, ok: bool = False) -> None:
        self._status.configure(text=msg, fg=Theme.GREEN if ok else Theme.RED)

    def _do_login(self) -> None:
        u = self._login_user.get().strip()
        p = self._login_pass.get()
        if not u or not p:
            self._set_status("Please fill all fields"); return
        row = self._db.verify_user(u, p)
        if not row:
            self._set_status("❌ Invalid credentials"); return
        self.user = User(
            id=row["id"], first_name=row["first_name"], last_name=row["last_name"],
            phone=row["phone"], username=row["username"],
        )
        self._set_status("✅ Welcome!", ok=True)
        self.after(200, self.destroy)

    def _do_register(self) -> None:
        first = self._reg_first.get().strip()
        last  = self._reg_last.get().strip()
        phone = self._reg_phone.get().strip()
        user  = self._reg_user.get().strip()
        pw1   = self._reg_pass.get()
        pw2   = self._reg_pass2.get()

        if not all([first, last, phone, user, pw1]):
            self._set_status("Please fill all fields"); return
        if len(user) < 3:
            self._set_status("Username must be ≥3 chars"); return
        if len(pw1) < 6:
            self._set_status("Password must be ≥6 chars"); return
        if pw1 != pw2:
            self._set_status("Passwords do not match"); return
        if not re.fullmatch(r"\+?\d{7,15}", phone):
            self._set_status("Invalid phone number"); return
        try:
            self._db.create_user(first, last, phone, user, pw1)
        except sqlite3.IntegrityError:
            self._set_status("❌ Username already taken"); return
        self._set_status("✅ Account created — now login", ok=True)
        self._login_user.delete(0, tk.END); self._login_user.insert(0, user)
        self._login_pass.delete(0, tk.END)


# ─────────────────────────────────────────────────────────────────────────────
# 9) CALCULATOR APP
# ─────────────────────────────────────────────────────────────────────────────

class CalculatorApp(tk.Tk):
    _BUTTONS = [
        ("MC", 2, 0, "mem"), ("MR", 2, 1, "mem"), ("M+", 2, 2, "mem"), ("M-", 2, 3, "mem"),
        ("C",  3, 0, "fn"),  ("⌫",  3, 1, "fn"),  ("%",  3, 2, "op"),  ("÷",  3, 3, "op"),
        ("7",  4, 0, "num"), ("8",  4, 1, "num"), ("9",  4, 2, "num"), ("×",  4, 3, "op"),
        ("4",  5, 0, "num"), ("5",  5, 1, "num"), ("6",  5, 2, "num"), ("−",  5, 3, "op"),
        ("1",  6, 0, "num"), ("2",  6, 1, "num"), ("3",  6, 2, "num"), ("+",  6, 3, "op"),
        ("±",  7, 0, "fn"),  ("0",  7, 1, "num"), (".",  7, 2, "num"), ("=",  7, 3, "eq"),
    ]
    _SCI_BUTTONS = [
        ("sin", "sin("), ("cos", "cos("), ("tan", "tan("),
        ("√",   "sqrt("),("log", "log10("),("ln", "log("),
        ("x²",  "**2"),  ("xʸ",  "^"),     ("x!",  "!"),
        ("π",   "pi"),   ("e",   "e"),     ("1/x", "1/"),
    ]

    def __init__(self, db: Database, user: User) -> None:
        super().__init__()
        self._db = db
        self._user = user
        self.title(f"🧮 Pro Calculator — {user.full_name} (@{user.username})")
        self.geometry("1000x740")
        self.minsize(840, 660)
        self.configure(bg=Theme.BG)

        self.pool = WorkerPool()
        self.engine = CalculatorEngine(self.pool, db, user)
        self.evaluator = ProcessEvaluator()

        self._expr = ""
        self._evaluating = False
        self._mem = 0.0

        self._f_display = tkfont.Font(family="Segoe UI", size=30, weight="bold")
        self._f_sub     = tkfont.Font(family="Segoe UI", size=14)
        self._f_btn     = tkfont.Font(family="Segoe UI", size=15, weight="bold")
        self._f_sci     = tkfont.Font(family="Segoe UI", size=11)
        self._f_hist    = tkfont.Font(family="Consolas", size=10)
        self._f_head    = tkfont.Font(family="Segoe UI", size=12, weight="bold")

        self._build_layout()
        self._build_userbar()
        self._build_display()
        self._build_keypad()
        self._build_scientific()
        self._build_history()
        self._bind_keys()
        self._load_history_from_db()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_layout(self) -> None:
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=2)
        self.left = tk.Frame(self, bg=Theme.BG)
        self.left.grid(row=1, column=0, sticky="nsew", padx=(14, 7), pady=(0, 14))
        self.right = tk.Frame(self, bg=Theme.BG_ALT)
        self.right.grid(row=1, column=1, sticky="nsew", padx=(7, 14), pady=(0, 14))
        self.left.grid_rowconfigure(2, weight=1)
        self.left.grid_columnconfigure(0, weight=1)

    def _build_userbar(self) -> None:
        bar = tk.Frame(self, bg=Theme.BG_ALT)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        bar.grid_columnconfigure(1, weight=1)
        tk.Label(bar, text=f"👤 {self._user.full_name}", bg=Theme.BG_ALT, fg=Theme.TEXT,
                 font=("Segoe UI", 11, "bold")).grid(row=0, column=0, padx=14, pady=10, sticky="w")
        tk.Label(bar, text=f"📱 {self._user.phone}   @{self._user.username}",
                 bg=Theme.BG_ALT, fg=Theme.TEXT_DIM, font=("Segoe UI", 10)).grid(
            row=0, column=1, padx=14, sticky="w")
        tk.Button(bar, text="Logout", command=self._logout,
                  bg=Theme.RED, fg=Theme.BG, relief="flat", bd=0,
                  font=("Segoe UI", 10, "bold"), cursor="hand2",
                  activebackground=Theme.PEACH).grid(row=0, column=2, padx=14, pady=8)

    def _build_display(self) -> None:
        frame = tk.Frame(self.left, bg=Theme.DISPLAY, padx=14, pady=12)
        frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.lbl_history = tk.Label(frame, text="", anchor="e",
                                    bg=Theme.DISPLAY, fg=Theme.TEXT_DIM, font=self._f_sub)
        self.lbl_history.pack(fill="x")
        self.lbl_main = tk.Label(frame, text="0", anchor="e", bg=Theme.DISPLAY,
                                 fg=Theme.TEXT, font=self._f_display, height=1)
        self.lbl_main.pack(fill="x", pady=(2, 0))
        self.lbl_status = tk.Label(self.left, text="Ready", anchor="w",
                                   bg=Theme.BG, fg=Theme.TEXT_DIM, font=("Segoe UI", 9))
        self.lbl_status.grid(row=1, column=0, sticky="ew", pady=(0, 6))

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

    def _build_scientific(self) -> None:
        frame = tk.Frame(self.left, bg=Theme.BG)
        frame.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        for c in range(6):
            frame.grid_columnconfigure(c, weight=1, uniform="sci")
        for i, (label, token) in enumerate(self._SCI_BUTTONS):
            r, c = divmod(i, 6)
            b = self._make_button(frame, label, "sci",
                                  command=lambda t=token, l=label: self._on_sci(t, l))
            b.grid(row=r, column=c, sticky="nsew", padx=2, pady=2)

    def _build_history(self) -> None:
        self.right.grid_rowconfigure(1, weight=1)
        self.right.grid_columnconfigure(0, weight=1)
        header = tk.Frame(self.right, bg=Theme.BG_ALT)
        header.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        header.grid_columnconfigure(0, weight=1)
        tk.Label(header, text="📜 History", bg=Theme.BG_ALT, fg=Theme.ACCENT,
                 font=self._f_head).grid(row=0, column=0, sticky="w")
        tk.Button(header, text="Clear", command=self._clear_history,
                  bg=Theme.PANEL, fg=Theme.RED, relief="flat", bd=0,
                  font=("Segoe UI", 9), cursor="hand2",
                  activebackground=Theme.PANEL_HOV, activeforeground=Theme.RED
                  ).grid(row=0, column=1, sticky="e")
        lf = tk.Frame(self.right, bg=Theme.BG_ALT)
        lf.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        lf.grid_rowconfigure(0, weight=1)
        lf.grid_columnconfigure(0, weight=1)
        self.hist_list = tk.Listbox(
            lf, bg=Theme.DISPLAY, fg=Theme.TEXT,
            selectbackground=Theme.ACCENT, selectforeground=Theme.BG,
            font=self._f_hist, relief="flat", bd=0, highlightthickness=0,
            activestyle="none",
        )
        self.hist_list.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.hist_list.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.hist_list.configure(yscrollcommand=sb.set)
        self.hist_list.bind("<Double-Button-1>", self._on_history_pick)

    def _make_button(self, parent, label, kind, command=None) -> tk.Button:
        bg, fg, hover = {
            "num": (Theme.PANEL,  Theme.TEXT,   Theme.PANEL_HOV),
            "op":  (Theme.MAUVE,  Theme.BG,     Theme.ACCENT_H),
            "eq":  (Theme.ACCENT, Theme.BG,     Theme.ACCENT_H),
            "fn":  (Theme.PEACH,  Theme.BG,     Theme.YELLOW),
            "mem": (Theme.GREEN,  Theme.BG,     Theme.YELLOW),
            "sci": (Theme.BG_ALT, Theme.ACCENT, Theme.PANEL),
        }[kind]
        f = self._f_sci if kind == "sci" else self._f_btn
        btn = tk.Button(parent, text=label, font=f, bg=bg, fg=fg,
                        relief="flat", bd=0, cursor="hand2",
                        activebackground=hover, activeforeground=fg,
                        command=command or (lambda l=label, k=kind: self._on_button(l, k)))
        btn.bind("<Enter>", lambda _e, b=btn, h=hover: b.configure(bg=h))
        btn.bind("<Leave>", lambda _e, b=btn, o=bg: b.configure(bg=o))
        return btn

    def _bind_keys(self) -> None:
        for ch in "0123456789.":
            self.bind(ch, lambda _e, c=ch: self._on_button(c, "num"))
        self.bind("+", lambda _e: self._on_button("+", "op"))
        self.bind("-", lambda _e: self._on_button("−", "op"))
        self.bind("*", lambda _e: self._on_button("×", "op"))
        self.bind("/", lambda _e: self._on_button("÷", "op"))
        self.bind("%", lambda _e: self._on_button("%", "op"))
        self.bind("<Return>", lambda _e: self._on_button("=", "eq"))
        self.bind("<KP_Enter>", lambda _e: self._on_button("=", "eq"))
        self.bind("<BackSpace>", lambda _e: self._on_button("⌫", "fn"))
        self.bind("<Escape>", lambda _e: self._on_button("C", "fn"))

    def _on_button(self, label, kind) -> None:
        if self._evaluating:
            return
        if kind == "num": self._append(label)
        elif kind == "op": self._append({"×": "*", "÷": "/", "−": "-"}.get(label, label))
        elif kind == "eq": self._evaluate()
        elif kind == "fn": self._handle_fn(label)
        elif kind == "mem": self._handle_mem(label)

    def _handle_fn(self, label: str) -> None:
        if label == "C":
            self._expr = ""
            self.lbl_history.configure(text="")
        elif label == "⌫":
            self._expr = self._expr[:-1]
        elif label == "±":
            if self._expr.startswith("-"):
                self._expr = self._expr[1:]
            elif self._expr:
                self._expr = "-" + self._expr
        self._refresh_display()

    def _handle_mem(self, label: str) -> None:
        try:
            cur = float(self._expr or "0")
        except ValueError:
            return
        if label == "MC": self._mem = 0.0
        elif label == "MR":
            self._expr = self._fmt(self._mem); self._refresh_display()
        elif label == "M+": self._mem += cur
        elif label == "M-": self._mem -= cur
        self._set_status(f"M = {self._fmt(self._mem)}")

    def _on_sci(self, token: str, label: str) -> None:
        if self._evaluating: return
        if label == "x!":
            self._evaluate_factorial(); return
        if token == "**2": self._expr += "**2"
        elif token == "^": self._expr += "^"
        elif token == "1/": self._expr = "1/(" + (self._expr or "0") + ")"
        else: self._expr += token
        self._refresh_display()

    def _append(self, text: str) -> None:
        self._expr += text
        self._refresh_display()

    def _evaluate(self) -> None:
        if not self._expr.strip(): return
        expr = self._expr
        self._set_status("⏳ Computing…")
        self._evaluating = True
        self.evaluator.evaluate(
            expr,
            lambda _e, res: self.after(0, lambda: self._apply_result(expr, res)),
        )

    def _apply_result(self, expr, result) -> None:
        self._evaluating = False
        if result is None:
            self._set_status("❌ Evaluation failed")
            log.warning("Eval failed user=%s expr=%s", self._user.username, expr)
            return
        self.lbl_history.configure(text=f"{expr} =")
        self.lbl_main.configure(text=result, fg=Theme.GREEN)
        self.engine.record(expr, result)
        log.info("calc user=%s expr=%s result=%s", self._user.username, expr, result)
        self._load_history_from_db()
        self._expr = result
        self._set_status("✅ Done")

    def _evaluate_factorial(self) -> None:
        raw = self._expr.strip()
        try:
            n = int(float(raw))
        except ValueError:
            self._set_status("❌ x! needs an integer"); return
        self._set_status("⏳ x! in thread pool…")
        fut = self.pool.submit(self.engine.factorial, n)
        fut.add_done_callback(lambda f: self.after(0, lambda: self._factorial_done(raw, f)))

    def _factorial_done(self, raw, fut: Future) -> None:
        try:
            res = str(fut.result())
            self._expr = res
            self.lbl_history.configure(text=f"{raw}! =")
            self.lbl_main.configure(text=res, fg=Theme.GREEN)
            self.engine.record(f"{raw}!", res)
            log.info("factorial user=%s n=%s", self._user.username, raw)
            self._load_history_from_db()
            self._set_status("✅ Done (thread)")
        except Exception as e:  # noqa: BLE001
            self._set_status(f"❌ {e}")
            log.exception("factorial failed")

    def _load_history_from_db(self) -> None:
        self.hist_list.delete(0, tk.END)
        for row in self.engine.history():
            ts = row["created_at"][11:19]
            self.hist_list.insert(tk.END, f"[{ts}] {row['expression']} = {row['result']}")

    def _clear_history(self) -> None:
        self.engine.clear_history()
        self._load_history_from_db()
        self._set_status("History cleared")

    def _on_history_pick(self, _evt) -> None:
        sel = self.hist_list.curselection()
        if not sel: return
        text = self.hist_list.get(sel[0])
        if " = " in text:
            self._expr = text.rsplit(" = ", 1)[1]
            self._refresh_display()

    def _refresh_display(self) -> None:
        self.lbl_main.configure(text=self._expr or "0", fg=Theme.TEXT)

    def _set_status(self, msg: str) -> None:
        self.lbl_status.configure(text=msg)

    @staticmethod
    def _fmt(x: float) -> str:
        return str(int(x)) if x == int(x) else f"{x:.10g}"

    def _logout(self) -> None:
        if messagebox.askyesno("Logout", "Log out now?"):
            self._db.audit(self._user.id, "logout", "")
            log.info("Logout: %s", self._user.username)
            self._on_close()

    def _on_close(self) -> None:
        try:
            self.pool.shutdown()
        finally:
            self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# 10) MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    mp.freeze_support()
    db = Database()
    auth = AuthWindow(db)
    auth.mainloop()
    if auth.user is None:
        log.info("Auth cancelled — exiting")
        return
    CalculatorApp(db, auth.user).mainloop()


if __name__ == "__main__":
    main()