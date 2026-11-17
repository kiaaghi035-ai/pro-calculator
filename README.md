# 🧮 Pro Calculator

ماشین حساب حرفه‌ای با Python 3.14 — OOP + Threading + Multiprocessing + Tkinter.

## ✨ ویژگی‌ها

- 🎨 رابط گرافیکی تیره و مدرن (تم Catppuccin)
- 🧵 محاسبات سنگین در `ThreadPoolExecutor` (بدون freeze)
- ⚙️ ارزیابی امن عبارات در `multiprocessing.Process` جدا
- 🔒 `eval` سندباکس‌شده (whitelist توابع)
- 📜 تاریخچه با دابل‌کلیک برای بازیابی
- 💾 حافظه (M+, M-, MR, MC)
- 🔬 پنل علمی (sin, cos, tan, √, log, x², xʸ, x!, π, e)
- ⌨️ پشتیبانی کامل کیبورد
- 🎯 معماری ماژولار (Engine / Pool / Evaluator / UI)

## 📦 پیش‌نیاز

- Python **3.14+**
- فقط کتابخانه‌ی استاندارد (بدون `pip install`)

## ▶️ اجرا

```bash
python calculator_pro.py
```

## ⌨️ کلیدهای میانبر

| کلید | عملکرد |
|---|---|
| `0-9` `.` | ورود عدد |
| `+ - * / %` | عملگرها |
| `Enter` | محاسبه (=) |
| `Backspace` | حذف آخرین کاراکتر |
| `Esc` | پاک کردن کامل |

## 🏗️ ساختار

```
CalculatorEngine   ← منطق محاسبات + تاریخچه
WorkerPool         ← ThreadPoolExecutor
ProcessEvaluator   ← ایزوله‌سازی در process جدا
CalculatorApp      ← رابط Tkinter
```

## 🐛 رفع باگ‌های نسخه‌ی ساده

- UI freeze → غیرهمزمان شد
- `eval` ناامن → سندباکس شد
- خطاهای کلی → پیام‌های دقیق
- کرش در shutdown → cleanup امن

## 📄 لایسنس

MIT