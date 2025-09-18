# -*- coding: utf-8 -*-
"""
批量视频压缩/抽音（Tk 弹窗进度 + 可取消 + speed 显示 + 可自选输出目录）
- 流程：选择模式 -> 选择多个视频 -> 选择输出目录 -> 弹出进度窗
- 进度：ffmpeg -progress pipe:1（解析 out_time_ms、speed）
- 取消：terminate() 等 1.5s，未退则 kill()；UI 提示“已取消”并退出主循环
- 完成：弹出“处理完成”提示；进度窗不自动关闭（按钮变“关闭”）
"""

import os
import sys
import re
import time
import threading
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple
from queue import Queue, Empty

# 若未加入 PATH，可写绝对路径
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
# FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe"
# FFPROBE = r"C:\ffmpeg\bin\ffprobe.exe"

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".wmv", ".m4v", ".ts", ".webm"}

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

# =============== 选择模式 ===============
class ModeSelector(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("选择处理模式")
        self.geometry("450x320")
        self.resizable(False, False)
        self.choice = tk.StringVar(value="quality")

        ttk.Label(self, text="请选择处理策略：", font=("Microsoft YaHei", 11)).pack(pady=10)
        frm = ttk.Frame(self)
        frm.pack(pady=2, fill="x")

        ttk.Radiobutton(frm, text="保全画质（CRF≈18，preset=slow，音频copy）",
                        variable=self.choice, value="quality").pack(anchor="w", padx=20, pady=4)
        ttk.Radiobutton(frm, text="优先压缩大小（CRF≈28，preset=veryslow，AAC128k，1080p上限）",
                        variable=self.choice, value="size").pack(anchor="w", padx=20, pady=4)
        ttk.Radiobutton(frm, text="只提取音频（.m4a，AAC 128k）",
                        variable=self.choice, value="audio").pack(anchor="w", padx=20, pady=4)
        ttk.Radiobutton(frm, text="视频合并（按自定义顺序拼接多个视频）",
                        variable=self.choice, value="merge").pack(anchor="w", padx=20, pady=4)
        ttk.Radiobutton(frm, text="视频拆分（按时间戳将单个视频拆成多段）",
                        variable=self.choice, value="split").pack(anchor="w", padx=20, pady=4)

        ttk.Button(self, text="下一步：选择文件", command=self.destroy).pack(pady=12)

def choose_mode() -> str:
    app = ModeSelector()
    app.mainloop()
    return app.choice.get()

def choose_files(multiple: bool = True,
                 title: str = "选择要处理的视频文件（可多选）") -> List[Path]:
    root = tk.Tk()
    root.withdraw()
    if multiple:
        paths = filedialog.askopenfilenames(
            title=title,
            filetypes=[("Video files", "*.mp4 *.mov *.mkv *.avi *.flv *.wmv *.m4v *.ts *.webm"),
                       ("All files", "*.*")]
        )
    else:
        single = filedialog.askopenfilename(
            title=title,
            filetypes=[("Video files", "*.mp4 *.mov *.mkv *.avi *.flv *.wmv *.m4v *.ts *.webm"),
                       ("All files", "*.*")]
        )
        paths = (single,) if single else ()
    root.update()
    root.destroy()
    files = [Path(p) for p in paths if Path(p).suffix.lower() in VIDEO_EXTS]
    return files

def choose_output_dir(default: Optional[Path] = None) -> Optional[Path]:
    """选择输出目录；取消则返回 None"""
    root = tk.Tk()
    root.withdraw()
    initial = str(default or Path(".").resolve())
    path = filedialog.askdirectory(title="选择处理后文件的保存位置", initialdir=initial, mustexist=True)
    root.update()
    if not path:
        return None
    return Path(path).resolve()

# =============== 基础工具 ===============
def which(cmd: str) -> Optional[str]:
    from shutil import which as _which
    return _which(cmd)

def ensure_ff_tools():
    ff = which(FFMPEG) or (FFMPEG if Path(FFMPEG).exists() else None)
    fp = which(FFPROBE) or (FFPROBE if Path(FFPROBE).exists() else None)
    if not ff or not fp:
        messagebox.showerror("错误", "未检测到 ffmpeg/ffprobe。\n请确保已安装并加入 PATH，或在脚本顶部设置绝对路径。")
        sys.exit(2)

def ffprobe_duration(path: Path) -> Optional[float]:
    try:
        out = subprocess.check_output(
            [FFPROBE, "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(path)],
            stderr=subprocess.STDOUT
        )
        dur = float(out.decode("utf-8", "replace").strip())
        return dur if dur > 0 else None
    except Exception:
        return None

def ffprobe_resolution(path: Path) -> Tuple[Optional[int], Optional[int]]:
    try:
        out = subprocess.check_output(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=s=x:p=0", str(path)],
            stderr=subprocess.STDOUT
        ).decode("utf-8", "replace").strip()
        if "x" in out:
            w, h = out.split("x")
            return int(w), int(h)
    except Exception:
        pass
    return None, None

def format_hms(sec: float) -> str:
    sec = max(0, int(sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

def ensure_unique_path(p: Path) -> Path:
    """若目标文件已存在，自动追加 _1/_2/..."""
    if not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    i = 1
    while True:
        cand = p.with_name(f"{stem}_{i}{suffix}")
        if not cand.exists():
            return cand
        i += 1


def parse_timestamp(value: str) -> Optional[float]:
    """解析字符串时间戳，支持 123 / mm:ss / hh:mm:ss[.ms]"""
    text = value.strip()
    if not text:
        return None
    parts = text.split(":")
    try:
        if len(parts) == 1:
            sec = float(parts[0])
            if sec < 0:
                return None
            return sec
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = float(parts[1])
            if minutes < 0 or seconds < 0 or seconds >= 60:
                return None
            return minutes * 60 + seconds
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
            if hours < 0 or minutes < 0 or minutes >= 60 or seconds < 0 or seconds >= 60:
                return None
            return hours * 3600 + minutes * 60 + seconds
    except ValueError:
        return None
    return None


def format_timestamp_precise(sec: float) -> str:
    """格式化浮点秒为 ffmpeg 需要的 hh:mm:ss.xxx"""
    total = max(0.0, float(sec))
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    seconds = round(total - hours * 3600 - minutes * 60, 3)
    if seconds >= 60:
        seconds -= 60
        minutes += 1
    if minutes >= 60:
        minutes -= 60
        hours += 1
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"


def escape_concat_path(path: Path) -> str:
    text = str(path)
    text = text.replace("\\", "\\\\")
    return text.replace("'", "'\\''")


class ReorderDialog(tk.Toplevel):
    def __init__(self, master, files: List[Path]):
        super().__init__(master)
        self.title("调整拼接顺序")
        self.resizable(False, False)
        self.result: Optional[List[Path]] = None
        self.file_paths = list(files)

        self.transient(master)

        self.protocol("WM_DELETE_WINDOW", self.on_cancel)

        ttk.Label(self, text="选中条目后使用按钮调整拼接顺序。",
                  font=("Microsoft YaHei", 10)).pack(padx=16, pady=(16, 8))

        self.listbox = tk.Listbox(self, selectmode=tk.SINGLE, width=56,
                                   height=min(12, max(4, len(files))))
        self.listbox.pack(padx=16, pady=(0, 8))

        btn_frame = ttk.Frame(self)
        btn_frame.pack(padx=16, pady=(0, 12))
        ttk.Button(btn_frame, text="上移", command=self.move_up, width=10).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="下移", command=self.move_down, width=10).pack(side="left", padx=4)

        action_frame = ttk.Frame(self)
        action_frame.pack(padx=16, pady=(0, 16), fill="x")
        ttk.Button(action_frame, text="取消", command=self.on_cancel, width=10).pack(side="right", padx=4)
        ttk.Button(action_frame, text="确定", command=self.on_ok, width=10).pack(side="right", padx=4)

        self.bind("<Escape>", lambda _: self.on_cancel())
        self.bind("<Return>", lambda _: self.on_ok())

        self.refresh_list(0)

        self.after(10, self._make_modal)

    def _make_modal(self):
        """确保窗口可见后再获取焦点和输入捕获，避免部分平台上抓取失败"""
        try:
            if not self.winfo_viewable():
                self.after(20, self._make_modal)
                return
            self.grab_set()
        except tk.TclError:
            self.after(20, self._make_modal)
            return
        self.lift()
        self.focus_force()


    def refresh_list(self, selection: int):
        self.listbox.delete(0, tk.END)
        for idx, path in enumerate(self.file_paths, 1):
            self.listbox.insert(tk.END, f"{idx}. {path.name}")
        if self.file_paths and 0 <= selection < len(self.file_paths):
            self.listbox.selection_clear(0, tk.END)
            self.listbox.selection_set(selection)
            self.listbox.activate(selection)
            self.listbox.see(selection)

    def move_up(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx <= 0:
            return
        self.file_paths[idx - 1], self.file_paths[idx] = self.file_paths[idx], self.file_paths[idx - 1]
        self.refresh_list(idx - 1)

    def move_down(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx >= len(self.file_paths) - 1:
            return
        self.file_paths[idx], self.file_paths[idx + 1] = self.file_paths[idx + 1], self.file_paths[idx]
        self.refresh_list(idx + 1)

    def on_ok(self):
        self.result = list(self.file_paths)
        self.destroy()

    def on_cancel(self):
        self.result = None
        self.destroy()


def reorder_files(files: List[Path]) -> Optional[List[Path]]:
    if len(files) <= 1:
        return files
    root = tk.Tk()
    root.withdraw()
    dlg = ReorderDialog(root, files)
    root.wait_window(dlg)
    result = getattr(dlg, "result", None)
    root.destroy()
    return result


def ask_split_points(duration: Optional[float]) -> Optional[List[float]]:
    root = tk.Tk()
    root.withdraw()

    prompt = [
        "请输入拆分时间点（多个时间可用逗号、空格或换行分隔）",
        "支持秒数或 HH:MM:SS[.毫秒] 格式。"
    ]
    if duration:
        prompt.append(f"视频总时长约为 {format_hms(duration)}。")
    prompt_text = "\n".join(prompt)

    while True:
        value = simpledialog.askstring("视频拆分", prompt_text, parent=root)
        if value is None:
            root.destroy()
            return None
        tokens = re.split(r"[,\s]+", value.strip())
        raw_points: List[float] = []
        error_msg = None
        for token in tokens:
            token = token.strip()
            if not token:
                continue
            sec = parse_timestamp(token)
            if sec is None:
                error_msg = f"无法解析时间戳：{token}"
                break
            raw_points.append(sec)
        if error_msg:
            messagebox.showerror("错误", error_msg)
            continue
        if not raw_points:
            messagebox.showerror("错误", "请至少输入一个有效的时间戳。")
            continue
        raw_points.sort()
        cleaned: List[float] = []
        for sec in raw_points:
            if sec <= 0:
                error_msg = "时间戳需大于 0 秒。"
                break
            if cleaned and sec <= cleaned[-1]:
                error_msg = "时间戳需严格递增且不可重复。"
                break
            if duration is not None and sec >= duration:
                error_msg = "时间戳需小于视频总时长。"
                break
            cleaned.append(sec)
        if error_msg:
            messagebox.showerror("错误", error_msg)
            continue
        root.destroy()
        return cleaned

# =============== 策略参数 ===============
@dataclass
class EncodePlan:
    args: List[str]
    out_suffix: str
    replace_ext: bool  # 只提取音频时替换为 .m4a

def build_plan(mode: str) -> EncodePlan:
    if mode == "quality":
        return EncodePlan(
            args=["-c:v", "libx264", "-crf", "18", "-preset", "slow",
                  "-c:a", "copy", "-movflags", "+faststart"],
            out_suffix="_hq.mp4",
            replace_ext=False
        )
    elif mode == "size":
        return EncodePlan(
            args=["-c:v", "libx264", "-crf", "28", "-preset", "veryslow",
                  "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"],
            out_suffix="_small.mp4",
            replace_ext=False
        )
    elif mode == "audio":
        return EncodePlan(
            args=["-vn", "-acodec", "aac", "-b:a", "128k"],
            out_suffix=".m4a",
            replace_ext=True
        )
    else:
        raise ValueError("未知模式")

def maybe_add_scale(args: List[str], in_path: Path) -> List[str]:
    # 仅视频模式，若高度 > 1080 则缩到 1080
    w, h = ffprobe_resolution(in_path)
    if h and h > 1080:
        if "-vf" in args:
            i = args.index("-vf") + 1
            args[i] = args[i] + ",scale=-2:1080"
        else:
            args += ["-vf", "scale=-2:1080"]
    return args

# =============== 进度窗口 ===============
class ProgressDialog(tk.Toplevel):
    def __init__(self, master, total_files: int):
        super().__init__(master)
        self.title("处理中…")
        self.geometry("640x260")
        self.minsize(640, 260)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.on_cancel)

        padx, pbar_len = 18, 600
        self.cancelled = False
        self._file_bar_indeterminate = False
        self._done_mode = False  # 完成态：按钮变“关闭”
        self.master_ref = master  # 保存父窗口引用（root）

        # 当前文件
        self.lbl_file = ttk.Label(self, text="文件：—", font=("Microsoft YaHei", 10))
        self.lbl_file.pack(anchor="w", padx=padx, pady=(12, 4))

        self.lbl_time = ttk.Label(self, text="时间：00:00 / 00:00", font=("Consolas", 11))
        self.lbl_time.pack(anchor="w", padx=padx, pady=(0, 2))

        self.lbl_speed = ttk.Label(self, text="速度：--", font=("Consolas", 10))  # 显示 speed
        self.lbl_speed.pack(anchor="w", padx=padx, pady=(0, 4))

        self.pb_file = ttk.Progressbar(self, orient="horizontal", length=pbar_len, mode="determinate")
        self.pb_file.pack(padx=padx, pady=(6, 6))

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=padx, pady=8)

        # 总体
        self.lbl_overall = ttk.Label(self, text=f"总进度：0/{total_files}", font=("Microsoft YaHei", 10))
        self.lbl_overall.pack(anchor="w", padx=padx)

        self.pb_overall = ttk.Progressbar(self, orient="horizontal", length=pbar_len,
                                          mode="determinate", maximum=total_files, value=0)
        self.pb_overall.pack(padx=padx, pady=(6, 8))

        # 状态 + 按钮
        frm = ttk.Frame(self)
        frm.pack(fill="x", padx=padx, pady=(2, 12))
        self.lbl_status = ttk.Label(frm, text="状态：准备中…", anchor="w")
        self.lbl_status.pack(side="left", expand=True, fill="x")
        self.btn_cancel = ttk.Button(frm, text="取消", command=self.on_cancel, width=10)
        self.btn_cancel.pack(side="right")

    def mark_done(self):
        self._done_mode = True
        self.btn_cancel.configure(text="关闭", state="normal", command=self.close_and_quit)

    def close_and_quit(self):
        # 销窗 + 退出主循环
        try:
            self.destroy()
        finally:
            try:
                if self.master_ref:
                    self.master_ref.quit()
            except Exception:
                pass

    def on_cancel(self):
        if self._done_mode:
            self.close_and_quit()
            return
        self.cancelled = True
        try:
            self.btn_cancel.configure(state="disabled")
        except Exception:
            pass
        self.set_status("正在取消…")

    def set_file(self, name: str):
        self.lbl_file.configure(text=f"文件：{name}")
        self.lbl_time.configure(text="时间：00:00 / 00:00")
        self.lbl_speed.configure(text="速度：--")
        self._file_bar_indeterminate = False
        try:
            self.pb_file.stop()
        except Exception:
            pass
        self.pb_file.configure(mode="determinate", maximum=100.0, value=0)

    def set_file_progress(self, processed: float, total: Optional[float], speed: Optional[str] = None):
        if total and total > 0:
            if self._file_bar_indeterminate:
                try: self.pb_file.stop()
                except Exception: pass
                self._file_bar_indeterminate = False
                self.pb_file.configure(mode="determinate", maximum=100.0)
            pct = max(0.0, min(processed / total * 100.0, 100.0))
            self.pb_file.configure(value=pct)
            self.lbl_time.configure(text=f"时间：{format_hms(processed)} / {format_hms(total)}")
        else:
            if not self._file_bar_indeterminate:
                self.pb_file.configure(mode="indeterminate")
                self.pb_file.start(10)
                self._file_bar_indeterminate = True
            self.lbl_time.configure(text=f"时间：{format_hms(processed)} / --:--")

        if speed:
            self.lbl_speed.configure(text=f"速度：{speed}")

    def set_overall(self, done: int, total: int):
        self.lbl_overall.configure(text=f"总进度：{done}/{total}")
        self.pb_overall.configure(value=done, maximum=total)

    def set_status(self, text: str):
        self.lbl_status.configure(text=f"状态：{text}")

# =============== 后台线程：处理 & 上报进度 ===============
def run_ffmpeg_with_progress(cmd: List[str], q: Queue, stop_flag: threading.Event,
                             duration: Optional[float]) -> Tuple[Optional[bool], bool]:
    creation_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creation_flag
        )
    except FileNotFoundError:
        q.put({"type": "error", "msg": "无法找到 ffmpeg，请确认已安装并加入 PATH。"})
        return False, True
    except Exception as e:
        q.put({"type": "error", "msg": f"无法启动 ffmpeg：{e}"})
        return False, True

    processed = 0.0
    speed: Optional[str] = None
    last_emit = time.time()

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if stop_flag.is_set():
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1.5)
                    except Exception:
                        if proc.poll() is None:
                            proc.kill()
                except Exception:
                    pass
                q.put({"type": "cancelled"})
                return None, False

            s = line.strip()
            if not s:
                continue
            if s.startswith("out_time_ms="):
                try:
                    v = int(s.split("=", 1)[1]) / 1_000_000.0
                    if v >= processed:
                        processed = v
                except Exception:
                    pass
            elif s.startswith("speed="):
                speed = s.split("=", 1)[1].strip()

            now = time.time()
            if now - last_emit >= 0.2:
                last_emit = now
                q.put({"type": "prog_file", "processed": processed, "total": duration, "speed": speed})

        proc.wait()
        ok = (proc.returncode == 0)
        if ok and duration:
            q.put({"type": "prog_file", "processed": duration, "total": duration, "speed": speed})
        return ok, False
    except Exception as e:
        try:
            if proc and proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        q.put({"type": "error", "msg": f"处理异常：{e}"})
        return False, True


def worker_thread(files: List[Path], plan: EncodePlan, out_dir: Path, q: Queue, stop_flag: threading.Event):
    """
    事件：
      - start_file {name,total}
      - prog_file  {processed,total,speed}
      - end_file   {ok,name,out}
      - overall    {done,total}
      - error      {msg}
      - cancelled  {}
      - done_all   {}
    """
    total = len(files)
    done = 0
    out_dir.mkdir(parents=True, exist_ok=True)

    for idx, in_path in enumerate(files, 1):
        if stop_flag.is_set():
            break

        # 输出文件名（统一放到 out_dir；若存在自动加序号避免覆盖）
        if plan.replace_ext:
            out_name = in_path.with_suffix(plan.out_suffix).name
        else:
            out_name = in_path.stem + plan.out_suffix
        out_path = ensure_unique_path(out_dir / out_name)

        duration = ffprobe_duration(in_path)
        q.put({"type": "start_file", "name": in_path.name, "total": duration})

        args = list(plan.args)
        if not plan.replace_ext:
            args = maybe_add_scale(args, in_path)

        cmd = [
            FFMPEG, "-y", "-hide_banner",
            "-loglevel", "warning",
            "-nostats", "-stats_period", "0.4",
            "-progress", "pipe:1",
            "-i", str(in_path)
        ] + args + [str(out_path)]

        ok, fatal = run_ffmpeg_with_progress(cmd, q, stop_flag, duration)
        if ok is None:
            return
        if ok:
            done += 1
        q.put({"type": "end_file", "ok": bool(ok), "name": in_path.name, "out": str(out_path)})
        q.put({"type": "overall", "done": done, "total": total})
        if fatal:
            break

    q.put({"type": "done_all"})


def merge_worker(files: List[Path], out_path: Path, q: Queue, stop_flag: threading.Event):
    out_path.parent.mkdir(parents=True, exist_ok=True)

    durations = [ffprobe_duration(p) for p in files]
    if all(d is not None for d in durations):
        total_duration = sum(float(d) for d in durations if d is not None)
    else:
        total_duration = None

    q.put({"type": "start_file", "name": out_path.name, "total": total_duration})

    list_path: Optional[Path] = None
    try:
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
                list_path = Path(tf.name)
                for file_path in files:
                    tf.write(f"file '{escape_concat_path(file_path.resolve())}'\n")
        except Exception as e:
            q.put({"type": "error", "msg": f"生成合并列表失败：{e}"})
            q.put({"type": "done_all"})
            return

        cmd = [
            FFMPEG, "-y", "-hide_banner",
            "-loglevel", "warning",
            "-nostats", "-stats_period", "0.4",
            "-progress", "pipe:1",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",
            str(out_path)
        ]

        ok, fatal = run_ffmpeg_with_progress(cmd, q, stop_flag, total_duration)
        if ok is None:
            return
        done = 1 if ok else 0
        q.put({"type": "end_file", "ok": bool(ok), "name": out_path.name, "out": str(out_path)})
        q.put({"type": "overall", "done": done, "total": 1})
        if fatal:
            q.put({"type": "done_all"})
            return
    finally:
        if list_path and list_path.exists():
            try:
                list_path.unlink()
            except Exception:
                pass

    q.put({"type": "done_all"})


def split_worker(in_path: Path, split_points: List[float], out_dir: Path, q: Queue, stop_flag: threading.Event):
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = ffprobe_duration(in_path)
    points = list(split_points)
    segments: List[Tuple[float, Optional[float]]] = []
    start = 0.0
    for point in points:
        segments.append((start, point))
        start = point
    segments.append((start, duration))

    total_segments = len(segments)
    done = 0
    suffix = in_path.suffix or ".mp4"

    for idx, (seg_start, seg_end) in enumerate(segments, 1):
        if stop_flag.is_set():
            break
        seg_duration = None
        if seg_end is not None:
            seg_duration = max(seg_end - seg_start, 0.0)
        elif duration is not None:
            seg_duration = max(duration - seg_start, 0.0)

        out_name = f"{in_path.stem}_part{idx:02d}{suffix}"
        out_path = ensure_unique_path(out_dir / out_name)
        if seg_end is not None:
            seg_label = f"{out_path.name} ({format_hms(seg_start)}-{format_hms(seg_end)})"
        else:
            seg_label = f"{out_path.name} ({format_hms(seg_start)}-结束)"
        q.put({"type": "start_file", "name": seg_label, "total": seg_duration})

        cmd = [
            FFMPEG, "-y", "-hide_banner",
            "-loglevel", "warning",
            "-nostats", "-stats_period", "0.4",
            "-progress", "pipe:1",
            "-i", str(in_path)
        ]
        if seg_start > 0:
            cmd += ["-ss", format_timestamp_precise(seg_start)]
        if seg_duration and seg_duration > 0:
            cmd += ["-t", format_timestamp_precise(seg_duration)]
        cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero", str(out_path)]

        ok, fatal = run_ffmpeg_with_progress(cmd, q, stop_flag, seg_duration)
        if ok is None:
            return
        if ok:
            done += 1
        q.put({"type": "end_file", "ok": bool(ok), "name": out_path.name, "out": str(out_path)})
        q.put({"type": "overall", "done": done, "total": total_segments})
        if fatal:
            break

    q.put({"type": "done_all"})

# =============== 主流程 ===============
def main():
    ensure_ff_tools()

    mode = choose_mode()
    worker_target = None
    worker_args_base: Tuple = ()
    out_dir: Optional[Path] = None
    total_files = 0

    if mode in {"quality", "size", "audio"}:
        plan = build_plan(mode)
        files = choose_files()
        if not files:
            messagebox.showinfo("提示", "未选择任何视频文件。")
            return
        out_dir = choose_output_dir(default=Path(".").resolve())
        if not out_dir:
            messagebox.showinfo("提示", "未选择保存位置，已取消。")
            return
        worker_target = worker_thread
        worker_args_base = (files, plan, out_dir)
        total_files = len(files)

    elif mode == "merge":
        files = choose_files(multiple=True, title="选择要合并的视频文件（顺序可在下一步调整）")
        if len(files) < 2:
            messagebox.showinfo("提示", "请至少选择两个视频文件。")
            return
        ordered = reorder_files(files)
        if not ordered or len(ordered) < 2:
            messagebox.showinfo("提示", "未确认合并顺序，已取消。")
            return
        default_dir = ordered[0].parent.resolve()
        out_dir = choose_output_dir(default=default_dir)
        if not out_dir:
            messagebox.showinfo("提示", "未选择保存位置，已取消。")
            return
        exts = {p.suffix.lower() for p in ordered if p.suffix}
        if len(exts) == 1:
            ext = exts.pop()
        else:
            ext = ".mp4"
        default_name = ordered[0].stem + "_merged" + ext
        merge_out_path = ensure_unique_path(out_dir / default_name)
        worker_target = merge_worker
        worker_args_base = (ordered, merge_out_path)
        total_files = 1

    elif mode == "split":
        selected = choose_files(multiple=False, title="选择要拆分的视频文件")
        if not selected:
            messagebox.showinfo("提示", "未选择任何视频文件。")
            return
        in_path = selected[0]
        duration = ffprobe_duration(in_path)
        split_points = ask_split_points(duration)
        if split_points is None:
            messagebox.showinfo("提示", "未输入拆分时间戳，已取消。")
            return
        if not split_points:
            messagebox.showinfo("提示", "需要至少一个拆分时间戳。")
            return
        out_dir = choose_output_dir(default=in_path.parent.resolve())
        if not out_dir:
            messagebox.showinfo("提示", "未选择保存位置，已取消。")
            return
        worker_target = split_worker
        worker_args_base = (in_path, split_points, out_dir)
        total_files = len(split_points) + 1

    else:
        messagebox.showerror("错误", "未知模式。")
        return

    if total_files <= 0 or worker_target is None or out_dir is None:
        messagebox.showerror("错误", "未能初始化处理任务。")
        return

    # 父窗口（隐藏）
    root = tk.Tk()
    root.withdraw()

    dlg = ProgressDialog(root, total_files=total_files)
    dlg.set_status("准备中...")
    dlg.update_idletasks()

    q: Queue = Queue()
    stop_flag = threading.Event()

    # 启动后台线程
    worker_args = tuple(worker_args_base) + (q, stop_flag)
    t = threading.Thread(target=worker_target, args=worker_args, daemon=True)
    t.start()

    done_count = 0  # ✅ 已完成文件数
    start_time = time.time()  # （若你用了“总用时”功能）
    current_total = None

    def poll_queue():
        nonlocal done_count, start_time

        try:
            while True:
                msg = q.get_nowait()
                typ = msg.get("type")

                if typ == "start_file":
                    # 新文件开始：重置当前文件 UI
                    dlg.set_file(msg["name"])
                    dlg.set_status("开始处理")

                elif typ == "prog_file":
                    # dur = 单文件总时长（秒），避免与总文件数冲突
                    dur = msg["total"]
                    dlg.set_file_progress(msg["processed"], dur, msg.get("speed"))

                elif typ == "end_file":
                    ok = msg["ok"];
                    name = msg["name"];
                    outp = msg["out"]
                    if ok:
                        done_count += 1
                        dlg.set_status(f"完成：{name} → {outp}")
                    else:
                        dlg.set_status(f"失败：{name}（详见控制台）")
                    # ✅ 总进度始终用 total_files
                    dlg.set_overall(done_count, total_files)

                elif typ == "overall":
                    # 即使 worker 也发 overall，这里也用其数据，但不改变 total_files
                    dlg.set_overall(msg["done"], total_files)

                elif typ == "error":
                    dlg.set_status(msg["msg"])
                    messagebox.showerror("错误", msg["msg"])

                elif typ == "cancelled":
                    dlg.set_status("已取消")
                    messagebox.showwarning("提示", "任务已取消，部分文件可能未完成。")
                    try:
                        dlg.destroy()
                    finally:
                        try:
                            root.quit()  # 结束主循环
                        except Exception:
                            pass
                    return  # 结束轮询

                elif typ == "done_all":
                    dlg.set_status("全部完成")
                    messagebox.showinfo("提示", f"所有文件处理完成！\n输出目录：{out_dir}")
                    dlg.set_overall(done_count, total_files)
                    dlg.mark_done()  # 不自动关闭，按钮变“关闭”
                q.task_done()
        except Empty:
            pass

        # 如果你开启了“总用时”，这行保留，否则删掉
        try:
            dlg.set_elapsed(time.time() - start_time)
        except Exception:
            pass

        if dlg.cancelled and not stop_flag.is_set():
            stop_flag.set()

        if t.is_alive():
            dlg.after(500, poll_queue)

    dlg.after(100, poll_queue)
    root.mainloop()

    # 善后：确保线程结束 & 退出进程
    try:
        stop_flag.set()
        if t.is_alive():
            t.join(timeout=2.0)
    except Exception:
        pass

    try:
        root.destroy()
    except Exception:
        pass

    print(f"输出目录：{out_dir}")
    if dlg.cancelled:
        print("[提示] 用户已取消。")

    sys.exit(0)

if __name__ == "__main__":
    main()
