#!/usr/bin/env python3
# MonojoMusic — Tkinter + ffplay/ffprobe
# Requisitos en sistema: ffplay, ffprobe, (zenity opcional)
import os
import subprocess
import time
import random
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
from pathlib import Path
import signal
import shutil

BASE = Path.home() / ".config" / "MonojoMusic"
MUSIC_DIR = BASE / "Musicas"
PLAYLIST_DIR = BASE / "Playlists"
ICON_PATH = BASE / "monojo_amarillo.png"

BASE.mkdir(parents=True, exist_ok=True)
MUSIC_DIR.mkdir(parents=True, exist_ok=True)
PLAYLIST_DIR.mkdir(parents=True, exist_ok=True)

POLL_INTERVAL_MS = 250  # ms para actualizar UI

#----- mover el icono a la carpeta de configuración (~/.config/MonojoMusic/) ----
def first_run_setup():
    script_dir = Path(__file__).resolve().parent
    source_icon = script_dir / "monojo_amarillo.png"

    # si no existe el icono en config, lo copiamos
    if not ICON_PATH.exists():
        try:
            shutil.move(source_icon, ICON_PATH)
        except Exception as e:
            print(f"[WARN] No se pudo copiar el icono: {e}")

# ---------------- util: duración con ffprobe ----------------
def ffprobe_duration(path):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stderr=subprocess.DEVNULL, universal_newlines=True
        )
        return float(out.strip())
    except Exception:
        return 0.0

# ---------------- util: zenity (opcional) ----------------
def zenity_select_multiple_files(title="Selecciona archivos", initial_dir=None):
    try:
        cmd = ["zenity", "--file-selection", "--multiple", "--separator=|", "--title=" + title]
        if initial_dir:
            cmd += ["--filename=" + os.path.join(initial_dir, "")]
        out = subprocess.check_output(cmd, universal_newlines=True).strip()
        if not out:
            return []
        return out.split("|")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []

# ---------------- App ----------------
class MonojoMusicApp:
    def __init__(self, root):
        first_run_setup()

        self.root = root
        root.title("Monojo Music")
        try:
            root.iconphoto(True, tk.PhotoImage(file=ICON_PATH))
        except Exception:
            pass

        # estado reproducción / proceso
        self.play_proc = None
        self.current_path = None
        self.current_duration = 0.0
        self.play_start_time = 0.0   # offset lógico (s) desde donde reproducir/reanudar
        self.play_time_offset = 0.0  # time.time() cuando se lanzó ffplay
        self.is_playing = False      # True si ffplay corriendo
        self.paused_flag = False     # True si hemos pausado (no hay proceso y hay tiempo guardado)

        # flags
        self.loop_flag = False
        self.shuffle_flag = False
        self.from_playlist = False

        # playlist
        self.playlist_name = ""
        self.playlist_items = []    # nombres relativos en MUSIC_DIR
        self.playlist_index = 0

        # historial para shuffle (stack) — guarda índices previos
        self.shuffle_history = []

        # UI
        self.build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # init
        self.refresh_library()
        self.reload_playlist_listbox()
        self.root.after(POLL_INTERVAL_MS, self.poll_playback)

    def on_close(self):
        # matar ffplay si existe
        try:
            if self.play_proc:
                self.play_proc.terminate()
                try:
                    self.play_proc.wait(timeout=1)
                except Exception:
                    self.play_proc.kill()
        except Exception:
            pass

        # cerrar Tkinter
        self.root.destroy()

    def build_ui(self):
        top = tk.Frame(self.root); top.pack(fill="x", padx=6, pady=6)
        tk.Button(top, text="Nueva Playlist", command=self.new_playlist).pack(side="left", padx=4)
        tk.Button(top, text="Guardar Playlist", command=self.save_playlist).pack(side="left", padx=4)
        tk.Button(top, text="Cargar Playlist", command=self.choose_and_load_playlist).pack(side="left", padx=4)

        main = tk.Frame(self.root); main.pack(fill="both", expand=True, padx=6, pady=6)
        left = tk.Frame(main); left.pack(side="left", fill="both", expand=True)
        tk.Label(left, text="Biblioteca (Músicas)").pack(anchor="w")
        self.lib_listbox = tk.Listbox(left, selectmode="extended")
        self.lib_listbox.pack(fill="both", expand=True, padx=4, pady=4)
        lib_controls = tk.Frame(left); lib_controls.pack(fill="x")
        tk.Button(lib_controls, text="Añadir música", command=self.add_music).pack(side="left", padx=2)
        tk.Button(lib_controls, text="Eliminar música", command=self.delete_music).pack(side="left", padx=2)
        tk.Button(lib_controls, text="Añadir a Playlist →", command=self.add_selected_to_playlist).pack(side="right", padx=2)

        right = tk.Frame(main); right.pack(side="left", fill="both", expand=True, padx=(10,0))
        self.playlist_label = tk.Label(right, text="Playlist actual: (sin nombre)")
        self.playlist_label.pack(anchor="w")
        self.pl_listbox = tk.Listbox(right)
        self.pl_listbox.pack(fill="both", expand=True, padx=4, pady=4)
        pl_controls = tk.Frame(right); pl_controls.pack(fill="x")
        tk.Button(pl_controls, text="← Quitar de Playlist", command=self.remove_selected_from_playlist).pack(side="left", padx=2)
        tk.Button(pl_controls, text="↑", width=3, command=lambda: self.move_in_playlist(-1)).pack(side="left", padx=2)
        tk.Button(pl_controls, text="↓", width=3, command=lambda: self.move_in_playlist(1)).pack(side="left", padx=2)
        tk.Button(pl_controls, text="▶ Reproducir Playlist", command=self.play_playlist).pack(side="right", padx=2)

        bottom = tk.Frame(self.root); bottom.pack(fill="x", padx=6, pady=6)
        controls = tk.Frame(bottom); controls.pack(side="left")
        tk.Button(controls, text="⬅", width=3, command=self.prev_track).pack(side="left", padx=2)
        self.play_btn = tk.Button(controls, text="▶ Reproducir", command=self.play_selected_or_resume); self.play_btn.pack(side="left", padx=4)
        self.pause_btn = tk.Button(controls, text="Pausar", command=self.pause_toggle); self.pause_btn.pack(side="left", padx=4)
        # El botón Parar SIEMPRE muestra "Parar"
        self.stop_btn = tk.Button(controls, text="Parar", command=self.stop_action); self.stop_btn.pack(side="left", padx=4)
        tk.Button(controls, text="➡", width=3, command=self.next_track).pack(side="left", padx=2)
        aux = tk.Frame(bottom); aux.pack(side="left", padx=(10,0))
        self.loop_btn = tk.Button(aux, text="Bucle: OFF", command=self.toggle_loop); self.loop_btn.pack(side="left", padx=4)
        #self.shuffle_btn = tk.Button(aux, text="Aleatorio: OFF", command=self.toggle_shuffle); self.shuffle_btn.pack(side="left", padx=4)

        self.now_lbl = tk.Label(bottom, text="Ninguna canción seleccionada"); self.now_lbl.pack(side="left", padx=10)

        right_prog = tk.Frame(bottom); right_prog.pack(side="right")
        self.time_lbl = tk.Label(right_prog, text="00:00 / 00:00"); self.time_lbl.pack(side="right", padx=6)
        self.progress = tk.Scale(right_prog, from_=0, to=1, orient="horizontal", length=380,
                                 showvalue=False, command=self.on_progress_drag)
        self.progress.pack(side="right")
        self.progress.bind("<ButtonRelease-1>", self.on_progress_release)

    # ------------- biblioteca -------------
    def refresh_library(self):
        self.lib_listbox.delete(0, tk.END)
        try:
            VALID_EXT = (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".opus", ".mp4", ".mkv") # Los de video solo utiliza el audio

            items = sorted([
                f for f in os.listdir(MUSIC_DIR)
                if f.lower().endswith(VALID_EXT)
            ])
        except Exception:
            items = []
        for it in items:
            self.lib_listbox.insert(tk.END, it)

    def add_music(self):
        paths = zenity_select_multiple_files(title="Selecciona MP3 para añadir", initial_dir=MUSIC_DIR)
        if not paths:
            paths = filedialog.askopenfilenames(title="Selecciona MP3", initialdir=MUSIC_DIR, filetypes=[("MP3", "*.mp3")])
            if not paths:
                return
        added = 0
        for p in paths:
            if not p:
                continue
            try:
                dest = os.path.join(MUSIC_DIR, os.path.basename(p))
                if os.path.exists(dest) and os.path.realpath(p) == os.path.realpath(dest):
                    continue
                if os.path.exists(dest):
                    base, ext = os.path.splitext(os.path.basename(p))
                    k = 1
                    while os.path.exists(os.path.join(MUSIC_DIR, f"{base}_{k}{ext}")):
                        k += 1
                    dest = os.path.join(MUSIC_DIR, f"{base}_{k}{ext}")
                with open(p, "rb") as src, open(dest, "wb") as dst:
                    dst.write(src.read())
                added += 1
            except Exception:
                messagebox.showwarning("Error", f"No se pudo copiar: {p}")
        if added:
            self.refresh_library()

    def delete_music(self):
        sel = list(self.lib_listbox.curselection())
        if not sel:
            messagebox.showinfo("Eliminar MP3", "Selecciona archivos en la biblioteca para eliminar.")
            return
        names = [self.lib_listbox.get(i) for i in sel]
        if not messagebox.askyesno("Confirmar", f"¿Eliminar {len(names)} archivo(s) de Músicas?"):
            return
        for n in names:
            try:
                full = os.path.join(MUSIC_DIR, n)
                if os.path.exists(full):
                    os.remove(full)
            except Exception:
                messagebox.showwarning("Error", f"No se pudo borrar: {n}")
        self.refresh_library()
        self.playlist_items = [x for x in self.playlist_items if x not in names]
        self.reload_playlist_listbox()

    # ------------- playlist -------------
    def new_playlist(self):
        name = simpledialog.askstring("Nueva Playlist", "Nombre de la playlist (sin extensión):")
        if not name:
            return
        self.playlist_name = name
        self.playlist_items = []
        self.reload_playlist_listbox()
        self.update_playlist_label()
        messagebox.showinfo("Playlist", f"Playlist '{name}' creada (vacía).")

    def save_playlist(self):
        if not self.playlist_name:
            name = simpledialog.askstring("Guardar Playlist", "Nombre de la playlist (sin extensión):")
            if not name:
                return
            self.playlist_name = name
        path = os.path.join(PLAYLIST_DIR, self.playlist_name + ".txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                for it in self.playlist_items:
                    f.write(it + "\n")
            self.update_playlist_label()
            messagebox.showinfo("Guardado", f"Playlist guardada: {path}")
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo guardar playlist:\n{e}")

    def choose_and_load_playlist(self):
        files = [f for f in os.listdir(PLAYLIST_DIR) if f.endswith(".txt")]
        if not files:
            messagebox.showinfo("Playlists", "No hay playlists guardadas.")
            return
        choice = simpledialog.askstring("Cargar Playlist", "Escribe el nombre de la playlist:\n" + "\n".join([f[:-4] for f in files]))
        if not choice:
            return
        path = os.path.join(PLAYLIST_DIR, choice + ".txt")
        if not os.path.exists(path):
            messagebox.showerror("Error", "No existe esa playlist.")
            return
        self.playlist_name = choice
        loaded = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                name = line.strip()
                if os.path.exists(os.path.join(MUSIC_DIR, name)):
                    loaded.append(name)
        self.playlist_items = loaded
        self.reload_playlist_listbox()
        self.update_playlist_label()
        messagebox.showinfo("Cargada", f"Playlist '{choice}' cargada con {len(loaded)} canciones.")

    def reload_playlist_listbox(self):
        self.pl_listbox.delete(0, tk.END)
        for it in self.playlist_items:
            self.pl_listbox.insert(tk.END, it)
        self.update_playlist_label()

    def update_playlist_label(self):
        display = self.playlist_name if self.playlist_name else "(sin nombre)"
        self.playlist_label.config(text=f"Playlist actual: {display}")

    def add_selected_to_playlist(self):
        sel = list(self.lib_listbox.curselection())
        if not sel:
            return
        for i in sel:
            name = self.lib_listbox.get(i)
            if name not in self.playlist_items:
                self.playlist_items.append(name)
        self.reload_playlist_listbox()

    def remove_selected_from_playlist(self):
        sel = list(self.pl_listbox.curselection())
        if not sel:
            return
        for i in reversed(sel):
            try:
                del self.playlist_items[i]
            except Exception:
                pass
        self.reload_playlist_listbox()

    def move_in_playlist(self, direction):
        sel = self.pl_listbox.curselection()
        if not sel:
            return
        i = sel[0]
        j = i + direction
        if j < 0 or j >= len(self.playlist_items):
            return
        self.playlist_items[i], self.playlist_items[j] = self.playlist_items[j], self.playlist_items[i]
        self.reload_playlist_listbox()
        self.pl_listbox.select_set(j)

    # ------------ playback core ------------
    def play_selected_or_resume(self):
        pl_sel = self.pl_listbox.curselection()
        if pl_sel:
            self.playlist_index = pl_sel[0]
            self.play_playlist(start_index=self.playlist_index)
            return

        lib_sel = self.lib_listbox.curselection()
        if lib_sel:
            name = self.lib_listbox.get(lib_sel[0])
            self.play_file(os.path.join(MUSIC_DIR, name), start_at=0.0, from_playlist=False)
            return

        # resume from pause
        if self.paused_flag and self.current_path:
            self.play_file(self.current_path, start_at=self.play_start_time, from_playlist=self.from_playlist)
            self.paused_flag = False
            self.pause_btn.config(text="Pausar")
            return

        # if there's a loaded file but not playing start it
        if self.current_path and not self.is_playing:
            self.play_file(self.current_path, start_at=self.play_start_time, from_playlist=self.from_playlist)
            return

    def play_file(self, path, start_at=0.0, from_playlist=False):
        dur = ffprobe_duration(path) or 0.0
        if dur > 0 and start_at >= dur:
            start_at = max(0.0, dur - 0.5)

        self.stop_process()
        self.current_path = path
        self.current_duration = dur
        self.play_start_time = float(start_at)
        self.play_time_offset = time.time()
        self.from_playlist = bool(from_playlist)
        self.paused_flag = False
        # aseguramos que el botón Parar siempre diga "Parar"
        self.pause_btn.config(text="Pausar")

        try:
            self.play_proc = subprocess.Popen(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-ss", str(self.play_start_time), path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self.is_playing = True
            self.update_now_label()
        except FileNotFoundError:
            messagebox.showerror("Error", "ffplay no encontrado. Instala ffmpeg (ffplay).")
            self.play_proc = None
            self.is_playing = False

    def stop_process(self):
        if self.play_proc:
            try:
                self.play_proc.terminate()
                try:
                    self.play_proc.wait(timeout=0.4)
                except Exception:
                    self.play_proc.kill()
            except Exception:
                pass
        self.play_proc = None
        self.is_playing = False

    def pause_toggle(self):
        if self.is_playing:
            # pausar: guardar tiempo y matar proceso
            cur = self.get_playback_time()
            self.stop_process()
            self.play_start_time = min(cur, self.current_duration)
            self.paused_flag = True
            self.pause_btn.config(text="Continuar")
            self.update_now_label()
            return

        # si está en pausa -> continuar
        if self.paused_flag and self.current_path:
            self.play_file(self.current_path, start_at=self.play_start_time, from_playlist=self.from_playlist)
            self.paused_flag = False
            self.pause_btn.config(text="Pausar")
            return

        # si no está tocando y no está pausado -> iniciar desde start_time
        if not self.is_playing and self.current_path:
            self.play_file(self.current_path, start_at=self.play_start_time, from_playlist=self.from_playlist)

    def stop_action(self):
        """Parar: detiene y pone tiempo a 0. EL BOTÓN 'Parar' SIEMPRE MUESTRA 'Parar'."""
        if self.is_playing or self.play_proc:
            self.stop_process()
        # poner la canción a 0
        self.play_start_time = 0.0
        # no activamos texto 'Continuar' en el botón Parar; dejamos botón Pausar en "Pausar"
        self.paused_flag = False
        self.pause_btn.config(text="Pausar")
        self.update_now_label()
        self.update_time_and_progress(0.0, 0.0)

    def get_playback_time(self):
        if not self.current_path:
            return 0.0
        if self.is_playing and self.play_proc:
            elapsed = time.time() - self.play_time_offset
            t = self.play_start_time + elapsed
            if self.current_duration > 0:
                return min(t, self.current_duration)
            return t
        else:
            return min(self.play_start_time, self.current_duration) if self.current_duration > 0 else self.play_start_time

    # ---------- playlist playback ----------
    def play_playlist(self, start_index=0):
        if not self.playlist_items:
            messagebox.showinfo("Playlist", "La playlist está vacía.")
            return
        if start_index < 0 or start_index >= len(self.playlist_items):
            start_index = 0
        self.playlist_index = start_index
        name = self.playlist_items[self.playlist_index]
        path = os.path.join(MUSIC_DIR, name)
        if not os.path.exists(path):
            messagebox.showerror("Error", f"No existe: {name}")
            return
        self.play_file(path, start_at=0.0, from_playlist=True)

    def advance_playlist(self):
        if not self.playlist_items:
            self.stop_action()
            return

        if self.shuffle_flag:
            # registrar actual en historial antes de cambiar
            if 0 <= self.playlist_index < len(self.playlist_items):
                self.shuffle_history.append(self.playlist_index)
            if len(self.playlist_items) == 1:
                next_idx = 0
            else:
                choices = list(range(len(self.playlist_items)))
                try:
                    choices.remove(self.playlist_index)
                except Exception:
                    pass
                next_idx = random.choice(choices)
        else:
            next_idx = self.playlist_index + 1

        if not self.shuffle_flag and next_idx >= len(self.playlist_items):
            if self.loop_flag:
                next_idx = 0
            else:
                self.stop_action()
                return

        self.playlist_index = next_idx
        name = self.playlist_items[self.playlist_index]
        path = os.path.join(MUSIC_DIR, name)
        if os.path.exists(path):
            self.play_file(path, start_at=0.0, from_playlist=True)
        else:
            try:
                del self.playlist_items[self.playlist_index]
            except Exception:
                pass
            self.reload_playlist_listbox()
            self.advance_playlist()

    def prev_playlist(self):
        if not self.playlist_items:
            return
        if self.shuffle_flag and self.shuffle_history:
            idx = self.shuffle_history.pop()
        else:
            idx = self.playlist_index - 1
            if idx < 0:
                if self.loop_flag:
                    idx = len(self.playlist_items) - 1
                else:
                    idx = 0
        self.playlist_index = idx
        name = self.playlist_items[self.playlist_index]
        path = os.path.join(MUSIC_DIR, name)
        if os.path.exists(path):
            self.play_file(path, start_at=0.0, from_playlist=True)

    # ------------ global next/prev ------------
    def next_track(self):
        if self.from_playlist and self.playlist_items:
            self.advance_playlist()
            return

        lib_items = [self.lib_listbox.get(i) for i in range(self.lib_listbox.size())]
        if not lib_items:
            return

        curname = os.path.basename(self.current_path) if self.current_path else None
        if self.shuffle_flag:
            # historial para anterior en biblioteca
            if curname in lib_items:
                try:
                    self.shuffle_history.append(lib_items.index(curname))
                except Exception:
                    pass
            if len(lib_items) == 1:
                idx = 0
            else:
                choices = list(range(len(lib_items)))
                if curname in lib_items:
                    try:
                        choices.remove(lib_items.index(curname))
                    except Exception:
                        pass
                idx = random.choice(choices)
            name = lib_items[idx]
            self.play_file(os.path.join(MUSIC_DIR, name), start_at=0.0, from_playlist=False)
            return

        # no shuffle
        if curname and curname in lib_items:
            idx = lib_items.index(curname) + 1
        else:
            sel = self.lib_listbox.curselection()
            if sel:
                idx = sel[0] + 1
            else:
                idx = 0

        if idx >= len(lib_items):
            if self.loop_flag:
                idx = 0
            else:
                self.stop_action()
                return

        name = lib_items[idx]
        self.play_file(os.path.join(MUSIC_DIR, name), start_at=0.0, from_playlist=False)

    def prev_track(self):
        if self.from_playlist and self.playlist_items:
            self.prev_playlist()
            return

        lib_items = [self.lib_listbox.get(i) for i in range(self.lib_listbox.size())]
        if not lib_items:
            return

        curname = os.path.basename(self.current_path) if self.current_path else None
        if self.shuffle_flag:
            if self.shuffle_history:
                idx = self.shuffle_history.pop()
            else:
                if len(lib_items) == 1:
                    idx = 0
                else:
                    choices = list(range(len(lib_items)))
                    if curname in lib_items:
                        try:
                            choices.remove(lib_items.index(curname))
                        except Exception:
                            pass
                    idx = random.choice(choices)
            name = lib_items[idx]
            self.play_file(os.path.join(MUSIC_DIR, name), start_at=0.0, from_playlist=False)
            return

        if curname and curname in lib_items:
            idx = lib_items.index(curname) - 1
        else:
            sel = self.lib_listbox.curselection()
            if sel:
                idx = sel[0] - 1
            else:
                idx = len(lib_items) - 1 if self.loop_flag else 0

        if idx < 0:
            if self.loop_flag:
                idx = len(lib_items) - 1
            else:
                idx = 0

        name = lib_items[idx]
        self.play_file(os.path.join(MUSIC_DIR, name), start_at=0.0, from_playlist=False)

    # ---------- progreso / seeking ----------
    def on_progress_drag(self, value):
        try:
            v = float(value)
        except Exception:
            v = 0.0
        dur = max(1.0, self.current_duration)
        self.time_lbl.config(text=f"{self.format_time(v)} / {self.format_time(dur)}")

    def on_progress_release(self, event):
        if not self.current_path:
            self.progress.set(0)
            return
        val = self.progress.get()
        if val < 0: val = 0
        if val > self.current_duration: val = self.current_duration
        self.play_start_time = float(val)
        if self.is_playing:
            self.play_file(self.current_path, start_at=self.play_start_time, from_playlist=self.from_playlist)
        else:
            self.update_time_and_progress(self.play_start_time, self.current_duration)

    def format_time(self, sec):
        sec = max(0, int(sec))
        m = sec // 60; s = sec % 60
        return f"{m:02d}:{s:02d}"

    # ---------- polling ----------
    def poll_playback(self):
        try:
            if self.is_playing and self.play_proc:
                cur = self.get_playback_time()
                self.update_time_and_progress(cur, self.current_duration)
                if self.play_proc.poll() is not None:
                    self.handle_playback_end()
            else:
                if self.current_path:
                    cur = self.get_playback_time()
                    self.update_time_and_progress(cur, self.current_duration)
        except Exception:
            pass
        self.root.after(POLL_INTERVAL_MS, self.poll_playback)

    def handle_playback_end(self):
        if self.loop_flag:
            # reiniciar misma pista desde 0
            self.play_file(self.current_path, start_at=0.0, from_playlist=self.from_playlist)
            return

        # si venía de playlist -> avanzar
        name = os.path.basename(self.current_path) if self.current_path else None
        if name and name in self.playlist_items and self.from_playlist:
            if 0 <= self.playlist_index < len(self.playlist_items) and self.playlist_items[self.playlist_index] == name:
                self.advance_playlist()
                return
            else:
                self.stop_action()
                return

        # else stop
        self.stop_action()

    def update_time_and_progress(self, cur, dur):
        if dur <= 0:
            self.progress.config(to=1)
            self.progress.set(0)
            self.time_lbl.config(text="00:00 / 00:00")
            return
        try:
            self.progress.config(to=max(1, int(dur)))
            pos = min(int(cur), int(dur))
            self.progress.set(pos)
        except Exception:
            pass
        cur_disp = min(cur, dur) if dur > 0 else cur
        self.time_lbl.config(text=f"{self.format_time(cur_disp)} / {self.format_time(dur)}")

    def update_now_label(self):
        if not self.current_path:
            self.now_lbl.config(text="Ninguna canción seleccionada")
            return
        base = os.path.basename(self.current_path)
        if self.is_playing:
            state = "Reproduciendo"
        elif self.paused_flag:
            state = "Pausado"
        else:
            state = "Detenido"
        text = f"{state}: {base}"
        if self.playlist_items and base in self.playlist_items:
            try:
                idx = self.playlist_items.index(base) + 1
                text += f"  ({idx}/{len(self.playlist_items)})"
            except Exception:
                pass
        self.now_lbl.config(text=text)

    # ---------- toggles ----------
    def toggle_loop(self):
        self.loop_flag = not self.loop_flag
        self.loop_btn.config(text=f"Bucle: {'ON' if self.loop_flag else 'OFF'}")

    def toggle_shuffle(self):
        self.shuffle_flag = not self.shuffle_flag
        # limpiar historial al (des)activar
        self.shuffle_history = []
        self.shuffle_btn.config(text=f"Aleatorio: {'ON' if self.shuffle_flag else 'OFF'}")

# ---------- run ----------
if __name__ == "__main__":
    root = tk.Tk()
    app = MonojoMusicApp(root)
    root.mainloop()
