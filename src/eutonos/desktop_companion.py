"""Quiet shared EUTONOS desktop view. The popup observes; its one control section is Crosstalk's own toggle and Stop."""
from __future__ import annotations

import ctypes
import json
import math
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser

from . import desktop_companion_windows as windows
from . import home as eutonos_home
from . import update_background
from .desktop_companion_data import Collector, local_configuration, timestamp

POLL_SECONDS = 5
DASHBOARD_PORT = 8765
DASHBOARD_START_TIMEOUT = 60
DASHBOARD_PROBE_INTERVAL = 0.25
WINDOW_TITLE = 'EUTONOS Companion'


def count_text(value, basis='observed'):
    """Present missing/capped evidence without a fabricated exact zero."""
    if value is None or basis == 'unknown' or basis == 'partial' and value == 0:
        return 'unknown'
    return f'≥{value}' if basis == 'partial' else str(value)

def selected_root(explicit=None):
    candidates = [explicit, os.environ.get('EUTONOS_COMPANION_ROOT')]
    source = Path(__file__).resolve().parents[2]
    if (source / 'eutonos.installation.json').is_file():
        candidates.append(source)
    if (Path.cwd() / 'eutonos.installation.json').is_file():
        candidates.append(Path.cwd())
    for candidate in candidates:
        if candidate:
            root = Path(candidate).resolve()
            if not (root / 'eutonos.installation.json').is_file():
                # Explicit selection stays explicit; source diagnostics explain it.
                return root
            try:
                result = subprocess.run(['git', 'rev-parse', '--git-common-dir'], cwd=root,
                                        capture_output=True, text=True, timeout=5,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                common = Path(result.stdout.strip())
                if result.returncode == 0 and result.stdout.strip():
                    return (common if common.is_absolute() else root / common).resolve().parent
            except (OSError, subprocess.TimeoutExpired):
                pass
            return root
    return None


def age_text(value, now=None):
    now = time.time() if now is None else now
    at = timestamp(value)
    if at is None or at > now + 5:
        return 'time unknown'
    seconds = max(0, int(now - at))
    if seconds < 60:
        return 'just now'
    if seconds < 3600:
        return f'{seconds // 60}m ago'
    if seconds < 86400:
        return f'{seconds // 3600}h ago'
    return f'{seconds // 86400}d ago'


CROSSTALK_ACTION_LABELS = {'halt': 'Stop', 'resume': 'Resume', 'enable': 'Turn on', 'off': 'Turn off'}


def crosstalk_view(block, busy=False):
    """What the Crosstalk section shows, from one snapshot block (pure, no Tk). The toggle
    and Stop are enabled only when Crosstalk is installed and readable, nothing is running
    and it is not halted; a halt is resumed with Resume, never with the toggle."""
    block = block if isinstance(block, dict) else {}
    status = block.get('status')
    observed = status == 'observed'
    halted = observed and bool(block.get('halted'))
    on = observed and block.get('messaging') == 'on' and not halted
    helper = block.get('helper') if observed and isinstance(block.get('helper'), dict) else None
    if helper is None:
        helper_text, helper_ok = 'Helper unknown', None
    elif helper.get('alive'):
        helper_text, helper_ok = 'Helper running', True
    else:
        helper_text, helper_ok = ('Helper not running' if helper.get('ever_ran') else 'Helper never started'), False
    transit = count_text(block.get('in_transit'), block.get('count_basis', 'observed')) if observed else 'unknown'
    state_text = ('HALTED' if halted else 'On' if on else 'Off') if observed else 'Not installed' if status == 'missing' else 'unknown'
    return {
        'installed': observed, 'unknown': status not in ('observed', 'missing'),
        'halted': halted, 'on': on, 'state_text': state_text,
        'toggle_enabled': observed and not halted and not busy,
        'toggle_verb': 'off' if on else 'enable',
        'stop_enabled': observed and not halted and not busy,
        'resume_visible': halted,
        'resume_enabled': halted and not busy,
        'helper_text': helper_text, 'helper_ok': helper_ok,
        'transit_text': transit + (' letter in transit' if transit == '1' else ' letters in transit'),
        'wake_notice': block.get('wake_budget_notice') if observed else None,
        'denied': count_text(block.get('denied'), block.get('count_basis', 'observed')) if observed else None,
    }


class DesktopCompanion(windows.WindowPrimitives):
    def __init__(self, coordinator, window=None, standalone=False):
        self.coordinator = coordinator
        self.standalone = standalone
        self.standalone_position = None
        self.project_root = coordinator.project_root
        self.configuration = coordinator.configuration
        self.snapshot = coordinator.snapshot
        self.harness = (window or {}).get('harness', 'shared')
        self.handle = int((window or {}).get('handle', 0))
        self.title = (window or {}).get('title', 'Standalone')
        self.details_open = False
        super().__init__(coordinator.root)
        self.root.title(WINDOW_TITLE + ' · ' + self.harness.title())

    def _build(self):
        p = self.palette
        self._close_menu()
        for widget in self.root.winfo_children():
            widget.destroy()
        self.root.configure(bg=windows.TRANSPARENT)
        if not self.expanded:
            self.window_size = self.geometry_size = (36, 36)
            self.root.geometry('36x36')
            self.ring = tk.Canvas(self.root, width=36, height=36, bg=windows.TRANSPARENT,
                                  highlightthickness=0, bd=0)
            self.ring.pack()
            self._draw_badge_surface(self.ring)
            self._render_ring()
            self.ring.bind('<Button-1>', lambda _event: self._expand(True))
            return
        self.backdrop = tk.Canvas(self.root, bg=windows.TRANSPARENT, highlightthickness=0, bd=0)
        self.backdrop.place(x=0, y=0, relwidth=1, relheight=1)
        self.backdrop.bind('<Configure>', self._draw_popup_surface)
        self.panel = tk.Frame(self.root, bg=p['bg'])
        self.panel.pack(fill='both', expand=True, padx=2, pady=12)
        top = tk.Frame(self.panel, bg=p['bg'], cursor='fleur')
        top.pack(fill='x', padx=14, pady=(2, 5))
        menu = self._menu_icon(top)
        menu.pack(side='left', padx=(0, 8))
        menu.bind('<Button-1>', lambda _event: self._menu(menu))
        title = tk.Label(top, text='ε  EUTONOS Companion', bg=p['bg'], fg=p['text'], cursor='fleur', font=('Segoe UI', 10, 'bold'))
        title.pack(side='left')
        collapse = tk.Label(top, text='−', bg=p['bg'], fg=p['muted'], cursor='hand2', font=('Segoe UI', 15))
        collapse.pack(side='right')
        collapse.bind('<Button-1>', lambda _event: self._expand(False))
        for widget in (top, title):
            widget.bind('<ButtonPress-1>', self._drag_start)
            widget.bind('<B1-Motion>', self._drag_move)
            widget.bind('<ButtonRelease-1>', self._drag_end)
        self._row(self.harness.title() + ' · shared project activity', muted=True)
        self.body = tk.Frame(self.panel, bg=p['bg'])
        self.body.pack(fill='both', expand=True)
        self._render_data()
        self.root.update_idletasks()
        self.geometry_size = self.window_size = (390, min(max(300, min(900, self.root.winfo_screenheight() - 80)), self.root.winfo_reqheight() + 10))
        self.root.geometry(f'{self.geometry_size[0]}x{self.geometry_size[1]}')

    def _row(self, text, *, parent=None, muted=False, bold=False, action=None, color=None):
        p = self.palette
        label = tk.Label(parent or self.panel, text=text, bg=p['bg'],
                         fg=color or (p['accent'] if action else p['muted'] if muted else p['text']),
                         anchor='w', justify='left', wraplength=350,
                         font=('Segoe UI', 9, 'bold' if bold else 'normal'), cursor='hand2' if action else 'arrow')
        label.pack(fill='x', padx=15, pady=2)
        if action:
            label.bind('<Button-1>', lambda _event: action())
        return label

    def _section(self, text):
        tk.Frame(self.body, bg=self.palette['track'], height=1).pack(fill='x', padx=15, pady=(8, 5))
        self._row(text, parent=self.body, bold=True)

    @staticmethod
    def _draw_badge_surface(canvas):
        # Tk has no gradient primitive; clipped one-pixel rows keep this
        # small circular surface native and dependency-free.
        for y in range(1, 35):
            span = math.sqrt(max(0, 17 ** 2 - (y + .5 - 18) ** 2))
            shade = round(255 - 43 * (y - 1) / 33)
            canvas.create_line(18 - span, y, 18 + span, y,
                               fill=f'#{shade:02x}{shade:02x}{shade:02x}', width=1)
        canvas.create_oval(1, 1, 35, 35, outline='#aaaaaa', width=1)

    def _render_ring(self):
        self.ring.delete('data')
        health = self.snapshot.get('health', 'unknown')
        if not getattr(self, 'badge_image', None):
            image = Path(__file__).with_name('dashboard_assets') / 'eutonos-epsilon-64.png'
            try:
                self.badge_image = tk.PhotoImage(master=self.root, file=str(image)).subsample(3, 3)
            except (OSError, tk.TclError):
                self.badge_image = False
        if self.badge_image:
            self.ring.create_image(18, 18, image=self.badge_image, tags='data')
        else:
            self.ring.create_text(18, 18, text='ε', fill='#111111', font=('Segoe UI', 20, 'bold'), tags='data')
        if health == 'attention':
            self.ring.create_text(29, 9, text='!', fill='#b45309', font=('Segoe UI', 10, 'bold'), tags='data')
        elif update_background.update_view(self._updates())['show']:
            self.ring.create_text(29, 9, text='\u2191', fill='#0f766e', font=('Segoe UI', 10, 'bold'), tags='data')

    def _remember_detail_scroll(self):
        canvas = getattr(self, 'detail_canvas', None)
        try:
            if canvas is not None and canvas.winfo_exists():
                self.detail_scroll = canvas.yview()[0]
        except tk.TclError:
            pass
    def _render_data(self):
        if not hasattr(self, 'body'):
            return
        self._remember_detail_scroll()
        for child in self.body.winfo_children():
            child.destroy()
        snap = self.snapshot
        records = snap.get('records', {})
        crosstalk = snap.get('crosstalk', {})
        agents = snap.get('agents', {})
        attention = snap.get('attention', [])
        if attention:
            problem = attention[0]
            self._row('! ' + problem['title'], parent=self.body, bold=True, color='#b45309')
            self._row(problem['detail'], parent=self.body, muted=True)
            self._row('Open problem source' + (f' · {len(attention)} need attention' if len(attention) > 1 else ''),
                      parent=self.body, action=lambda: self._open_source(problem['source']))
        elif snap.get('health') == 'unknown':
            self._row('Some activity sources are unavailable', parent=self.body, muted=True)
        self._render_update()
        self._row('Open EUTONOS Dashboard ↗', parent=self.body, bold=True, action=self._open_dashboard)
        self._section('Records')
        for key, label in (('docs', 'Docs / work'), ('ram', 'RAM')):
            value = records.get(key) or {}
            text = f"{label} · {value.get('state', 'unknown')} · {age_text(value.get('updated_at'))}"
            if value.get('freshness') in ('stale', 'unknown'):
                text += ' · ' + ('older receipt' if value['freshness'] == 'stale' else 'time unknown')
            self._row(text, parent=self.body, muted=value.get('state') in (None, 'missing', 'unknown'),
                      action=(lambda path=value.get('source'): self._open_source(path)) if value.get('source') else None)
        self._render_crosstalk(crosstalk)
        self._section('Agents')
        for role, label in (('orchestrator', 'Orchestrators'), ('worker', 'Workers'), ('director', 'Directors')):
            value = agents.get('counts', {}).get(role, {})
            line = tk.Frame(self.body, bg=self.palette['bg'])
            line.pack(fill='x', padx=15, pady=2)
            tk.Label(line, text=f"{label} · {count_text(value.get('registered'), agents.get('count_basis', 'observed'))} registered, ", bg=self.palette['bg'],
                     fg=self.palette['text'], font=('Segoe UI', 9)).pack(side='left')
            tk.Label(line, text=f"{count_text(value.get('active'), agents.get('count_basis', 'observed'))} active", bg=self.palette['bg'],
                     fg=self.palette['text'], font=('Segoe UI', 9, 'bold')).pack(side='left')
        self._row('Hide details ▴' if self.details_open else 'Show agents & sources ▾', parent=self.body, action=self._toggle_details)
        if self.details_open:
            # Scroll only the optional detail section; compact summary stays visible.
            holder = tk.Frame(self.body, bg=self.palette['bg'])
            holder.pack(fill='x', padx=12, pady=2)
            self.root.update_idletasks()
            cap = max(300, min(900, self.root.winfo_screenheight() - 80))
            detail_height = max(40, min(180, cap - self.panel.winfo_reqheight() - 45))
            canvas = tk.Canvas(holder, height=detail_height, bg=self.palette['bg'], bd=0, highlightthickness=0)
            scroll = tk.Scrollbar(holder, orient='vertical', command=canvas.yview)
            scroll.pack(side='right', fill='y')
            canvas.pack(side='left', fill='both', expand=True)
            detail = tk.Frame(canvas, bg=self.palette['bg'])
            canvas.create_window((0, 0), window=detail, anchor='nw', width=350)
            self.detail_canvas = canvas
            canvas.configure(yscrollcommand=scroll.set)
            detail.bind('<Configure>', lambda _e: canvas.configure(scrollregion=canvas.bbox('all')))
            for row in agents.get('rows', []):
                self._row(f"{row['role']} · {row['harness']} · {row['state']}\n{row['assignment']} · {age_text(row['updated_at'])}",
                          parent=detail, bold=row['active'], muted=not row['active'])
                self._row(row.get('registration', 'registration basis unknown'), parent=detail, muted=True)
            if not agents.get('rows'):
                self._row('Agent lifecycle coverage unavailable', parent=detail, muted=True)
            self._row('Lifecycle · ' + agents.get('coverage_detail', agents.get('coverage', 'unknown')), parent=detail, muted=True)
            for item in snap.get('diagnostics', [])[:12]:
                self._row(item['code'] + ' · ' + Path(item['source']).name + ' · Open source', parent=detail, muted=True,
                          action=lambda path=item['source']: self._open_source(path))
            for problem in attention[1:] + records.get('inspection', []):
                self._row(problem['title'] + ' · ' + problem['detail'], parent=detail,
                          action=lambda path=problem['source']: self._open_source(path))
            self.root.update_idletasks()
            canvas.yview_moveto(getattr(self, 'detail_scroll', 0.0))
        self._row('Observed ' + age_text(snap.get('observed_at')) + ' · read only', parent=self.body, muted=True)
        if not self.project_root:
            self._row('Project unknown. Start with --root PATH to select EUTONOS.', parent=self.body, muted=True)

    def _render_crosstalk(self, block):
        """The popup's one control section: title and on/off toggle, then the running
        indicator and Stop. State always comes from a snapshot/readback, never from the click."""
        p = self.palette
        busy = getattr(self, 'crosstalk_busy', False)
        view = crosstalk_view(block, busy=busy)
        self.crosstalk_shown = view
        tk.Frame(self.body, bg=p['track'], height=1).pack(fill='x', padx=15, pady=(8, 5))
        head = tk.Frame(self.body, bg=p['bg'])
        head.pack(fill='x', padx=15, pady=2)
        tk.Label(head, text='Crosstalk', bg=p['bg'], fg=p['text'], font=('Segoe UI', 9, 'bold')).pack(side='left')
        enabled = view['toggle_enabled']
        switch = tk.Canvas(head, width=34, height=18, bg=p['bg'], highlightthickness=0, bd=0,
                           cursor='hand2' if enabled else 'arrow')
        track = (p['accent'] if view['on'] else '#9aa0a0') if enabled else ('#a9d6d3' if view['on'] else '#e4e6e6')
        switch.create_oval(0, 0, 18, 18, fill=track, outline=track)
        switch.create_oval(16, 0, 34, 18, fill=track, outline=track)
        switch.create_rectangle(9, 0, 25, 18, fill=track, outline=track)
        knob = 18 if view['on'] else 2
        switch.create_oval(knob, 2, knob + 14, 16, fill='#ffffff', outline=track)
        switch.pack(side='right')
        self.crosstalk_toggle = switch
        if enabled:
            switch.bind('<Button-1>', lambda _event, verb=view['toggle_verb']: self._crosstalk_act(verb))
        tk.Label(head, text=view['state_text'], bg=p['bg'], fg='#b91c1c' if view['halted'] else p['muted'],
                 font=('Segoe UI', 9, 'bold' if view['halted'] else 'normal')).pack(side='right', padx=(0, 8))
        if not view['installed']:
            self._row('Crosstalk status unknown' if view['unknown'] else 'Crosstalk not installed on this device',
                      parent=self.body, muted=True)
            self._button(self.body, 'Stop', None, enabled=False, danger=True).pack(anchor='e', padx=15, pady=2)
        else:
            if view['halted']:
                self._row('HALTED: nothing is sent or woken until you resume.', parent=self.body, bold=True, color='#b91c1c')
            line = tk.Frame(self.body, bg=p['bg'])
            line.pack(fill='x', padx=15, pady=2)
            dot = {True: '#15803d', False: '#b45309', None: p['muted']}[view['helper_ok']]
            tk.Label(line, text='●', bg=p['bg'], fg=dot, font=('Segoe UI', 9)).pack(side='left')
            tk.Label(line, text=f"{view['helper_text']} · {view['transit_text']}", bg=p['bg'], fg=p['text'],
                     font=('Segoe UI', 9), anchor='w').pack(side='left', padx=(4, 0))
            if view['resume_visible']:
                button = self._button(line, 'Resume', lambda: self._crosstalk_act('resume'), enabled=view['resume_enabled'])
            else:
                button = self._button(line, 'Stop', lambda: self._crosstalk_act('halt'), enabled=view['stop_enabled'], danger=True)
            button.pack(side='right')
            self.crosstalk_button = button
            if view['wake_notice']:
                self._row(view['wake_notice'], parent=self.body, muted=True)
            self._row(f"{view['denied']} denied", parent=self.body, muted=True)
            for item in (block or {}).get('recent_letters', [])[:3]:
                self._row(f"{item.get('from') or '?'} → {item.get('to') or '?'} · {item.get('state') or 'unknown'}",
                          parent=self.body, muted=True)
        note = getattr(self, 'crosstalk_note', None)
        if busy:
            self._row('Applying…', parent=self.body, muted=True)
        elif note:
            self._row(note, parent=self.body, bold=True, color='#b45309')

    def _updates(self):
        """The coordinator's update agent, or None (a view built without a coordinator has no offers)."""
        return getattr(getattr(self, 'coordinator', None), 'updates', None)

    def _render_update(self):
        """The update prompt, when the companion's update agent has an offer (it never installs by itself: only the
        Install now button does)."""
        view = update_background.update_view(self._updates())
        if not view['show']:
            return
        p = self.palette
        self._row(view['headline'], parent=self.body, bold=True, color=p['accent'])
        self._row(view['detail'], parent=self.body, muted=True)
        if view['note']:
            self._row(view['note'], parent=self.body, bold=True, color='#b45309')
        if view['buttons']:
            line = tk.Frame(self.body, bg=p['bg'])
            line.pack(fill='x', padx=15, pady=2)
            for label, verb in view['buttons']:
                self._button(line, label, lambda verb=verb: self._update_act(verb)).pack(side='left', padx=(0, 8))

    def _update_act(self, verb):
        agent = self._updates()
        if agent is None:
            return
        if verb == 'install':
            agent.install_now()
        elif verb == 'later':
            agent.later()
        self.coordinator.update_revision = agent.revision
        for view in list(self.coordinator.views.values()):
            view.update_offer_changed(prompt=False)

    def update_offer_changed(self, prompt=True):
        """Redraw for a new or changed offer. A newly seen offer opens the popup once (per release, per window) so the
        person is asked; a refresh after a click or a snooze does not."""
        agent = self._updates()
        offer = agent.visible_offer() if agent is not None else None
        seen = self.__dict__.setdefault('update_prompted', set())
        if prompt and offer and offer['artifact_id'] not in seen:
            seen.add(offer['artifact_id'])
            if not self.expanded:
                self._expand(True)
                return
        if self.expanded:
            self._render_data()
        elif hasattr(self, 'ring'):
            self._render_ring()

    def _button(self, parent, text, action, enabled=True, danger=False):
        p = self.palette
        label = tk.Label(parent, text=text, padx=10, pady=1, relief='solid', bd=1, font=('Segoe UI', 9),
                         bg=('#fef2f2' if danger else p['bg']) if enabled else p['track'],
                         fg=('#b91c1c' if danger else p['accent']) if enabled else p['muted'],
                         cursor='hand2' if enabled else 'arrow')
        if enabled and action:
            label.bind('<Button-1>', lambda _event: action())
        return label

    def _crosstalk_act(self, verb):
        """Run one of Crosstalk's own controls (halt, resume, enable, off) off the UI thread.
        The core action re-reads controls.json to prove the result and a fresh Crosstalk block
        is read back afterwards; the display changes only from that readback."""
        if verb not in CROSSTALK_ACTION_LABELS or getattr(self, 'crosstalk_busy', False):
            return
        self.crosstalk_busy = True
        self.crosstalk_note = None
        if getattr(self, 'crosstalk_results', None) is None:
            self.crosstalk_results = queue.Queue()
        results = self.crosstalk_results
        root = self.project_root

        def work():
            from . import crosstalk_dashboard_feeds, crosstalk_status
            base = root or crosstalk_status.default_roots()[0]
            try:
                result = crosstalk_dashboard_feeds.action(base, {'action': verb})
            except Exception as exc:
                result = {'ok': False, 'error': type(exc).__name__}
            try:
                block = Collector(base)._crosstalk(time.time())
                block['count_basis'] = 'observed' if block.get('status') == 'observed' else 'unknown'
            except Exception:
                block = {'status': 'unknown'}
            results.put((verb, result, block))

        threading.Thread(target=work, daemon=True).start()
        if getattr(self, 'expanded', False):
            self._render_data()

    def _apply_crosstalk_result(self, verb, result, block):
        self.crosstalk_busy = False
        self.snapshot = {**self.snapshot, 'crosstalk': block}
        label = CROSSTALK_ACTION_LABELS.get(verb, verb)
        if result.get('ok'):
            self.crosstalk_note = None
        else:
            readback = (f"readback shows messaging {block.get('messaging')}, {'halted' if block.get('halted') else 'not halted'}"
                        if block.get('status') == 'observed' else 'readback unavailable')
            self.crosstalk_note = f"{label} did not take effect ({result.get('error') or 'unknown error'}); {readback}."
        if getattr(self, 'expanded', False):
            self._render_data()

    def _drain_crosstalk_results(self):
        results = getattr(self, 'crosstalk_results', None)
        while results is not None:
            try:
                verb, result, block = results.get_nowait()
            except queue.Empty:
                break
            self._apply_crosstalk_result(verb, result, block)

    def _position(self):
        if not getattr(self, 'standalone', False):
            return super()._position()
        width, height = self.window_size
        if self.standalone_position is None:
            self.standalone_position = (max(0, self.root.winfo_screenwidth() - width - 20),
                                        max(0, self.root.winfo_screenheight() - height - 60))
        x, y = self.standalone_position
        x = min(max(0, x), max(0, self.root.winfo_screenwidth() - width))
        y = min(max(0, y), max(0, self.root.winfo_screenheight() - height))
        self.root.geometry(f'{width}x{height}+{x}+{y}')

    def _rebuild(self):
        self._remember_detail_scroll()
        super()._rebuild()
        if getattr(self, 'standalone', False):
            self._position()
            self.root.deiconify()

    def _drag_move(self, event):
        if not getattr(self, 'standalone', False):
            return super()._drag_move(event)
        if self.expanded and self.drag_origin:
            mouse_x, mouse_y, initial_x, initial_y = self.drag_origin
            self.standalone_position = (initial_x + event.x_root - mouse_x,
                                        initial_y + event.y_root - mouse_y)
            self._position()
            self.last_motion = time.monotonic()

    def _toggle_details(self):
        self.details_open = not self.details_open
        self._rebuild()

    def _menu_entries(self):
        entries = [('header', 'ICON APPEARANCE')]
        for mode, label in (('system', 'System'), ('light', 'Light'), ('dark', 'Dark')):
            entries.append(('item', label, self.theme_mode == mode, lambda value=mode: self._set_appearance(value)))
        entries += [('separator',),
                    ('item', 'Launch at sign-in', bool(self.startup_var.get()), self._toggle_startup),
                    ('item', 'Quit companion', False, self.quit)]
        return entries

    def _menu_icon(self, parent):
        """Three equal bars on a canvas: the font glyph's bars were unevenly spaced."""
        scale = getattr(self, 'dpi_scale', 1.0)
        bar, gap, width = max(2, round(2 * scale)), max(3, round(3 * scale)), round(14 * scale)
        height = 3 * bar + 2 * gap
        icon = tk.Canvas(parent, width=width, height=height, bg=self.palette['bg'],
                         highlightthickness=0, bd=0, cursor='hand2')
        icon.bars = [(0, n * (bar + gap), width, n * (bar + gap) + bar) for n in range(3)]
        self.menu_icon = icon
        self._redraw_menu_icon()
        return icon

    def _redraw_menu_icon(self):
        icon = getattr(self, 'menu_icon', None)
        try:
            if icon is None or not icon.winfo_exists():
                return
            icon.configure(bg=self.palette['bg'])
            icon.delete('bar')
            for box in icon.bars:
                icon.create_rectangle(*box, fill=self.palette['muted'], outline='', tags='bar')
        except tk.TclError:
            pass

    def _open_source(self, path):
        if not path:
            return
        selected = Path(path)
        # Sources are paths generated by the collector, never commands from mail.
        while not selected.exists() and selected != selected.parent:
            selected = selected.parent
        if selected.suffix == '.sqlite':
            self._row('Inspect required writes with workflow receipts --host HOST --root ROOT.', parent=self.body, muted=True)
            selected = selected.parent
        if selected.exists():
            try:
                os.startfile(str(selected))
            except OSError:
                self._row('Source could not be opened: ' + str(selected), parent=self.body, muted=True)

    def _open_dashboard(self):
        url = self.configuration.get('dashboard_url')
        launcher = self.configuration.get('dashboard_launcher')
        if url:
            webbrowser.open(url)
        elif launcher:
            # Probe off the UI thread so a click never freezes Tk.
            threading.Thread(target=self._open_running_or_launch, args=(launcher,), daemon=True).start()
        else:
            self._row('Dashboard launch unavailable. Configure dashboard_url in .eutonos-local/desktop-companion.json.', parent=self.body, muted=True)

    @staticmethod
    def _dashboard_listening(port=DASHBOARD_PORT, timeout=0.25):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=timeout):
                return True
        except OSError:
            return False

    def _open_running_or_launch(self, launcher):
        ready = self._dashboard_listening()
        if not ready:
            # Explicit user click uses the existing configured launch mechanism.
            # The marker leaves browser ownership with this readiness probe.
            quoted = launcher.replace("'", "''")
            command = ("$env:EUTONOS_DASHBOARD_OPENED_BY_COMPANION='1'; "
                       f"Start-Process -FilePath '{quoted}' -WindowStyle Hidden")
            try:
                subprocess.Popen(['powershell.exe', '-NoProfile', '-Command', command],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            except OSError:
                self._dashboard_startup_failed()
                return

            deadline = time.monotonic() + DASHBOARD_START_TIMEOUT
            while time.monotonic() < deadline:
                if self._dashboard_listening():
                    ready = True
                    break
                time.sleep(DASHBOARD_PROBE_INTERVAL)

        if ready:
            # The cold-start path opens only once the server can accept requests.
            webbrowser.open(f'http://127.0.0.1:{DASHBOARD_PORT}')
        else:
            self._dashboard_startup_failed()

    def _dashboard_startup_failed(self):
        root = getattr(self, 'root', None)
        if root is None:
            return

        def report():
            try:
                self._row('Dashboard did not become ready. Check the installed runtime and launcher.',
                          parent=self.body, muted=True)
            except (AttributeError, tk.TclError):
                pass

        try:
            root.after(0, report)
        except (AttributeError, RuntimeError, tk.TclError):
            pass

    def update_snapshot(self, snapshot):
        self.snapshot = snapshot
        if self.expanded:
            self._render_data()
        else:
            self._render_ring()

    def follow_window(self, foreground, observation_fresh=True):
        self._drain_crosstalk_results()
        if self.standalone:
            self._position()
            if self.root.state() == 'withdrawn':
                self.root.deiconify()
            return
        rect = windows.window_rect(self.handle) if observation_fresh else None
        if not rect:
            self.root.withdraw()
            self.codex_was_foreground = False
            return
        own = windows.USER32.GetAncestor(self.root.winfo_id(), windows.GA_ROOT)
        if rect != self.last_rect:
            self._position()
            self.last_motion = time.monotonic()
        pair = (own, self.handle)
        if self.window_owner != pair:
            ctypes.set_last_error(0)
            windows.USER32.SetWindowLongPtrW(own, -8, self.handle)
            if ctypes.get_last_error() == 0:
                self.window_owner = pair
        if self.root.state() == 'withdrawn':
            self.root.deiconify()
        windows.stack_above_owner(own, self.handle)
        self.codex_was_foreground = foreground == self.handle

    def quit(self):
        from .desktop_companion_runtime import set_paused
        set_paused(True)
        self.coordinator.quit()


def app_windows(observed, previous=None, foreground=0):
    """Choose one stable visible root window per recognized native app PID."""
    grouped = {}
    for item in observed[:64]:
        if not isinstance(item, dict) or item.get('harness') not in ('claude', 'codex', 'hermes'):
            continue
        if type(item.get('pid')) is not int or item['pid'] <= 0 or type(item.get('handle')) is not int or item['handle'] <= 0:
            continue
        grouped.setdefault((item['harness'], item['pid']), []).append(item)
    selected = {}
    for key, candidates in list(sorted(grouped.items()))[:16]:
        preferred = (previous or {}).get(key, {}).get('handle')
        selected[key] = next((item for item in candidates if item['handle'] == foreground), None) or next((item for item in candidates if item['handle'] == preferred), None) or min(candidates, key=lambda item: item['handle'])
    return selected


class Coordinator:
    """One hidden resident owns all app badges, one probe, and one collector."""
    def __init__(self, options):
        self.options = options
        self.project_root = selected_root(options.root)
        self.configuration = local_configuration(self.project_root) if self.project_root else {}
        self.collector = Collector(self.project_root, options.host or self.configuration.get('host') or (eutonos_home.hosts() / 'eutonos')) if self.project_root else None
        self.snapshot = {'health': 'unknown', 'attention': [], 'observed_at': None}
        self.snapshot_results = queue.Queue(maxsize=1)
        self.snapshot_busy = False
        self.last_snapshot = 0
        self.selected = {}
        self.views = {}
        self.foreground = 0
        self.last_observation = 0
        self.exit_code = 0
        self.closed = False
        try:
            self.updates = update_background.UpdateAgent()
        except Exception:
            self.updates = None
        self.update_revision = 0
        self.root = tk.Tk()
        self.root.title('EUTONOS Companion Coordinator')
        self.root.withdraw()
        # Force the withdrawn native wrapper to exist for readiness discovery.
        self.root.update_idletasks()
        self.native_root = windows.USER32.GetAncestor(self.root.winfo_id(), windows.GA_ROOT)
        self.root.report_callback_exception = self._callback_failure
        self.probe = windows.Probe()
        if options.standalone:
            self.views[('shared', 0)] = DesktopCompanion(self, standalone=True)
        self.root.after(100, self._tick)

    def _callback_failure(self, _kind, _error, _trace):
        # Tk otherwise silently stops a failing recurring callback. Exit with a
        # failure status so the hidden supervisor can restore the resident.
        from .desktop_companion_runtime import runtime_diagnostic
        runtime_diagnostic(self.project_root, 'callback-failed', error_type=getattr(_kind, '__name__', 'unknown'))
        self.exit_code = 1
        self.quit()

    def _sync_windows(self, observed, foreground):
        selected = app_windows(observed, self.selected, foreground)
        for key in list(self.views):
            if key != ('shared', 0) and key not in selected:
                self.views.pop(key).root.destroy()
        for key, item in selected.items():
            if key not in self.views:
                self.views[key] = DesktopCompanion(self, item)
            else:
                view = self.views[key]
                if view.handle != item['handle']:
                    view.handle, view.title = item['handle'], item.get('title', '')
                    view.window_owner = None
                    view.last_rect = None
                    view._rebuild()
        self.selected = selected

    def _tick(self):
        if self.closed:
            return
        self.probe.ensure_running()
        while True:
            try:
                observation = self.probe.messages.get_nowait()
            except queue.Empty:
                break
            if not observation.get('error') and isinstance(observation.get('windows'), list):
                self.foreground = observation.get('foreground', 0)
                self._sync_windows(observation['windows'], self.foreground)
                self.last_observation = time.monotonic()
        while True:
            try:
                self.snapshot = self.snapshot_results.get_nowait()
                self.snapshot_busy = False
                for view in self.views.values():
                    view.update_snapshot(self.snapshot)
            except queue.Empty:
                break
        now = time.monotonic()
        if self.collector and not self.snapshot_busy and now - self.last_snapshot >= POLL_SECONDS:
            self.snapshot_busy = True
            self.last_snapshot = now
            def read_snapshot():
                try:
                    fresh = self.collector.collect()
                except Exception:
                    fresh = {'health': 'unknown', 'attention': [], 'observed_at': time.time(), 'diagnostics': [{'code': 'collector-unavailable', 'source': str(self.project_root)}]}
                try:
                    self.snapshot_results.put_nowait(fresh)
                except queue.Full:
                    pass
            threading.Thread(target=read_snapshot, daemon=True).start()
        if time.time() - (self.snapshot.get('observed_at') or 0) > POLL_SECONDS * 3 and self.snapshot.get('health') != 'unknown':
            self.snapshot = {**self.snapshot, 'health': 'unknown'}
            for view in self.views.values():
                view.update_snapshot(self.snapshot)
        self._update_tick()
        if self.closed:
            return
        foreground = windows.USER32.GetForegroundWindow()
        for view in self.views.values():
            view.follow_window(foreground, now - self.last_observation < 2)
        interval = windows.MOVING_INTERVAL_MS if any(now - view.last_motion < .35 for view in self.views.values()) else windows.BACKGROUND_INTERVAL_MS
        self.root.after(interval, self._tick)

    def _update_tick(self):
        """Drive the update agent (a cheap poll; the daily check and Install now run on its own threads)."""
        agent = getattr(self, 'updates', None)
        if agent is None:
            return
        agent.tick()
        if agent.restart_requested:
            agent.restart_requested = False
            self._restart_for_update()
            if self.closed:
                return
        if agent.revision != getattr(self, 'update_revision', 0):
            self.update_revision = agent.revision
            for view in list(self.views.values()):
                view.update_offer_changed()

    def _restart_for_update(self):
        """After a good update: start a helper from the NEW runtime that waits for this companion to leave and starts the
        companion again, then leave. The old supervisor would otherwise restart the old runtime."""
        from . import desktop_companion_runtime as runtime
        python = update_background.active_runtime_python(self.updates.home)
        if python is not None and runtime.spawn_restart(python, self.options):
            runtime.set_paused(True)  # the old supervisor leaves; the helper clears this and starts the new generation
            self.quit()
        else:
            self.updates.install = {**(self.updates.install or {}), 'phase': 'done',
                                    'message': 'Sign out and back in (or restart the companion) to use it.'}
            self.updates.revision += 1

    def quit(self):
        if self.closed:
            return
        self.closed = True
        self.probe.close()
        self.root.quit()
        self.root.destroy()

    def run(self):
        self.root.mainloop()
        return self.exit_code
