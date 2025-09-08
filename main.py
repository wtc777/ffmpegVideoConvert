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
from tkinter import ttk, filedialog, messagebox

# =============== 选择模式 ===============
class ModeSelector(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("选择处理模式")
        self.geometry("420x230")
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

        ttk.Button(self, text="下一步：选择文件", command=self.destroy).pack(pady=12)

def choose_mode() -> str:
    app = ModeSelector()
    app.mainloop()
    return app.choice.get()

def choose_files() -> List[Path]:
    root = tk.Tk()
    root.withdraw()
    paths = filedialog.askopenfilenames(
        title="选择要处理的视频文件（可多选）",
        filetypes=[("Video files", "*.mp4 *.mov *.mkv *.avi *.flv *.wmv *.m4v *.ts *.webm"),
                   ("All files", "*.*")]
    )
    root.update()
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

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,           # 进度键值
                stderr=subprocess.STDOUT,         # 合流
                text=True, encoding="utf-8", errors="replace",
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW  # ✅ 关键 ffmpeg后台静默运行
            )
        except FileNotFoundError:
            q.put({"type": "error", "msg": "无法找到 ffmpeg，请确认已安装并加入 PATH。"}); break
        except Exception as e:
            q.put({"type": "error", "msg": f"无法启动 ffmpeg：{e}"}); break

        processed = 0.0
        speed = None
        last_emit = time.time()

        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if stop_flag.is_set():
                    # 取消：优雅→超时→强杀；通知 UI
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
                    return

                s = line.strip()
                if not s:
                    continue

                if s.startswith("out_time_ms="):
                    try:
                        v = int(s.split("=", 1)[1]) / 1_000_000.0
                        if v >= processed:  # 单调前进
                            processed = v
                    except Exception:
                        pass
                elif s.startswith("speed="):
                    speed = s.split("=", 1)[1].strip()  # 形如 1.23x

                now = time.time()
                if now - last_emit >= 0.2:
                    last_emit = now
                    q.put({"type": "prog_file", "processed": processed, "total": duration, "speed": speed})

            proc.wait()
            ok = (proc.returncode == 0)
            if ok:
                done += 1

            q.put({"type": "end_file", "ok": ok, "name": in_path.name, "out": str(out_path)})
            q.put({"type": "overall", "done": done, "total": total})

        except Exception as e:
            try:
                if proc and proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
            q.put({"type": "error", "msg": f"处理异常：{e}"})
            break

    q.put({"type": "done_all"})

# =============== 主流程 ===============
def main():
    ensure_ff_tools()

    mode = choose_mode()
    plan = build_plan(mode)

    files = choose_files()
    if not files:
        messagebox.showinfo("提示", "未选择任何视频文件。")
        return

    # ✅ 选择输出目录（必选）
    out_dir = choose_output_dir(default=Path(".").resolve())
    if not out_dir:
        messagebox.showinfo("提示", "未选择保存位置，已取消。")
        return

    total_files = len(files)

    # 父窗口（隐藏）
    root = tk.Tk()
    root.withdraw()

    dlg = ProgressDialog(root, total_files=total_files)
    dlg.set_status("准备中...")
    dlg.update_idletasks()

    q: Queue = Queue()
    stop_flag = threading.Event()

    # 启动后台线程
    t = threading.Thread(target=worker_thread, args=(files, plan, out_dir, q, stop_flag), daemon=True)
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
