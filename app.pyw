# -*- coding: utf-8 -*-
"""Окно приложения: ссылки, очередь Zoom, расписание, время пар, настройки, журнал."""

import copy
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk, messagebox, filedialog

import engine as E


def day_sort_key(day):
    if day in E.DAYS:
        return (E.DAYS.index(day), None)
    if day == "Будни":
        return (7, None)
    if day == "Каждый день":
        return (8, None)
    try:
        return (9, datetime.strptime(day, "%d.%m.%Y"))
    except ValueError:
        return (10, None)


EDIT_CLASSES = ("Entry", "TEntry", "Text", "TCombobox")
# Физические клавиши (Windows keycode) — работают в любой раскладке, в том числе русской
CTRL_KEYS = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>", 90: "<<Undo>>"}


def select_all(w):
    if isinstance(w, tk.Text):
        w.tag_add("sel", "1.0", "end-1c")
        w.mark_set("insert", "end-1c")
    else:
        w.select_range(0, "end")
        w.icursor("end")


def install_clipboard(root):
    """Ctrl+V/C/X/A в русской раскладке и меню по правой кнопке мыши во всех полях ввода."""
    def on_ctrl(e):
        if e.keycode == 65:                       # Ctrl+A
            select_all(e.widget)
            return "break"
        ev = CTRL_KEYS.get(e.keycode)
        if ev and e.keysym.lower() not in ("v", "c", "x", "z"):   # латиница — Tk обработает сам
            e.widget.event_generate(ev)
            return "break"

    menu = tk.Menu(root, tearoff=0)

    def popup(e):
        w = e.widget
        w.focus_set()
        menu.delete(0, "end")
        menu.add_command(label="Вырезать", command=lambda: w.event_generate("<<Cut>>"))
        menu.add_command(label="Копировать", command=lambda: w.event_generate("<<Copy>>"))
        menu.add_command(label="Вставить", command=lambda: w.event_generate("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label="Выделить всё", command=lambda: select_all(w))
        menu.tk_popup(e.x_root, e.y_root)
        return "break"

    for cls in EDIT_CLASSES:
        root.bind_class(cls, "<Control-KeyPress>", on_ctrl, add="+")
        root.bind_class(cls, "<Button-3>", popup, add="+")


def make_tree(parent, columns, height=10):
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings",
                        height=height, selectmode="browse")
    for key, title, width in columns:
        tree.heading(key, text=title)
        tree.column(key, width=width, stretch=(width > 150))
    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    return frame, tree


def selected_index(tree):
    sel = tree.selection()
    return int(sel[0]) if sel else None


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = E.load_config()
        if not self.cfg.get("upper_week_monday"):   # по умолчанию текущая неделя — верхняя
            self.cfg["upper_week_monday"] = E.monday_of(datetime.now().date()).isoformat()
        E.save_config(self.cfg)
        self.msgs = queue.Queue()
        self.runner = None
        self.thread = None
        self.closing = False

        install_clipboard(root)
        root.title("Автозапись Zoom / Mirapolis + OBS")
        root.geometry("960x720")
        root.minsize(820, 600)

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.build_links(self.tab(nb, "Ссылки"))
        self.build_schedule(self.tab(nb, "Расписание"))
        self.build_pairs(self.tab(nb, "Время пар"))
        self.build_queue(self.tab(nb, "Zoom по очереди"))
        self.build_settings(self.tab(nb, "Настройки"))

        bottom = ttk.Frame(root)
        bottom.pack(fill="both", padx=8, pady=(0, 8))
        bar = ttk.Frame(bottom)
        bar.pack(fill="x")
        self.btn_sched = ttk.Button(bar, text="▶ Запустить по расписанию",
                                    command=lambda: self.start("schedule"))
        self.btn_queue = ttk.Button(bar, text="▶ Запустить Zoom по очереди",
                                    command=lambda: self.start("queue"))
        self.btn_stop = ttk.Button(bar, text="■ Стоп", command=self.stop, state="disabled")
        self.btn_sched.pack(side="left")
        self.btn_queue.pack(side="left", padx=6)
        self.btn_stop.pack(side="left")
        self.status_var = tk.StringVar(value="Не запущено")
        ttk.Label(bar, textvariable=self.status_var, font=("Segoe UI", 10, "bold")).pack(side="left", padx=12)

        logf = ttk.Frame(bottom)
        logf.pack(fill="both", expand=True, pady=(6, 0))
        self.log_text = tk.Text(logf, height=9, state="disabled", wrap="word")
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(200, self.poll)
        self.refresh_all()

    @staticmethod
    def tab(nb, title):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text=title)
        return f

    def save(self):
        E.save_config(self.cfg)

    def log(self, msg):
        self.msgs.put(("log", msg))

    # ================= Ссылки =================

    def build_links(self, p):
        ttk.Label(p, text="Короткое название и ссылка. Zoom — ссылка-приглашение или «номер код»; "
                          "Mirapolis — ссылка на комнату. Если преподаватель за пару переводит в несколько "
                          "конференций Zoom — впишите их все по порядку, по одной на строку: закончилась одна — "
                          "запись на паузе, вход в следующую, запись продолжается.", wraplength=880).pack(anchor="w")
        f, self.links_tree = make_tree(p, [("name", "Название", 140), ("site", "Сайт", 110),
                                           ("url", "Ссылка", 580)])
        f.pack(fill="both", expand=True, pady=6)
        self.links_tree.bind("<<TreeviewSelect>>", self.on_link_select)

        form = ttk.Frame(p)
        form.pack(fill="x")
        ttk.Label(form, text="Название").grid(row=0, column=0, sticky="w")
        ttk.Label(form, text="Ссылка (несколько конференций Zoom — по одной на строку)").grid(
            row=0, column=1, sticky="w", padx=(8, 0))
        self.link_name = tk.StringVar()
        ttk.Entry(form, textvariable=self.link_name, width=20).grid(row=1, column=0, sticky="nwe")
        self.link_text = tk.Text(form, width=80, height=4, wrap="none")
        self.link_text.grid(row=1, column=1, sticky="we", padx=(8, 0))
        form.columnconfigure(1, weight=1)
        btns = ttk.Frame(p)
        btns.pack(fill="x", pady=6)
        ttk.Button(btns, text="Добавить", command=self.link_add).pack(side="left")
        ttk.Button(btns, text="Сохранить изменения", command=self.link_edit).pack(side="left", padx=6)
        ttk.Button(btns, text="Удалить", command=self.link_delete).pack(side="left")

    def link_names(self):
        return [l["name"] for l in self.cfg["links"]]

    def on_link_select(self, _=None):
        i = selected_index(self.links_tree)
        if i is not None:
            self.link_name.set(self.cfg["links"][i]["name"])
            self.link_text.delete("1.0", "end")
            self.link_text.insert("1.0", self.cfg["links"][i]["url"])

    def read_link_form(self, skip=None):
        name = self.link_name.get().strip()
        urls = E.split_urls(self.link_text.get("1.0", "end"))
        if not name or not urls:
            raise ValueError("Заполните название и ссылку")
        if any(l["name"] == name for j, l in enumerate(self.cfg["links"]) if j != skip):
            raise ValueError(f"Название «{name}» уже есть")
        for u in urls:
            E.open_url(u)  # проверка ссылки Zoom
        if len(urls) > 1 and any(E.kind(u) != "zoom" for u in urls):
            raise ValueError("Несколько ссылок подряд можно указать только для Zoom")
        return name, "\n".join(urls)

    def link_add(self):
        try:
            name, url = self.read_link_form()
        except ValueError as e:
            return messagebox.showerror("Ссылка", str(e))
        self.cfg["links"].append({"name": name, "url": url})
        self.save()
        self.refresh_all()

    def link_edit(self):
        i = selected_index(self.links_tree)
        if i is None:
            return messagebox.showinfo("Ссылка", "Выберите ссылку в списке")
        try:
            name, url = self.read_link_form(skip=i)
        except ValueError as e:
            return messagebox.showerror("Ссылка", str(e))
        old = self.cfg["links"][i]["name"]
        self.cfg["links"][i] = {"name": name, "url": url}
        # переименование — обновляем расписание и очередь
        self.cfg["queue"] = [name if n == old else n for n in self.cfg["queue"]]
        for s in self.cfg["schedule"]:
            if s["link"] == old:
                s["link"] = name
        self.save()
        self.refresh_all()

    def link_delete(self):
        i = selected_index(self.links_tree)
        if i is None:
            return
        name = self.cfg["links"][i]["name"]
        used = sum(s["link"] == name for s in self.cfg["schedule"]) + self.cfg["queue"].count(name)
        msg = f"Удалить «{name}»?"
        if used:
            msg += f"\nОна используется в расписании/очереди ({used} шт.) — эти строки тоже удалятся."
        if not messagebox.askyesno("Удаление", msg):
            return
        del self.cfg["links"][i]
        self.cfg["queue"] = [n for n in self.cfg["queue"] if n != name]
        self.cfg["schedule"] = [s for s in self.cfg["schedule"] if s["link"] != name]
        self.save()
        self.refresh_all()

    # ================= Расписание =================

    def build_schedule(self, p):
        ttk.Label(p, text="В начале занятия программа сама подключается и включает запись, в конце — "
                          "останавливает запись и отключается. День: Пн…Вс, «Будни», «Каждый день» "
                          "или дата ДД.ММ.ГГГГ. Неделя: «Каждая», «Верхняя» или «Нижняя».",
                  wraplength=880).pack(anchor="w")
        wk = ttk.Frame(p)
        wk.pack(anchor="w", pady=(6, 0))
        self.week_var = tk.StringVar()
        ttk.Label(wk, textvariable=self.week_var, font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(wk, text="Неверно — поменять верхнюю/нижнюю", command=self.toggle_week).pack(side="left", padx=8)
        f, self.sched_tree = make_tree(p, [("day", "День", 110), ("week", "Неделя", 80), ("pair", "Пара", 60),
                                           ("start", "Начало", 80), ("end", "Конец", 80),
                                           ("link", "Ссылка", 300)])
        f.pack(fill="both", expand=True, pady=6)
        self.sched_tree.bind("<<TreeviewSelect>>", self.on_sched_select)
        self.next_var = tk.StringVar()
        ttk.Label(p, textvariable=self.next_var, foreground="#2a6").pack(anchor="w")

        form = ttk.Frame(p)
        form.pack(fill="x", pady=(6, 0))
        for col, t in enumerate(["День", "Неделя", "Пара", "Начало", "Конец", "Ссылка"]):
            ttk.Label(form, text=t).grid(row=0, column=col, sticky="w", padx=(0, 8))
        self.s_day = tk.StringVar(value="Пн")
        self.s_week = tk.StringVar(value="Каждая")
        self.s_pair = tk.StringVar(value="Своё время")
        self.s_start = tk.StringVar()
        self.s_end = tk.StringVar()
        self.s_link = tk.StringVar()
        ttk.Combobox(form, textvariable=self.s_day, values=E.DAY_CHOICES, width=13).grid(row=1, column=0, padx=(0, 8))
        ttk.Combobox(form, textvariable=self.s_week, values=E.WEEK_CHOICES, state="readonly",
                     width=9).grid(row=1, column=1, padx=(0, 8))
        self.pair_combo = ttk.Combobox(form, textvariable=self.s_pair, state="readonly", width=22)
        self.pair_combo.grid(row=1, column=2, padx=(0, 8))
        self.pair_combo.bind("<<ComboboxSelected>>", self.on_pair_pick)
        e_start = ttk.Entry(form, textvariable=self.s_start, width=8)
        e_start.grid(row=1, column=3, padx=(0, 8))
        e_start.bind("<KeyRelease>", self.on_start_typed)
        e_end = ttk.Entry(form, textvariable=self.s_end, width=8)
        e_end.grid(row=1, column=4, padx=(0, 8))
        e_end.bind("<KeyRelease>", lambda _: self.s_pair.set("Своё время"))
        self.link_combo = ttk.Combobox(form, textvariable=self.s_link, state="readonly", width=24)
        self.link_combo.grid(row=1, column=5, padx=(0, 8))

        btns = ttk.Frame(p)
        btns.pack(fill="x", pady=6)
        ttk.Button(btns, text="Добавить", command=self.sched_add).pack(side="left")
        ttk.Button(btns, text="Сохранить изменения", command=self.sched_edit).pack(side="left", padx=6)
        ttk.Button(btns, text="Удалить", command=self.sched_delete).pack(side="left")

    def pair_label(self, pr):
        return f"{pr['num']} пара ({pr['start']}–{pr['end']})"

    def on_pair_pick(self, _=None):
        for pr in self.cfg["pairs"]:
            if self.pair_label(pr) == self.s_pair.get():
                self.s_start.set(pr["start"])
                self.s_end.set(pr["end"])

    def on_start_typed(self, _=None):
        self.s_pair.set("Своё время")
        try:
            self.s_end.set(E.add_minutes(self.s_start.get(), int(self.cfg["lesson_minutes"])))
        except ValueError:
            pass

    def on_sched_select(self, _=None):
        i = selected_index(self.sched_tree)
        if i is None:
            return
        s = self.cfg["schedule"][i]
        self.s_day.set(s["day"])
        self.s_week.set(s.get("week") or "Каждая")
        self.s_start.set(s["start"])
        self.s_end.set(s["end"])
        self.s_link.set(s["link"])
        pr = next((p for p in self.cfg["pairs"] if p["num"] == s.get("pair")), None)
        self.s_pair.set(self.pair_label(pr) if pr else "Своё время")

    def read_sched_form(self):
        day = self.s_day.get().strip()
        if not E.valid_day(day):
            raise ValueError("День: Пн…Вс, «Будни», «Каждый день» или дата ДД.ММ.ГГГГ")
        start, end = self.s_start.get().strip(), self.s_end.get().strip()
        E.parse_hm(start)
        E.parse_hm(end)
        if not self.s_link.get():
            raise ValueError("Выберите ссылку")
        pr = next((p for p in self.cfg["pairs"] if self.pair_label(p) == self.s_pair.get()), None)
        return {"day": day, "week": self.s_week.get() or "Каждая", "start": E.add_minutes(start, 0), "end": E.add_minutes(end, 0),
                "link": self.s_link.get(), "pair": pr["num"] if pr else None}

    def sched_add(self):
        try:
            entry = self.read_sched_form()
        except ValueError as e:
            return messagebox.showerror("Расписание", str(e))
        self.cfg["schedule"].append(entry)
        self.sort_schedule()
        self.save()
        self.refresh_all()

    def sched_edit(self):
        i = selected_index(self.sched_tree)
        if i is None:
            return messagebox.showinfo("Расписание", "Выберите занятие в списке")
        try:
            self.cfg["schedule"][i] = self.read_sched_form()
        except ValueError as e:
            return messagebox.showerror("Расписание", str(e))
        self.sort_schedule()
        self.save()
        self.refresh_all()

    def sched_delete(self):
        i = selected_index(self.sched_tree)
        if i is not None:
            del self.cfg["schedule"][i]
            self.save()
            self.refresh_all()

    def sort_schedule(self):
        self.cfg["schedule"].sort(key=lambda s: (E.WEEK_CHOICES.index(s.get("week") or "Каждая"),
                                                 day_sort_key(s["day"]), s["start"]))

    def toggle_week(self):
        ref = E.upper_ref(self.cfg)
        self.cfg["upper_week_monday"] = (ref + E.timedelta(days=7)).isoformat()
        self.save()
        self.refresh_all()

    # ================= Время пар =================

    def build_pairs(self, p):
        ttk.Label(p, text="Время пар. В расписании можно выбрать пару — время подставится само. "
                          "Если поменять время пары здесь, оно поменяется и во всех занятиях с этой парой.",
                  wraplength=880).pack(anchor="w")
        f, self.pairs_tree = make_tree(p, [("num", "№", 50), ("start", "Начало", 100), ("end", "Конец", 100)])
        f.pack(fill="both", expand=True, pady=6)
        self.pairs_tree.bind("<<TreeviewSelect>>", self.on_pairrow_select)

        form = ttk.Frame(p)
        form.pack(fill="x")
        for col, t in enumerate(["№", "Начало", "Конец", "Длительность пары, мин"]):
            ttk.Label(form, text=t).grid(row=0, column=col, sticky="w", padx=(0, 8))
        self.p_num, self.p_start, self.p_end = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.p_len = tk.StringVar(value=str(self.cfg["lesson_minutes"]))
        ttk.Entry(form, textvariable=self.p_num, width=5).grid(row=1, column=0, padx=(0, 8))
        e = ttk.Entry(form, textvariable=self.p_start, width=8)
        e.grid(row=1, column=1, padx=(0, 8))
        e.bind("<KeyRelease>", self.on_pstart_typed)
        ttk.Entry(form, textvariable=self.p_end, width=8).grid(row=1, column=2, padx=(0, 8))
        el = ttk.Entry(form, textvariable=self.p_len, width=6)
        el.grid(row=1, column=3, sticky="w")
        el.bind("<FocusOut>", self.save_len)
        el.bind("<Return>", self.save_len)

        btns = ttk.Frame(p)
        btns.pack(fill="x", pady=6)
        ttk.Button(btns, text="Добавить", command=self.pair_add).pack(side="left")
        ttk.Button(btns, text="Сохранить изменения", command=self.pair_edit).pack(side="left", padx=6)
        ttk.Button(btns, text="Удалить", command=self.pair_delete).pack(side="left")

    def save_len(self, _=None):
        try:
            v = int(self.p_len.get())
            if v <= 0:
                raise ValueError
            self.cfg["lesson_minutes"] = v
            self.save()
        except ValueError:
            self.p_len.set(str(self.cfg["lesson_minutes"]))

    def on_pstart_typed(self, _=None):
        try:
            self.p_end.set(E.add_minutes(self.p_start.get(), int(self.cfg["lesson_minutes"])))
        except ValueError:
            pass

    def on_pairrow_select(self, _=None):
        i = selected_index(self.pairs_tree)
        if i is not None:
            pr = self.cfg["pairs"][i]
            self.p_num.set(str(pr["num"]))
            self.p_start.set(pr["start"])
            self.p_end.set(pr["end"])

    def read_pair_form(self, skip=None):
        try:
            num = int(self.p_num.get())
        except ValueError:
            raise ValueError("№ пары — число")
        if any(p["num"] == num for j, p in enumerate(self.cfg["pairs"]) if j != skip):
            raise ValueError(f"Пара №{num} уже есть")
        return {"num": num, "start": E.add_minutes(self.p_start.get(), 0),
                "end": E.add_minutes(self.p_end.get(), 0)}

    def pair_add(self):
        try:
            self.cfg["pairs"].append(self.read_pair_form())
        except ValueError as e:
            return messagebox.showerror("Время пар", str(e))
        self.cfg["pairs"].sort(key=lambda p: p["num"])
        self.save()
        self.refresh_all()

    def pair_edit(self):
        i = selected_index(self.pairs_tree)
        if i is None:
            return messagebox.showinfo("Время пар", "Выберите пару в списке")
        try:
            new = self.read_pair_form(skip=i)
        except ValueError as e:
            return messagebox.showerror("Время пар", str(e))
        old_num = self.cfg["pairs"][i]["num"]
        self.cfg["pairs"][i] = new
        for s in self.cfg["schedule"]:
            if s.get("pair") == old_num:
                s.update(pair=new["num"], start=new["start"], end=new["end"])
        self.cfg["pairs"].sort(key=lambda p: p["num"])
        self.sort_schedule()
        self.save()
        self.refresh_all()

    def pair_delete(self):
        i = selected_index(self.pairs_tree)
        if i is None:
            return
        num = self.cfg["pairs"][i]["num"]
        del self.cfg["pairs"][i]
        for s in self.cfg["schedule"]:
            if s.get("pair") == num:
                s["pair"] = None   # время занятия остаётся, просто «своё»
        self.save()
        self.refresh_all()

    # ================= Zoom по очереди =================

    def build_queue(self, p):
        ttk.Label(p, text="Конференции Zoom подключаются по очереди: закончилась одна — запись на паузе, "
                          "подключение к следующей, запись продолжается. После последней запись "
                          "останавливается. Первые N минут (Настройки) пропажа конференции не считается концом.",
                  wraplength=880).pack(anchor="w")
        f = ttk.Frame(p)
        f.pack(fill="both", expand=True, pady=6)
        self.queue_list = tk.Listbox(f, height=10)
        self.queue_list.pack(side="left", fill="both", expand=True)
        side = ttk.Frame(f)
        side.pack(side="left", fill="y", padx=8)
        self.q_link = tk.StringVar()
        self.q_combo = ttk.Combobox(side, textvariable=self.q_link, state="readonly", width=24)
        self.q_combo.pack(fill="x")
        ttk.Button(side, text="Добавить в очередь", command=self.q_add).pack(fill="x", pady=(4, 12))
        ttk.Button(side, text="↑ Выше", command=lambda: self.q_move(-1)).pack(fill="x")
        ttk.Button(side, text="↓ Ниже", command=lambda: self.q_move(1)).pack(fill="x", pady=4)
        ttk.Button(side, text="Убрать", command=self.q_remove).pack(fill="x")

    def q_add(self):
        if self.q_link.get():
            self.cfg["queue"].append(self.q_link.get())
            self.save()
            self.refresh_all()

    def q_sel(self):
        s = self.queue_list.curselection()
        return s[0] if s else None

    def q_move(self, d):
        i = self.q_sel()
        q = self.cfg["queue"]
        if i is None or not 0 <= i + d < len(q):
            return
        q[i], q[i + d] = q[i + d], q[i]
        self.save()
        self.refresh_all()
        self.queue_list.selection_set(i + d)

    def q_remove(self):
        i = self.q_sel()
        if i is not None:
            del self.cfg["queue"][i]
            self.save()
            self.refresh_all()

    # ================= Настройки =================

    def build_settings(self, p):
        canvas = tk.Canvas(p, highlightthickness=0)
        sb = ttk.Scrollbar(p, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        c = self.cfg
        self.v = {}
        row = [0]

        def section(title):
            ttk.Label(inner, text=title, font=("Segoe UI", 10, "bold")).grid(
                row=row[0], column=0, columnspan=4, sticky="w", pady=(10, 2))
            row[0] += 1

        def field(label, key, width=30, value=None):
            var = tk.StringVar(value=str(c[key] if value is None else value))
            ttk.Label(inner, text=label).grid(row=row[0], column=0, sticky="w", padx=(0, 8))
            e = ttk.Entry(inner, textvariable=var, width=width)
            e.grid(row=row[0], column=1, sticky="w", pady=1)
            self.v[key] = var
            row[0] += 1
            return e

        def check(label, key):
            var = tk.BooleanVar(value=c[key])
            ttk.Checkbutton(inner, text=label, variable=var).grid(row=row[0], column=0, columnspan=3, sticky="w")
            self.v[key] = var
            row[0] += 1

        section("Имя")
        field("Имя в Zoom", "zoom_name")
        field("Фамилия (Mirapolis)", "last_name")
        field("Имя (Mirapolis)", "first_name")
        field("Отчество (Mirapolis)", "middle_name")
        check("Держать микрофон выключенным", "mic_off")
        check("Держать камеру выключенной", "camera_off")

        section("Горячие клавиши OBS (как в OBS → Настройки → Горячие клавиши)")
        for key, label in (("start", "Начать запись"), ("stop", "Остановить запись"),
                           ("pause", "Пауза"), ("unpause", "Снять с паузы")):
            field(label, "hk_" + key, 20, c["hotkeys"][key])
        ttk.Label(inner, text="Примеры: pause, shift+pause, ctrl+shift+f9, alt+r, f12. Не используйте f11.",
                  foreground="#666").grid(row=row[0], column=0, columnspan=3, sticky="w")
        row[0] += 1
        ttk.Button(inner, text="Проверить клавиши (старт → пауза → снять паузу → стоп)",
                   command=self.test_keys).grid(row=row[0], column=0, columnspan=3, sticky="w", pady=4)
        row[0] += 1

        section("Файл записи OBS")
        check("Переименовывать запись после остановки", "obs_rename")
        fe = field("Папка записей OBS", "obs_folder", 50)
        ttk.Button(inner, text="Обзор…", command=lambda: self.browse_dir("obs_folder")).grid(
            row=row[0] - 1, column=2, sticky="w", padx=4)
        self.tpl_entry = field("Название файла", "obs_template", 50)
        tb = ttk.Frame(inner)
        tb.grid(row=row[0], column=1, columnspan=3, sticky="w")
        for label, ph in (("+ Название", "{name}"), ("+ Дата", "{date}"),
                          ("+ Время", "{time}"), ("+ День недели", "{day}")):
            ttk.Button(tb, text=label, command=lambda ph=ph: self.insert_ph(ph)).pack(side="left", padx=(0, 4))
        ttk.Button(tb, text="Сбросить", command=lambda: self.v["obs_template"].set("{name} {date} {time}")).pack(side="left")
        row[0] += 1
        self.preview_var = tk.StringVar()
        ttk.Label(inner, textvariable=self.preview_var, foreground="#2a6").grid(
            row=row[0], column=1, columnspan=3, sticky="w")
        row[0] += 1
        self.v["obs_template"].trace_add("write", lambda *_: self.update_preview())
        self.update_preview()

        section("Тайминги")
        field("Подключаться до начала пары, мин", "connect_before_min", 6)
        field("Zoom: пара затянулась — ждать конца до, мин", "zoom_overtime_min", 6)
        field("Zoom: первые N минут не считать конец", "grace_minutes", 6)
        field("Zoom: конференции нет N секунд — конец", "end_confirm_sec", 6)
        field("Zoom по очереди: ждать подключения, мин", "join_timeout_min", 6)

        section("Chrome")
        field("Путь к chrome.exe (пусто — найти сам)", "chrome_path", 50)
        ttk.Button(inner, text="Обзор…", command=self.browse_chrome).grid(row=row[0] - 1, column=2, sticky="w", padx=4)

        ttk.Button(inner, text="Сохранить настройки", command=self.save_settings).grid(
            row=row[0], column=0, sticky="w", pady=12)

    def insert_ph(self, ph):
        self.tpl_entry.insert(tk.INSERT, ph)
        self.tpl_entry.focus_set()

    def update_preview(self):
        now = datetime.now()
        name = E.format_name(self.v["obs_template"].get(), "Мира 1", now)
        self.preview_var.set(f"Пример: {name}.mkv")

    def browse_dir(self, key):
        d = filedialog.askdirectory(initialdir=self.v[key].get() or None)
        if d:
            self.v[key].set(d.replace("/", "\\"))

    def browse_chrome(self):
        f = filedialog.askopenfilename(filetypes=[("chrome.exe", "chrome.exe")])
        if f:
            self.v["chrome_path"].set(f.replace("/", "\\"))

    def save_settings(self, silent=False):
        c = self.cfg
        try:
            for key in ("zoom_name", "last_name", "first_name", "middle_name",
                        "obs_folder", "obs_template", "chrome_path"):
                c[key] = self.v[key].get().strip()
            for key in ("mic_off", "camera_off", "obs_rename"):
                c[key] = bool(self.v[key].get())
            for key in ("connect_before_min", "zoom_overtime_min", "grace_minutes", "end_confirm_sec", "join_timeout_min"):
                try:
                    c[key] = float(self.v[key].get().replace(",", "."))
                except ValueError:
                    raise ValueError(f"Нужно число: {self.v[key].get()!r}")
            for key in ("start", "stop", "pause", "unpause"):
                combo = self.v["hk_" + key].get().strip().lower()
                E.parse_hotkey(combo)
                c["hotkeys"][key] = combo
        except ValueError as e:
            messagebox.showerror("Настройки", str(e))
            return False
        self.save()
        if not silent:
            self.log("Настройки сохранены")
        return True

    def test_keys(self):
        if not self.save_settings(silent=True):
            return
        hk = dict(self.cfg["hotkeys"])

        def run():
            self.log("Проверка клавиш через 5 секунд — переключитесь на OBS")
            time.sleep(5)
            for key, name in (("start", "старт"), ("pause", "пауза"), ("unpause", "снять паузу"), ("stop", "стоп")):
                E.press_hotkey(hk[key])
                self.log(f"Нажато: {name} ({hk[key]})")
                time.sleep(5)
            self.log("Проверка клавиш закончена")
        threading.Thread(target=run, daemon=True).start()

    # ================= Обновление списков =================

    def refresh_all(self):
        t = self.links_tree
        t.delete(*t.get_children())
        names = {"zoom": "Zoom", "mira": "Mirapolis", "other": "Другой"}
        for i, l in enumerate(self.cfg["links"]):
            urls = E.split_urls(l["url"]) or [""]
            try:
                site = names[E.kind(urls[0])]
            except ValueError:
                site = "?"
            if len(urls) > 1:
                site += f" ×{len(urls)}"
            t.insert("", "end", iid=str(i), values=(l["name"], site, "  →  ".join(urls)))

        t = self.sched_tree
        t.delete(*t.get_children())
        for i, s in enumerate(self.cfg["schedule"]):
            t.insert("", "end", iid=str(i), values=(s["day"], s.get("week") or "Каждая", s.get("pair") or "—",
                                                    s["start"], s["end"], s["link"]))

        today = datetime.now().date()
        cur = E.week_type(today, E.upper_ref(self.cfg))
        self.week_var.set(f"Сейчас {cur.lower()} неделя "
                          f"(с {E.monday_of(today):%d.%m}), следующая — "
                          f"{'нижняя' if cur == 'Верхняя' else 'верхняя'}")

        t = self.pairs_tree
        t.delete(*t.get_children())
        for i, pr in enumerate(self.cfg["pairs"]):
            t.insert("", "end", iid=str(i), values=(pr["num"], pr["start"], pr["end"]))

        self.queue_list.delete(0, "end")
        for i, n in enumerate(self.cfg["queue"], 1):
            self.queue_list.insert("end", f"{i}. {n}")

        links = self.link_names()
        self.link_combo["values"] = links
        self.q_combo["values"] = links
        self.pair_combo["values"] = [self.pair_label(p) for p in self.cfg["pairs"]] + ["Своё время"]

        try:
            nxt = E.occurrences(self.cfg["schedule"], datetime.now(), E.upper_ref(self.cfg))
        except ValueError:
            nxt = []
        if nxt:
            s, e, entry = nxt[0]
            self.next_var.set(f"Ближайшее: {entry['link']} — {E.DAYS[s.weekday()]} {s:%d.%m} "
                              f"{entry['start']}–{entry['end']}")
        else:
            self.next_var.set("Ближайших занятий нет")

    # ================= Запуск =================

    def start(self, mode):
        if self.thread and self.thread.is_alive():
            return
        if not self.save_settings(silent=True):
            return
        if mode == "schedule" and not self.cfg["schedule"]:
            return messagebox.showinfo("Расписание", "Добавьте занятия во вкладке «Расписание»")
        if mode == "queue" and not self.cfg["queue"]:
            return messagebox.showinfo("Очередь", "Добавьте конференции во вкладке «Zoom по очереди»")
        if self.cfg["obs_rename"] and not self.cfg["obs_folder"]:
            return messagebox.showinfo("OBS", "Укажите папку записей OBS в настройках")
        self.runner = E.Runner(copy.deepcopy(self.cfg), self.log,
                               lambda s: self.msgs.put(("status", s)))
        self.thread = threading.Thread(target=self.runner.run, args=(mode,), daemon=True)
        self.thread.start()
        self.btn_sched["state"] = self.btn_queue["state"] = "disabled"
        self.btn_stop["state"] = "normal"

    def stop(self):
        if self.runner:
            self.log("Останавливаю…")
            self.runner.stop()

    def on_close(self):
        if self.thread and self.thread.is_alive():
            if not messagebox.askyesno("Выход", "Идёт работа. Остановить запись и выйти?"):
                return
            self.closing = True
            self.stop()
        else:
            self.root.destroy()

    def poll(self):
        while True:
            try:
                kind, msg = self.msgs.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.log_text["state"] = "normal"
                self.log_text.insert("end", time.strftime("[%H:%M:%S] ") + msg + "\n")
                self.log_text.see("end")
                self.log_text["state"] = "disabled"
            else:
                self.status_var.set(msg)
        if self.thread and not self.thread.is_alive():
            self.thread = None
            self.btn_sched["state"] = self.btn_queue["state"] = "normal"
            self.btn_stop["state"] = "disabled"
            if self.closing:
                self.root.after(1500, self.root.destroy)
        self.root.after(200, self.poll)


if __name__ == "__main__":
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
