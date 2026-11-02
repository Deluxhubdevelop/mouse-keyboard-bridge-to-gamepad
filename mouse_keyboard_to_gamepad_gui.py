#!/usr/bin/env python3
"""
mouse_keyboard_to_gamepad_gui.py
Versão definitiva: evdev puro, grab real, hotkey toggle sem hold, thread única.
"""

# =====================================================================
# NOTAS DA REVISÃO
#  - Nenhuma linha original foi apagada. Quando uma linha precisou mudar,
#    a original ficou comentada com "# [ORIGINAL]" e a nova vem logo abaixo.
#  - Requisitos: python-evdev, PyQt6, módulo uinput carregado e permissão em
#    /dev/uinput e /dev/input/event* (grupo "input" ou root).
# =====================================================================

import os
import re
import errno
import sys
import select
import signal
import traceback
import time
from evdev import InputDevice, UInput, ecodes as e, AbsInfo, list_devices

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QComboBox, QPushButton, QSpinBox, QGroupBox,
    QFormLayout, QTextEdit, QMessageBox, QTableWidget, QTableWidgetItem,
    QHeaderView
)
# [NOVO] imports extras usados nas melhorias
# [ORIGINAL v3] from PyQt6.QtCore import QSettings
from PyQt6.QtCore import QSettings, QTimer
from PyQt6.QtWidgets import QCheckBox, QDoubleSpinBox
import math

# ─────────────────────────────────────────────────────────────
# MAPEAMENTO EXATO DO PRESET
# ─────────────────────────────────────────────────────────────
DEFAULT_KEYMAP = {
    # [ORIGINAL] e.KEY_Q:         e.BTN_WEST,    # Q -> X (Deslizar)
    # [CORRIGIDO] no layout Xbox o botão X é o código 0x133 (= BTN_NORTH no evdev)
    e.KEY_Q:         e.BTN_NORTH,   # Q -> X (Deslizar)
    e.KEY_R:         e.BTN_EAST,    # R -> B (Recarregar)
    # [ORIGINAL] e.KEY_E:         e.BTN_NORTH,   # E -> Triângulo (Habilidade 2)
    # [CORRIGIDO] o Y/Triângulo do Xbox é o código 0x134 (= BTN_WEST no evdev)
    e.KEY_E:         e.BTN_WEST,    # E -> Triângulo/Y (Habilidade 2)
    e.KEY_F:         e.BTN_TL,      # F -> L1 (Habilidade 1)
    e.KEY_T:         e.BTN_TR,      # T -> R1 (Swap Weapon)
    e.KEY_V:         e.BTN_THUMBR,  # V -> R3
    e.KEY_LEFTSHIFT: e.BTN_THUMBL,  # Shift Esq -> L3
    e.KEY_SPACE:     e.BTN_SOUTH,   # Espaço -> A (Pulo)
    e.KEY_1:         e.BTN_START,   # 1 -> Start
    e.KEY_2:         e.BTN_SELECT,  # 2 -> Select
    e.KEY_C:         e.BTN_TL2,     # C -> L2
}

MOUSE_BTN_TO_R2 = e.BTN_LEFT
TOGGLE_KEY = e.KEY_F6
PANIC_KEYS = {e.KEY_F, e.KEY_T, e.KEY_1}
ABS_MIN = -32768
ABS_MAX = 32767

# Constantes de física da câmera
CAMERA_FRICTION = 0.82  # Quanto menor, mais rápido para (0.82 = 18% de redução por frame)
CAMERA_DEADZONE = 800

# [NOVO] Setas -> D-Pad (eixo, direção)
DPAD_KEYMAP = {
    e.KEY_UP:    (e.ABS_HAT0Y, -1),
    e.KEY_DOWN:  (e.ABS_HAT0Y, 1),
    e.KEY_LEFT:  (e.ABS_HAT0X, -1),
    e.KEY_RIGHT: (e.ABS_HAT0X, 1),
}

# [NOVO] Se True, o gamepad virtual se apresenta como um controle Xbox 360 real
# (layout idêntico ao driver xpad). É o formato que SDL/Wine/Proton/Steam
# reconhecem melhor. Se algo ficar estranho no seu jogo, ponha False.
EMULATE_XBOX360 = True
XBOX_NAME = "Microsoft X-Box 360 pad"
XBOX_VENDOR = 0x045E
XBOX_PRODUCT = 0x028E
XBOX_VERSION = 0x0110
VIRTUAL_NAMES = {"RobloxController", XBOX_NAME}

# [NOVO] No padrão Linux/SDL, "para cima" no analógico é valor NEGATIVO.
# Se um dia quiser o comportamento antigo (W = positivo), ponha False.
MOVE_Y_UP_IS_NEGATIVE = True
DIAGONAL_SCALE = 0.7071  # normaliza diagonais (W+D) pra não passar de 100% no círculo
LOOP_TIMEOUT = 0.005

# [NOVO] ── Modo "mouse real" ─────────────────────────────────────────
# O Roblox aplica no analógico direito: deadzone de 10% + curva exponencial
# (K_CURVATURE = 2). Por isso o analógico nunca parece um mouse. O modo abaixo
# aplica a curva INVERSA, deixando a câmera linear, como um mouse.
ROBLOX_K_CURVATURE = 2.0
ROBLOX_K_DEADZONE = 0.1
# Velocidade do mouse (contagens/s) que equivale ao analógico 100%.
# 480 = estimativa do Roblox (0.5°/contagem no mouse vs 4°/frame@60fps no analógico).
# Ajuste fino pelo campo "Sensibilidade (%)" da interface.
FULL_STICK_COUNTS_PER_SEC = 480.0
MIN_STICK_FRACTION = 0.003  # abaixo disso considera parado

# [NOVO v3] "Preservar ângulo": o analógico tem teto de velocidade (100% = 240°/s no Roblox).
# Quando o mouse é mais rápido que o teto, o excedente vira uma "dívida" de rotação que é
# paga depois (a câmera termina no mesmo ângulo do mouse, com pouco atraso).
CATCHUP_TAU = 0.03    # s: quão rápido a dívida é paga quando há folga
TAIL_CUTOFF = 0.02    # abaixo disso a dívida restante é descartada (evita 'deriva')

# [NOVO v3] botões exibidos no monitor ao vivo (código, nome no padrão Xbox)
MONITOR_BUTTONS = (
    (e.BTN_SOUTH, "A"), (e.BTN_EAST, "B"), (e.BTN_NORTH, "X"), (e.BTN_WEST, "Y"),
    (e.BTN_TL, "LB"), (e.BTN_TR, "RB"), (e.BTN_THUMBL, "L3"), (e.BTN_THUMBR, "R3"),
    (e.BTN_START, "Start"), (e.BTN_SELECT, "Back"),
)

# [NOVO v3] palavras que indicam o dispositivo ERRADO na seleção automática
MOUSE_BAD_WORDS = ("keyboard", "kbd", "consumer", "system control", "virtual")
KEYBOARD_BAD_WORDS = ("mouse", "consumer", "system control", "virtual", "power", "hotkey")


class WorkerThread(QThread):
    log_signal = pyqtSignal(str)
    status_signal = pyqtSignal(str)
    stopped_signal = pyqtSignal()

    # [ORIGINAL] def __init__(self, mouse_path, keyboard_path, sensitivity, deadzone):
    # [ORIGINAL v2] def __init__(self, mouse_path, keyboard_path, sensitivity, deadzone,
    # [ORIGINAL v2]                  friction=CAMERA_FRICTION, invert_y=False):
    # [ORIGINAL v3] def __init__(self, mouse_path, keyboard_path, sensitivity, deadzone,
    # [ORIGINAL v3]                  friction=CAMERA_FRICTION, invert_y=False,
    # [ORIGINAL v3]                  mouse_like=True, mouse_pct=100, vert_pct=100,
    # [ORIGINAL v3]                  smooth_ms=10, compensate=True):
    def __init__(self, mouse_path, keyboard_path, sensitivity, deadzone,
                 friction=CAMERA_FRICTION, invert_y=False,
                 mouse_like=True, mouse_pct=100, vert_pct=100,
                 smooth_ms=10, compensate=True,
                 keep_angle=True, max_lag_ms=150, swap_xy=False,
                 emulate_xbox=EMULATE_XBOX360):
        super().__init__()
        self.mouse_path = mouse_path
        self.keyboard_path = keyboard_path
        self.sensitivity = sensitivity
        self.deadzone = deadzone
        self.running = False
        self._ui = None

        self.mouse_dev = None
        self.keyboard_dev = None

        # Estados dos analógicos
        self.cam_x = 0.0
        self.cam_y = 0.0
        self.move_x = 0
        self.move_y = 0

        self.is_grabbed = False
        self.toggle_requested = False
        self.panic_requested = False

        # [NOVO] parâmetros ajustáveis ao vivo
        self.friction = friction
        self.invert_y = invert_y
        # [NOVO] parâmetros do modo mouse real
        self.mouse_like = mouse_like
        self.mouse_pct = mouse_pct
        self.vert_pct = vert_pct
        self.smooth_ms = smooth_ms
        self.compensate = compensate
        self._acc_x = 0.0
        self._acc_y = 0.0
        self._rate_x = 0.0
        self._rate_y = 0.0
        self._cam_t = time.monotonic()
        # [NOVO v3]
        self.keep_angle = keep_angle
        self.max_lag_ms = max_lag_ms
        self.swap_xy = swap_xy
        self.emulate_xbox = emulate_xbox
        self._debt = [0.0, 0.0]
        self._reset_pending = False
        self.n_mouse_events = 0
        self.n_key_events = 0

        # [NOVO] estado interno de saída (evita eventos duplicados e botões presos)
        self._last_abs = {}
        self._btn_sources = {}   # botão -> conjunto de origens que o seguram
        self._btn_state = {}     # botão -> True/False já enviado
        self._wasd = set()
        self._dpad_keys = set()
        self._cleaned = False

    def _clamp(self, v, lo, hi):
        return max(lo, min(hi, v))

    # ── [NOVO] ajustes ao vivo vindos da GUI ──────────────────
    # [ORIGINAL v2] def update_params(self, sensitivity, deadzone, friction, invert_y):
    # [ORIGINAL v3] def update_params(self, sensitivity, deadzone, friction, invert_y,
    # [ORIGINAL v3]                   mouse_like=None, mouse_pct=None, vert_pct=None,
    # [ORIGINAL v3]                   smooth_ms=None, compensate=None):
    def update_params(self, sensitivity, deadzone, friction, invert_y,
                      mouse_like=None, mouse_pct=None, vert_pct=None,
                      smooth_ms=None, compensate=None,
                      keep_angle=None, max_lag_ms=None, swap_xy=None):
        self.sensitivity = sensitivity
        self.deadzone = deadzone
        self.friction = friction
        self.invert_y = invert_y
        if mouse_like is not None: self.mouse_like = mouse_like
        if mouse_pct is not None: self.mouse_pct = mouse_pct
        if vert_pct is not None: self.vert_pct = vert_pct
        if smooth_ms is not None: self.smooth_ms = smooth_ms
        if compensate is not None: self.compensate = compensate
        if keep_angle is not None: self.keep_angle = keep_angle
        if max_lag_ms is not None: self.max_lag_ms = max_lag_ms
        if swap_xy is not None and swap_xy != self.swap_xy:
            self.swap_xy = swap_xy
            self._reset_pending = True  # solta botões presos ao trocar o layout

    # ── [NOVO] helpers de saída ───────────────────────────────
    def _write_abs(self, code, value):
        """Escreve eixo só se mudou. Retorna True se enviou algo."""
        value = int(value)
        if self._last_abs.get(code) == value:
            return False
        self._last_abs[code] = value
        self._ui.write(e.EV_ABS, code, value)
        return True

    def _set_button(self, source, btn, pressed):
        """
        Liga/desliga um botão considerando várias origens
        (ex.: Espaço e Clique Direito ambos -> A: só solta quando os dois soltarem).
        Gatilhos L2/R2 também são enviados como eixos analógicos.
        """
        srcs = self._btn_sources.setdefault(btn, set())
        if pressed:
            srcs.add(source)
        else:
            srcs.discard(source)
        state = bool(srcs)
        if self._btn_state.get(btn, False) == state:
            return
        self._btn_state[btn] = state

        if btn == e.BTN_TL2:
            self._write_abs(e.ABS_Z, 255 if state else 0)
            # [ORIGINAL v3] if not EMULATE_XBOX360:
            if not self.emulate_xbox:
                self._ui.write(e.EV_KEY, btn, int(state))
        elif btn == e.BTN_TR2:
            self._write_abs(e.ABS_RZ, 255 if state else 0)
            # [ORIGINAL v3] if not EMULATE_XBOX360:
            if not self.emulate_xbox:
                self._ui.write(e.EV_KEY, btn, int(state))
        else:
            self._ui.write(e.EV_KEY, btn, int(state))

    def _update_move(self):
        """Recalcula o analógico esquerdo a partir das teclas WASD seguradas."""
        x = (1 if e.KEY_D in self._wasd else 0) - (1 if e.KEY_A in self._wasd else 0)
        y = (1 if e.KEY_S in self._wasd else 0) - (1 if e.KEY_W in self._wasd else 0)
        if not MOVE_Y_UP_IS_NEGATIVE:
            y = -y
        scale = DIAGONAL_SCALE if (x and y) else 1.0
        self.move_x = int(x * scale * ABS_MAX)
        self.move_y = int(y * scale * ABS_MAX)
        self._write_abs(e.ABS_X, self.move_x)
        self._write_abs(e.ABS_Y, self.move_y)

    def _update_dpad(self):
        hx = (1 if e.KEY_RIGHT in self._dpad_keys else 0) - (1 if e.KEY_LEFT in self._dpad_keys else 0)
        hy = (1 if e.KEY_DOWN in self._dpad_keys else 0) - (1 if e.KEY_UP in self._dpad_keys else 0)
        self._write_abs(e.ABS_HAT0X, hx)
        self._write_abs(e.ABS_HAT0Y, hy)

    def _stick_from_fraction(self, r):
        """
        [NOVO] r = velocidade desejada (-1..1, 1 = máxima). Devolve o valor do analógico
        (-32767..32767) que faz o Roblox girar a câmera LINEARMENTE com r,
        desfazendo a deadzone + curva exponencial que o Roblox aplica.
        """
        a = abs(r)
        if a < MIN_STICK_FRACTION:
            return 0
        a = min(a, 1.0)
        if self.compensate:
            k = ROBLOX_K_CURVATURE
            dz = ROBLOX_K_DEADZONE
            x = dz + (1.0 - dz) * math.log(a * (math.exp(k) - 1.0) + 1.0) / k
        else:
            x = a
        return math.copysign(x, r) * ABS_MAX

    def _xy(self, btn):
        """[NOVO v3] troca X<->Y (BTN_NORTH<->BTN_WEST) se o usuário pedir."""
        if self.swap_xy:
            if btn == e.BTN_NORTH:
                return e.BTN_WEST
            if btn == e.BTN_WEST:
                return e.BTN_NORTH
        return btn

    def _limit_with_debt(self, f, idx, dt):
        """
        [NOVO v3] Limita a velocidade a 100% do analógico. O que passar do teto vira
        'dívida' e é pago quando houver folga, então a rotação total continua igual
        à do mouse. A dívida é limitada por max_lag_ms (evita ficar girando sem fim).
        """
        if not self.keep_angle:
            return max(-1.0, min(1.0, f))
        t = CATCHUP_TAU
        d = self._debt[idx]
        s = max(-1.0, min(1.0, f + d / t))
        d += (f - s) * dt
        dmax = max(0.0, float(self.max_lag_ms)) / 1000.0
        d = max(-dmax, min(dmax, d))
        if abs(f) < MIN_STICK_FRACTION and abs(d / t) < TAIL_CUTOFF:
            d = 0.0
            s = 0.0
        self._debt[idx] = d
        return s

    def _camera_step(self):
        """
        [NOVO] Câmera "mouse real": a velocidade do mouse (contagens/s, suavizada por
        uma janela curta) vira a posição do analógico. Sem inércia/atrito: parou o
        mouse, parou a câmera. A rotação total = contagens totais do mouse.
        """
        now = time.monotonic()
        dt = now - self._cam_t
        self._cam_t = now
        dt = max(1e-4, min(dt, 0.05))

        tau = max(1.0, float(self.smooth_ms)) / 1000.0
        alpha = 1.0 - math.exp(-dt / tau)
        self._rate_x += ((self._acc_x / dt) - self._rate_x) * alpha
        self._rate_y += ((self._acc_y / dt) - self._rate_y) * alpha
        self._acc_x = 0.0
        self._acc_y = 0.0

        gain_x = (self.mouse_pct / 100.0) / FULL_STICK_COUNTS_PER_SEC
        gain_y = gain_x * (self.vert_pct / 100.0)
        sx = self._limit_with_debt(self._rate_x * gain_x, 0, dt)
        sy = self._limit_with_debt(self._rate_y * gain_y, 1, dt)
        out_x = self._stick_from_fraction(sx)
        out_y = self._stick_from_fraction(sy)

        self.cam_x = out_x
        self.cam_y = out_y
        changed = self._write_abs(e.ABS_RX, out_x)
        changed = self._write_abs(e.ABS_RY, out_y) or changed
        if changed:
            self._ui.syn()

    def snapshot(self):
        """[NOVO v3] estado do gamepad virtual para o monitor da interface (só leitura)."""
        st = self._btn_state
        ab = self._last_abs
        return {
            "buttons": [(name, bool(st.get(code, False))) for code, name in MONITOR_BUTTONS],
            "lt": ab.get(e.ABS_Z, 0), "rt": ab.get(e.ABS_RZ, 0),
            "lx": ab.get(e.ABS_X, 0), "ly": ab.get(e.ABS_Y, 0),
            "rx": ab.get(e.ABS_RX, 0), "ry": ab.get(e.ABS_RY, 0),
            "hx": ab.get(e.ABS_HAT0X, 0), "hy": ab.get(e.ABS_HAT0Y, 0),
            "n_mouse": self.n_mouse_events, "n_key": self.n_key_events,
            "grabbed": self.is_grabbed,
        }

    def _reset_outputs(self):
        """Solta tudo e centraliza todos os eixos (evita botão/analógico preso)."""
        self.cam_x = 0.0
        self.cam_y = 0.0
        self.move_x = 0
        self.move_y = 0
        self._wasd.clear()
        self._dpad_keys.clear()
        self._acc_x = self._acc_y = 0.0
        self._rate_x = self._rate_y = 0.0
        self._debt = [0.0, 0.0]
        if not self._ui:
            return
        try:
            for btn in list(self._btn_sources.keys()):
                self._btn_sources[btn].clear()
                self._set_button("reset", btn, False)
            for code in (e.ABS_X, e.ABS_Y, e.ABS_RX, e.ABS_RY, e.ABS_Z,
                         e.ABS_RZ, e.ABS_HAT0X, e.ABS_HAT0Y):
                self._write_abs(code, 0)
            self._ui.syn()
        except Exception:
            pass

    def run(self):
        self.running = True

        try:
            self.mouse_dev = InputDevice(self.mouse_path)
            self.keyboard_dev = InputDevice(self.keyboard_path)
            self.log_signal.emit(f"Mouse:   {self.mouse_dev.name}")
            self.log_signal.emit(f"Teclado: {self.keyboard_dev.name}")

            capabilities = {
                e.EV_KEY: [
                    e.BTN_SOUTH, e.BTN_EAST, e.BTN_NORTH, e.BTN_WEST,
                    e.BTN_TL, e.BTN_TR, e.BTN_TL2, e.BTN_TR2,
                    e.BTN_START, e.BTN_SELECT, e.BTN_THUMBL, e.BTN_THUMBR
                ],
                e.EV_ABS: [
                    (e.ABS_X, AbsInfo(0, ABS_MIN, ABS_MAX, 0, 0, 0)), # Analógico Esq X (WASD)
                    (e.ABS_Y, AbsInfo(0, ABS_MIN, ABS_MAX, 0, 0, 0)), # Analógico Esq Y (WASD)
                    (e.ABS_RX, AbsInfo(0, ABS_MIN, ABS_MAX, 0, 0, 0)), # Analógico Dir X (Câmera)
                    (e.ABS_RY, AbsInfo(0, ABS_MIN, ABS_MAX, 0, 0, 0)), # Analógico Dir Y (Câmera)
                    (e.ABS_Z, AbsInfo(0, 0, 255, 0, 0, 0)),           # Gatilho L2
                    (e.ABS_RZ, AbsInfo(0, 0, 255, 0, 0, 0)),          # Gatilho R2
                ],
            }

            # [NOVO] D-Pad (setas) em ambos os modos
            hat_caps = [
                (e.ABS_HAT0X, AbsInfo(0, -1, 1, 0, 0, 0)),
                (e.ABS_HAT0Y, AbsInfo(0, -1, 1, 0, 0, 0)),
            ]

            # [ORIGINAL v3] if EMULATE_XBOX360:
            if self.emulate_xbox:
                # [NOVO] Layout idêntico ao de um Xbox 360 (xpad): sem BTN_TL2/TR2
                # (gatilhos são só eixos) e com BTN_MODE.
                xbox_caps = {
                    e.EV_KEY: [
                        e.BTN_SOUTH, e.BTN_EAST, e.BTN_NORTH, e.BTN_WEST,
                        e.BTN_TL, e.BTN_TR,
                        e.BTN_SELECT, e.BTN_START, e.BTN_MODE,
                        e.BTN_THUMBL, e.BTN_THUMBR
                    ],
                    e.EV_ABS: [
                        (e.ABS_X, AbsInfo(0, ABS_MIN, ABS_MAX, 16, 128, 0)),
                        (e.ABS_Y, AbsInfo(0, ABS_MIN, ABS_MAX, 16, 128, 0)),
                        (e.ABS_Z, AbsInfo(0, 0, 255, 0, 0, 0)),
                        (e.ABS_RX, AbsInfo(0, ABS_MIN, ABS_MAX, 16, 128, 0)),
                        (e.ABS_RY, AbsInfo(0, ABS_MIN, ABS_MAX, 16, 128, 0)),
                        (e.ABS_RZ, AbsInfo(0, 0, 255, 0, 0, 0)),
                    ],
                }
                xbox_caps[e.EV_ABS].extend(hat_caps)
                self._ui = UInput(xbox_caps, name=XBOX_NAME, vendor=XBOX_VENDOR,
                                  product=XBOX_PRODUCT, version=XBOX_VERSION,
                                  bustype=e.BUS_USB)
            else:
                capabilities[e.EV_ABS].extend(hat_caps)
                self._ui = UInput(capabilities, name="RobloxController")
            self.log_signal.emit("Gamepad virtual criado. Aguardando F6.")
            # [NOVO v3] diagnóstico: modo e nó do dispositivo virtual
            try:
                modo = "Xbox 360" if self.emulate_xbox else "genérico"
                self.log_signal.emit(f"Modo do gamepad: {modo} | nó: {self._ui.device.path}")
            except Exception:
                pass

            # [ORIGINAL] last_time = time.time()
            last_time = time.monotonic()  # [CORRIGIDO] relógio monotônico (não sofre ajuste de hora)
            pressed_panic_keys = set()

            # Loop único e coeso
            while self.running:
                # Processa toggle e pânico pendentes
                if self._reset_pending:
                    self._reset_pending = False
                    self._reset_outputs()

                if self.toggle_requested:
                    self.toggle_requested = False
                    self._do_toggle()

                if self.panic_requested:
                    self.panic_requested = False
                    self.log_signal.emit("Pânico (F+T+1) detectado! Fechando...")
                    self.running = False
                    break

                # Calcula o delta de tempo para o atrito
                # [ORIGINAL] current_time = time.time()
                current_time = time.monotonic()
                dt = current_time - last_time
                last_time = current_time

                # 1. Física da Câmera (Atrito) e envio
                # [ORIGINAL] if self.is_grabbed:
                # [CORRIGIDO] o modelo de atrito/inércia vira o modo "legado"
                if self.is_grabbed and not self.mouse_like:
                    self.cam_x = self._clamp(self.cam_x, ABS_MIN, ABS_MAX)
                    self.cam_y = self._clamp(self.cam_y, ABS_MIN, ABS_MAX)

                    # [ORIGINAL] if abs(self.cam_x) < CAMERA_DEADZONE: self.cam_x = 0
                    # [ORIGINAL] if abs(self.cam_y) < CAMERA_DEADZONE: self.cam_y = 0
                    # [CORRIGIDO] agora usa o valor de Deadzone da interface (antes era ignorado)
                    if abs(self.cam_x) < self.deadzone: self.cam_x = 0
                    if abs(self.cam_y) < self.deadzone: self.cam_y = 0

                    # [ORIGINAL] self._ui.write(e.EV_ABS, e.ABS_RX, int(self.cam_x))
                    # [ORIGINAL] self._ui.write(e.EV_ABS, e.ABS_RY, int(self.cam_y))
                    changed = self._write_abs(e.ABS_RX, self.cam_x)
                    changed = self._write_abs(e.ABS_RY, self.cam_y) or changed

                    # Fator de atrito ajustado para 60 FPS
                    # [ORIGINAL] friction_factor = CAMERA_FRICTION ** (dt * 60)
                    friction_factor = self.friction ** (dt * 60)
                    self.cam_x *= friction_factor
                    self.cam_y *= friction_factor
                    # [ORIGINAL] self._ui.syn()
                    if changed:
                        self._ui.syn()

                # 2. Lê eventos do mouse e teclado
                # [ORIGINAL] r, _, _ = select.select([self.mouse_dev, self.keyboard_dev], [], [], 0.005)
                r, _, _ = select.select([self.mouse_dev, self.keyboard_dev], [], [], LOOP_TIMEOUT)

                for dev in r:
                    if dev is self.mouse_dev:
                        for event in dev.read():
                            if not self.is_grabbed:
                                continue
                            self.n_mouse_events += 1
                            if event.type == e.EV_REL:
                                if event.code == e.REL_X:
                                    if self.mouse_like:
                                        self._acc_x += event.value
                                    else:
                                        self.cam_x += event.value * self.sensitivity
                                elif event.code == e.REL_Y:
                                    # [ORIGINAL] self.cam_y += event.value * self.sensitivity
                                    dy = -event.value if self.invert_y else event.value
                                    if self.mouse_like:
                                        self._acc_y += dy
                                    else:
                                        self.cam_y += dy * self.sensitivity
                            elif event.type == e.EV_KEY:
                                if event.code == MOUSE_BTN_TO_R2:
                                    # [ORIGINAL] val = 255 if event.value == 1 else 0
                                    # [ORIGINAL] self._ui.write(e.EV_ABS, e.ABS_RZ, val)
                                    self._set_button("mouse_left", e.BTN_TR2, event.value == 1)
                                elif event.code == e.BTN_RIGHT:
                                    # [ORIGINAL] self._ui.write(e.EV_KEY, e.BTN_SOUTH, event.value)
                                    self._set_button("mouse_right", e.BTN_SOUTH, event.value == 1)
                        self._ui.syn()

                    elif dev is self.keyboard_dev:
                        for event in dev.read():
                            if event.type != e.EV_KEY:
                                continue

                            # [CORRIGIDO] ignora auto-repeat (value 2): antes ele virava
                            # "valor 2" no botão do gamepad virtual.
                            if event.value == 2:
                                continue
                            self.n_key_events += 1

                            # Toggle (F6)
                            # [ORIGINAL] if event.code == TOGGLE_KEY and event.value == 1:
                            # [ORIGINAL]     self.toggle_requested = True
                            # [ORIGINAL]     continue
                            # [CORRIGIDO] alterna ao SOLTAR o F6 (o sistema vê o ciclo
                            # completo da tecla, sem F6 "preso") e na hora, sem esperar o loop.
                            if event.code == TOGGLE_KEY:
                                if event.value == 0:
                                    self._do_toggle()
                                    pressed_panic_keys.clear()
                                continue

                            if not self.is_grabbed:
                                continue

                            # Pânico (F+T+1)
                            if event.code in PANIC_KEYS:
                                if event.value == 1:
                                    pressed_panic_keys.add(event.code)
                                elif event.value == 0:
                                    pressed_panic_keys.discard(event.code)

                                if PANIC_KEYS.issubset(pressed_panic_keys):
                                    self.panic_requested = True
                                    break
                                # [ORIGINAL] continue
                                # [CORRIGIDO] o "continue" acima fazia F, T e 1 NUNCA chegarem
                                # ao mapeamento (L1, R1 e Start não funcionavam). Agora seguem.

                            # Mapeamento de teclas
                            if event.code in DEFAULT_KEYMAP:
                                # [ORIGINAL] self._ui.write(e.EV_KEY, DEFAULT_KEYMAP[event.code], event.value)
                                # [ORIGINAL v3] self._set_button(event.code, DEFAULT_KEYMAP[event.code], event.value == 1)
                                self._set_button(event.code, self._xy(DEFAULT_KEYMAP[event.code]), event.value == 1)

                            # [NOVO] Setas -> D-Pad
                            elif event.code in DPAD_KEYMAP:
                                if event.value == 1:
                                    self._dpad_keys.add(event.code)
                                else:
                                    self._dpad_keys.discard(event.code)
                                self._update_dpad()

                            # WASD -> Analógico Esquerdo
                            elif event.code in (e.KEY_W, e.KEY_A, e.KEY_S, e.KEY_D):
                                # [ORIGINAL] if event.code == e.KEY_W: self.move_y = ABS_MAX if event.value == 1 else 0
                                # [ORIGINAL] if event.code == e.KEY_S: self.move_y = ABS_MIN if event.value == 1 else 0
                                # [ORIGINAL] if event.code == e.KEY_A: self.move_x = ABS_MIN if event.value == 1 else 0
                                # [ORIGINAL] if event.code == e.KEY_D: self.move_x = ABS_MAX if event.value == 1 else 0
                                #
                                # [ORIGINAL] self._ui.write(e.EV_ABS, e.ABS_X, self.move_x)
                                # [ORIGINAL] self._ui.write(e.EV_ABS, e.ABS_Y, self.move_y)
                                # [CORRIGIDO] W/S estavam INVERTIDOS (no padrão de gamepad Linux
                                # "cima" é negativo). Também: W+S ou A+D juntos agora se anulam
                                # corretamente e as diagonais são normalizadas.
                                if event.value == 1:
                                    self._wasd.add(event.code)
                                else:
                                    self._wasd.discard(event.code)
                                self._update_move()
                        self._ui.syn()

                # [NOVO] câmera calculada DEPOIS de ler os eventos (menor latência)
                if self.is_grabbed and self.mouse_like:
                    self._camera_step()

        except PermissionError as ex:
            self.log_signal.emit(f"[ERRO] Permissão negada: {ex}")
            self.log_signal.emit("Dica: rode como root OU adicione seu usuário ao grupo 'input' "
                                 "e libere /dev/uinput (veja instruções no final do script).")
        except OSError as ex:
            # [NOVO v3] mensagem clara quando o mouse/teclado é desconectado
            if ex.errno == errno.ENODEV:
                self.log_signal.emit("[ERRO] Dispositivo desconectado (mouse/teclado). Reconecte e reinicie.")
            else:
                self.log_signal.emit(f"[ERRO] {ex}")
                self.log_signal.emit(traceback.format_exc())
        except Exception as ex:
            self.log_signal.emit(f"[ERRO] {ex}")
            self.log_signal.emit(traceback.format_exc())
        finally:
            self._cleanup()

    def _do_toggle(self):
        """Função real que executa o grab/ungrab."""
        if not self.mouse_dev or not self.keyboard_dev:
            return

        if self.is_grabbed:
            # [CORRIGIDO] tenta soltar cada dispositivo separadamente: se um falhar,
            # o outro ainda é liberado (antes um erro deixava o outro preso).
            errors = []
            for dev in (self.mouse_dev, self.keyboard_dev):
                try:
                    dev.ungrab()
                except Exception as ex:
                    errors.append(str(ex))
            self.is_grabbed = False
            self._reset_outputs()  # [NOVO] solta botões e centraliza eixos
            if errors:
                self.log_signal.emit(f"Erro ao ungrab: {'; '.join(errors)}")
            self.log_signal.emit("🔴 DESATIVADO - Mouse e teclado livres.")
            self.status_signal.emit("INATIVO (Aperte F6)")
        else:
            try:
                self.mouse_dev.grab()
                try:
                    self.keyboard_dev.grab()
                except Exception:
                    # [CORRIGIDO] rollback: se o teclado falhar, não deixa o mouse preso
                    try:
                        self.mouse_dev.ungrab()
                    except Exception:
                        pass
                    raise
                self.is_grabbed = True
                self.cam_x = 0
                self.cam_y = 0
                self._acc_x = self._acc_y = 0.0
                self._rate_x = self._rate_y = 0.0
                self._debt = [0.0, 0.0]
                self._cam_t = time.monotonic()
                self.log_signal.emit("🟢 ATIVADO - Controle ativo.")
                self.status_signal.emit("ATIVO — F6 para desativar")
            except Exception as ex:
                self.log_signal.emit(f"Erro ao grab: {ex}")

    def _cleanup(self):
        # [NOVO] proteção contra chamada dupla
        if self._cleaned:
            return
        self._cleaned = True
        self.running = False
        # [NOVO] solta tudo no gamepad virtual antes de fechar
        self._reset_outputs()
        # [CORRIGIDO] devolve mouse/teclado ANTES de destruir o gamepad virtual
        if self.mouse_dev:
            try: self.mouse_dev.ungrab()
            except Exception: pass
        if self.keyboard_dev:
            try: self.keyboard_dev.ungrab()
            except Exception: pass
        if self._ui:
            try: self._ui.close()
            except Exception: pass
        # [NOVO] fecha os descritores de arquivo
        for dev in (self.mouse_dev, self.keyboard_dev):
            if dev:
                try: dev.close()
                except Exception: pass
        self.is_grabbed = False

        self.status_signal.emit("INATIVO")
        self.log_signal.emit("Script finalizado.")
        self.stopped_signal.emit()

    def stop(self):
        self.running = False


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Mouse+Teclado → Gamepad (FINAL)")
        # [ORIGINAL] self.resize(600, 750)
        self.resize(640, 940)
        self.worker = None

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        dev_box = QGroupBox("Dispositivos")
        dev_layout = QFormLayout(dev_box)
        self.mouse_combo = QComboBox()
        self.keyboard_combo = QComboBox()
        self._populate_devices()
        refresh_btn = QPushButton("Atualizar lista")
        refresh_btn.clicked.connect(self._populate_devices)
        dev_layout.addRow("Mouse:", self.mouse_combo)
        dev_layout.addRow("Teclado:", self.keyboard_combo)
        dev_layout.addRow("", refresh_btn)
        root.addWidget(dev_box)

        param_box = QGroupBox("Parâmetros")
        param_layout = QFormLayout(param_box)
        self.sens_spin = QSpinBox()
        self.sens_spin.setRange(1, 20000)
        self.sens_spin.setValue(1500)
        self.sens_spin.setSuffix(" (câmera)")

        self.deadzone_spin = QSpinBox()
        self.deadzone_spin.setRange(0, 10000)
        self.deadzone_spin.setValue(500)

        # [NOVO] atrito da câmera e inversão do eixo Y, ajustáveis ao vivo
        self.friction_spin = QDoubleSpinBox()
        self.friction_spin.setRange(0.50, 0.99)
        self.friction_spin.setSingleStep(0.01)
        self.friction_spin.setDecimals(2)
        self.friction_spin.setValue(CAMERA_FRICTION)
        self.invert_check = QCheckBox("Inverter eixo Y da câmera")
        # [NOVO] modo mouse real
        self.mouse_like_check = QCheckBox("Modo mouse real (recomendado)")
        self.mouse_like_check.setChecked(True)
        self.mouse_pct_spin = QSpinBox()
        self.mouse_pct_spin.setRange(1, 2000)
        self.mouse_pct_spin.setValue(100)
        self.mouse_pct_spin.setSuffix(" %")
        self.vert_pct_spin = QSpinBox()
        self.vert_pct_spin.setRange(10, 300)
        self.vert_pct_spin.setValue(100)
        self.vert_pct_spin.setSuffix(" %")
        self.smooth_spin = QSpinBox()
        self.smooth_spin.setRange(1, 100)
        self.smooth_spin.setValue(10)
        self.smooth_spin.setSuffix(" ms")
        self.comp_check = QCheckBox("Compensar curva do analógico do Roblox")
        self.comp_check.setChecked(True)
        # [NOVO v3] preservar ângulo + compatibilidade
        self.keep_angle_check = QCheckBox("Preservar ângulo (compensa o teto do analógico)")
        self.keep_angle_check.setChecked(True)
        self.lag_spin = QSpinBox()
        self.lag_spin.setRange(10, 1000)
        self.lag_spin.setValue(150)
        self.lag_spin.setSuffix(" ms")
        self.swap_xy_check = QCheckBox("Trocar X ↔ Y (Q/E) — use se estiverem invertidos no jogo")
        self.xbox_check = QCheckBox("Emular controle Xbox 360 (aplica ao Iniciar)")
        self.xbox_check.setChecked(EMULATE_XBOX360)

        param_layout.addRow("Sensibilidade:", self.sens_spin)
        param_layout.addRow("Deadzone:", self.deadzone_spin)
        param_layout.addRow("Atrito (menor = para mais rápido):", self.friction_spin)
        param_layout.addRow("", self.invert_check)
        param_layout.addRow("", self.mouse_like_check)
        param_layout.addRow("Sensibilidade (100 = mouse):", self.mouse_pct_spin)
        param_layout.addRow("Sens. vertical:", self.vert_pct_spin)
        param_layout.addRow("Suavização:", self.smooth_spin)
        param_layout.addRow("", self.comp_check)
        param_layout.addRow("", self.keep_angle_check)
        param_layout.addRow("Atraso máx. (dívida):", self.lag_spin)
        param_layout.addRow("", self.swap_xy_check)
        param_layout.addRow("", self.xbox_check)
        root.addWidget(param_box)

        map_box = QGroupBox("Mapeamento")
        map_layout = QVBoxLayout(map_box)
        self.map_table = QTableWidget(0, 2)
        self.map_table.setHorizontalHeaderLabels(["Tecla / Botão", "Ação"])
        self.map_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._populate_keymap_table()
        map_layout.addWidget(self.map_table)

        info_lbl = QLabel(
            "<b>CÂMERA:</b> Movimento do Mouse (Analógico Direito)<br>"
            "<b>MOVIMENTO:</b> WASD (Analógico Esquerdo)<br>"
            "<b>ATIVAR/DESATIVAR:</b> F6 (Toggle)<br>"
            "<b>PÂNICO:</b> F + T + 1"
        )
        info_lbl.setStyleSheet("background: #222; color: #0f0; padding: 8px; border-radius: 4px;")
        map_layout.addWidget(info_lbl)
        root.addWidget(map_box)

        # [NOVO v3] monitor ao vivo do gamepad virtual (mostra exatamente o que o jogo recebe)
        mon_box = QGroupBox("Monitor do gamepad virtual (ao vivo)")
        mon_layout = QVBoxLayout(mon_box)
        self.monitor_label = QLabel("(script parado)")
        self.monitor_label.setStyleSheet("font-family: monospace; background: #111; color: #9f9; padding: 6px;")
        mon_layout.addWidget(self.monitor_label)
        root.addWidget(mon_box)

        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("▶ Iniciar Script")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("■ Encerrar Script")
        self.stop_btn.clicked.connect(self._stop)
        self.stop_btn.setEnabled(False)
        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.stop_btn)
        root.addLayout(btn_layout)

        self.status_label = QLabel("Status: AGUARDANDO INÍCIO")
        self.status_label.setStyleSheet("font-weight: bold; padding: 4px; background: #333; color: #eee;")
        root.addWidget(self.status_label)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        root.addWidget(QLabel("Log:"))
        root.addWidget(self.log_box, stretch=1)

        # [NOVO] ajustes ao vivo + persistência das configurações
        self.sens_spin.valueChanged.connect(self._push_params)
        self.deadzone_spin.valueChanged.connect(self._push_params)
        self.friction_spin.valueChanged.connect(self._push_params)
        self.invert_check.toggled.connect(self._push_params)
        self.mouse_like_check.toggled.connect(self._on_mode_changed)
        self.mouse_like_check.toggled.connect(self._push_params)
        self.mouse_pct_spin.valueChanged.connect(self._push_params)
        self.vert_pct_spin.valueChanged.connect(self._push_params)
        self.smooth_spin.valueChanged.connect(self._push_params)
        self.comp_check.toggled.connect(self._push_params)
        self.keep_angle_check.toggled.connect(self._push_params)
        self.lag_spin.valueChanged.connect(self._push_params)
        self.swap_xy_check.toggled.connect(self._push_params)
        self._settings = QSettings("MouseKbToGamepad", "RobloxController")
        self._load_settings()
        self._check_environment()
        self._on_mode_changed()
        # [NOVO v3] atualiza o monitor ~20x por segundo
        self._mon_timer = QTimer(self)
        self._mon_timer.timeout.connect(self._update_monitor)
        self._mon_timer.start(50)

    # ── [NOVO] utilidades ─────────────────────────────────────
    def _check_environment(self):
        """Avisa cedo se faltam permissões (o erro mais comum)."""
        if not os.path.exists("/dev/uinput"):
            self.log_box.append("⚠ /dev/uinput não existe. Rode: sudo modprobe uinput")
        elif not os.access("/dev/uinput", os.W_OK):
            self.log_box.append("⚠ Sem permissão de escrita em /dev/uinput "
                                "(rode como root ou ajuste a regra udev / grupo 'input').")
        if self.mouse_combo.count() == 0 or self.keyboard_combo.count() == 0:
            self.log_box.append("⚠ Nenhum mouse/teclado listado. Provável falta de permissão em "
                                "/dev/input/event* (grupo 'input' ou root).")

    def _load_settings(self):
        try:
            self.sens_spin.setValue(int(self._settings.value("sensitivity", self.sens_spin.value())))
            self.deadzone_spin.setValue(int(self._settings.value("deadzone", self.deadzone_spin.value())))
            self.friction_spin.setValue(float(self._settings.value("friction", self.friction_spin.value())))
            self.invert_check.setChecked(str(self._settings.value("invert_y", "false")).lower() == "true")
            self.mouse_like_check.setChecked(str(self._settings.value("mouse_like", "true")).lower() == "true")
            self.mouse_pct_spin.setValue(int(self._settings.value("mouse_pct", self.mouse_pct_spin.value())))
            self.vert_pct_spin.setValue(int(self._settings.value("vert_pct", self.vert_pct_spin.value())))
            self.smooth_spin.setValue(int(self._settings.value("smooth_ms", self.smooth_spin.value())))
            self.comp_check.setChecked(str(self._settings.value("compensate", "true")).lower() == "true")
            # [ORIGINAL v3] for combo, key in ((self.mouse_combo, "mouse_path"), (self.keyboard_combo, "keyboard_path")):
            # [ORIGINAL v3]     saved = self._settings.value(key, "")
            # [ORIGINAL v3]     idx = combo.findData(saved)
            # [ORIGINAL v3]     if idx >= 0:
            # [ORIGINAL v3]         combo.setCurrentIndex(idx)
            # [CORRIGIDO v3] os números /dev/input/eventN MUDAM entre boots/plugs; agora o
            # dispositivo salvo é reencontrado pelo NOME (antes podia escolher o errado).
            for combo, key, nkey in ((self.mouse_combo, "mouse_path", "mouse_name"),
                                     (self.keyboard_combo, "keyboard_path", "keyboard_name")):
                saved = self._settings.value(key, "")
                saved_name = self._settings.value(nkey, "")
                idx = self._find_device_index(combo, saved, saved_name)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            self.keep_angle_check.setChecked(str(self._settings.value("keep_angle", "true")).lower() == "true")
            self.lag_spin.setValue(int(self._settings.value("lag_ms", self.lag_spin.value())))
            self.swap_xy_check.setChecked(str(self._settings.value("swap_xy", "false")).lower() == "true")
            self.xbox_check.setChecked(str(self._settings.value("xbox", "true")).lower() == "true")
        except Exception:
            pass

    def _save_settings(self):
        try:
            self._settings.setValue("sensitivity", self.sens_spin.value())
            self._settings.setValue("deadzone", self.deadzone_spin.value())
            self._settings.setValue("friction", self.friction_spin.value())
            self._settings.setValue("invert_y", self.invert_check.isChecked())
            self._settings.setValue("mouse_like", self.mouse_like_check.isChecked())
            self._settings.setValue("mouse_pct", self.mouse_pct_spin.value())
            self._settings.setValue("vert_pct", self.vert_pct_spin.value())
            self._settings.setValue("smooth_ms", self.smooth_spin.value())
            self._settings.setValue("compensate", self.comp_check.isChecked())
            self._settings.setValue("mouse_path", self.mouse_combo.currentData() or "")
            self._settings.setValue("keyboard_path", self.keyboard_combo.currentData() or "")
            self._settings.setValue("mouse_name", self.mouse_combo.currentText().rsplit(" [", 1)[0])
            self._settings.setValue("keyboard_name", self.keyboard_combo.currentText().rsplit(" [", 1)[0])
            self._settings.setValue("keep_angle", self.keep_angle_check.isChecked())
            self._settings.setValue("lag_ms", self.lag_spin.value())
            self._settings.setValue("swap_xy", self.swap_xy_check.isChecked())
            self._settings.setValue("xbox", self.xbox_check.isChecked())
        except Exception:
            pass

    def _update_monitor(self):
        """[NOVO v3] mostra botões/eixos que estão sendo enviados ao jogo."""
        try:
            w = self.worker
            if not w or not w.running or not w._ui:
                self.monitor_label.setText("(script parado)")
                return
            sn = w.snapshot()
            btns = "  ".join(f"{n}{'●' if on else '○'}" for n, on in sn["buttons"])
            self.monitor_label.setText(
                f"{btns}\n"
                f"LT {sn['lt']:3d}  RT {sn['rt']:3d}   D-Pad ({sn['hx']:+d},{sn['hy']:+d})\n"
                f"LS ({sn['lx']:+6d},{sn['ly']:+6d})   RS ({sn['rx']:+6d},{sn['ry']:+6d})\n"
                f"Eventos recebidos -> mouse: {sn['n_mouse']} | teclado: {sn['n_key']} | "
                f"{'CONTROLE ATIVO' if sn['grabbed'] else 'inativo (F6)'}"
            )
        except Exception:
            pass

    def _on_mode_changed(self, *_):
        """[NOVO] habilita só os controles do modo escolhido."""
        real = self.mouse_like_check.isChecked()
        for w in (self.mouse_pct_spin, self.vert_pct_spin, self.smooth_spin, self.comp_check,
                  self.keep_angle_check, self.lag_spin):
            w.setEnabled(real)
        for w in (self.sens_spin, self.friction_spin):
            w.setEnabled(not real)

    def _push_params(self, *_):
        if self.worker:
            self.worker.update_params(
                self.sens_spin.value(),
                self.deadzone_spin.value(),
                self.friction_spin.value(),
                self.invert_check.isChecked(),
                mouse_like=self.mouse_like_check.isChecked(),
                mouse_pct=self.mouse_pct_spin.value(),
                vert_pct=self.vert_pct_spin.value(),
                smooth_ms=self.smooth_spin.value(),
                compensate=self.comp_check.isChecked(),
                keep_angle=self.keep_angle_check.isChecked(),
                max_lag_ms=self.lag_spin.value(),
                swap_xy=self.swap_xy_check.isChecked(),
            )

    def _find_device_index(self, combo, saved_path, saved_name):
        """[NOVO v3] acha o dispositivo salvo pelo nome (e, se houver vários, pelo caminho)."""
        if saved_name:
            matches = [i for i in range(combo.count())
                       if combo.itemText(i).rsplit(" [", 1)[0] == saved_name]
            for i in matches:
                if combo.itemData(i) == saved_path:
                    return i
            return matches[0] if matches else -1
        return combo.findData(saved_path)

    def _pick_default(self, combo, bad_words):
        """[NOVO v3] sem seleção salva: escolhe o dispositivo com menos 'palavras suspeitas'."""
        best, best_score = 0, None
        for i in range(combo.count()):
            txt = combo.itemText(i).lower()
            score = sum(1 for w in bad_words if w in txt)
            if best_score is None or score < best_score:
                best, best_score = i, score
        if combo.count():
            combo.setCurrentIndex(best)

    def _populate_devices(self):
        # [NOVO] preserva a seleção atual ao atualizar a lista
        prev_mouse = self.mouse_combo.currentData()
        prev_kb = self.keyboard_combo.currentData()
        self.mouse_combo.clear()
        self.keyboard_combo.clear()
        # [NOVO] teclas que um teclado "de verdade" precisa ter pra este script
        required_keys = {
            e.KEY_W, e.KEY_A, e.KEY_S, e.KEY_D, e.KEY_Q, e.KEY_E, e.KEY_R,
            e.KEY_F, e.KEY_T, e.KEY_V, e.KEY_C, e.KEY_SPACE, e.KEY_LEFTSHIFT,
            e.KEY_1, e.KEY_2, e.KEY_F6,
        }
        # [ORIGINAL v3] for path in sorted(list_devices()):
        # [CORRIGIDO v3] ordenação natural (event2 antes de event10)
        for path in sorted(list_devices(), key=lambda p: [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', p)]):
            try:
                dev = InputDevice(path)
            except Exception:
                continue
            try:
                # [NOVO] não lista o próprio gamepad virtual
                if dev.name in VIRTUAL_NAMES:
                    continue
                caps = dev.capabilities()
                label = f"{dev.name} [{dev.path}]"
                # [ORIGINAL v3] if e.REL_X in caps.get(e.EV_REL, []):
                # [ORIGINAL v3]     self.mouse_combo.addItem(label, dev.path)
                # [ORIGINAL v3] keys = caps.get(e.EV_KEY, [])
                keys = caps.get(e.EV_KEY, [])
                rel = caps.get(e.EV_REL, [])
                # [CORRIGIDO v3] mouse de verdade: REL_X + REL_Y + botão esquerdo
                if e.REL_X in rel and e.REL_Y in rel and e.BTN_LEFT in keys:
                    self.mouse_combo.addItem(label, dev.path)
                # [ORIGINAL] letter_keys = [k for k in keys if (e.KEY_A <= k <= e.KEY_Z) or (e.KEY_0 <= k <= e.KEY_9)]
                # [ORIGINAL] if len(letter_keys) > 10:
                # [ORIGINAL]     self.keyboard_combo.addItem(label, dev.path)
                # [CORRIGIDO] KEY_A..KEY_Z e KEY_0..KEY_9 NÃO são faixas contínuas de letras/números
                # em evdev (a segunda faixa era vazia). Agora checa as teclas realmente usadas.
                if required_keys.issubset(set(keys)):
                    self.keyboard_combo.addItem(label, dev.path)
            finally:
                # [NOVO] evita vazar descritores de arquivo a cada atualização
                try: dev.close()
                except Exception: pass
        for combo, prev in ((self.mouse_combo, prev_mouse), (self.keyboard_combo, prev_kb)):
            if prev:
                idx = combo.findData(prev)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
        # [NOVO v3] sem seleção anterior: prefere o dispositivo mais provável
        if not prev_mouse:
            self._pick_default(self.mouse_combo, MOUSE_BAD_WORDS)
        if not prev_kb:
            self._pick_default(self.keyboard_combo, KEYBOARD_BAD_WORDS)

    def _populate_keymap_table(self):
        mapping = [
            ("Mouse (Movimento)", "Câmera (Analógico Direito)"),
            ("Clique Esq", "R2 (Atirar)"),
            ("Clique Dir", "A (Escopo)"),
            ("W, A, S, D", "Movimento (Analógico Esquerdo)"),
            ("Q", "X (Deslizar)"),
            ("R", "B (Recarregar)"),
            ("E", "Triângulo (Habilidade 2)"),
            ("F", "L1 (Habilidade 1)"),
            ("T", "R1 (Swap Weapon)"),
            ("Espaço", "A (Pulo)"),
            # [NOVO] linhas que faltavam na tabela
            ("V", "R3"),
            ("Shift Esq", "L3"),
            ("C", "L2"),
            ("1", "Start"),
            ("2", "Select"),
            ("Setas", "D-Pad"),
            ("F6", "Ativar / Desativar"),
        ]
        self.map_table.setRowCount(0)
        for k, b in mapping:
            row = self.map_table.rowCount()
            self.map_table.insertRow(row)
            self.map_table.setItem(row, 0, QTableWidgetItem(k))
            self.map_table.setItem(row, 1, QTableWidgetItem(b))
            for col in (0, 1):
                item = self.map_table.item(row, col)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

    def _start(self):
        mouse_path = self.mouse_combo.currentData()
        keyboard_path = self.keyboard_combo.currentData()
        if not mouse_path or not keyboard_path:
            QMessageBox.warning(self, "Aviso", "Selecione mouse e teclado.")
            return
        # [NOVO] mesmo dispositivo nos dois campos causaria falha no grab
        if mouse_path == keyboard_path:
            QMessageBox.warning(self, "Aviso", "Mouse e teclado não podem ser o mesmo dispositivo.")
            return
        # [CORRIGIDO] evita iniciar duas threads
        if self.worker is not None:
            return

        self._save_settings()

        # [ORIGINAL] self.worker = WorkerThread(
        # [ORIGINAL]     mouse_path=mouse_path,
        # [ORIGINAL]     keyboard_path=keyboard_path,
        # [ORIGINAL]     sensitivity=self.sens_spin.value(),
        # [ORIGINAL]     deadzone=self.deadzone_spin.value()
        # [ORIGINAL] )
        self.worker = WorkerThread(
            mouse_path=mouse_path,
            keyboard_path=keyboard_path,
            sensitivity=self.sens_spin.value(),
            deadzone=self.deadzone_spin.value(),
            friction=self.friction_spin.value(),
            invert_y=self.invert_check.isChecked(),
            mouse_like=self.mouse_like_check.isChecked(),
            mouse_pct=self.mouse_pct_spin.value(),
            vert_pct=self.vert_pct_spin.value(),
            smooth_ms=self.smooth_spin.value(),
            compensate=self.comp_check.isChecked(),
            keep_angle=self.keep_angle_check.isChecked(),
            max_lag_ms=self.lag_spin.value(),
            swap_xy=self.swap_xy_check.isChecked(),
            emulate_xbox=self.xbox_check.isChecked(),
        )
        self.worker.log_signal.connect(self._on_log)
        self.worker.status_signal.connect(self._on_status)
        self.worker.stopped_signal.connect(self._on_stopped)
        # [NOVO] só descarta a referência quando a thread REALMENTE terminou
        self.worker.finished.connect(self._on_thread_finished)
        self.worker.start()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.log_box.append("Script iniciado. Aperte F6 para ativar.")

    def _stop(self):
        if self.worker:
            self.worker.stop()

    def _on_log(self, msg):
        self.log_box.append(msg)

    def _on_status(self, status):
        self.status_label.setText(f"Status: {status}")
        # [ORIGINAL] if "ATIVO" in status:
        # [ORIGINAL] elif "INATIVO" in status:
        # [CORRIGIDO] "INATIVO" contém a palavra "ATIVO", então o 1º if SEMPRE vencia
        # e o status ficava verde mesmo desativado. Agora testa INATIVO primeiro.
        if "INATIVO" in status:
            self.status_label.setStyleSheet("font-weight: bold; padding: 4px; background: #333; color: #eee;")
        elif "ATIVO" in status:
            self.status_label.setStyleSheet("font-weight: bold; padding: 4px; background: #225; color: #7f7;")

    def _on_stopped(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        # [ORIGINAL] self.worker = None
        # [CORRIGIDO] zerar aqui destruía o QThread enquanto ele ainda estava terminando
        # (risco de crash "QThread: Destroyed while thread is still running").
        # A referência agora é solta em _on_thread_finished.

    def _on_thread_finished(self):
        if self.worker:
            self.worker.deleteLater()
        self.worker = None

    def _shutdown(self):
        """[NOVO] encerramento limpo (fechar janela, Ctrl+C, aboutToQuit)."""
        self._save_settings()
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(2000)

    def closeEvent(self, event):
        # [ORIGINAL] if self.worker and self.worker.isRunning():
        # [ORIGINAL]     self.worker.stop()
        # [ORIGINAL]     self.worker.wait(2000)
        self._shutdown()
        event.accept()

def main():
    # [NOVO] Ctrl+C no terminal fecha o app normalmente
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    app.aboutToQuit.connect(win._shutdown)
    sys.exit(app.exec())

if __name__ == "__main__":
    main()

# =====================================================================
# PERMISSÕES (Linux) — rode uma vez se der "Permission denied":
#   sudo modprobe uinput
#   echo 'KERNEL=="uinput", GROUP="input", MODE="0660"' | sudo tee /etc/udev/rules.d/99-uinput.rules
#   sudo usermod -aG input $USER
#   sudo udevadm control --reload-rules && sudo udevadm trigger
#   (depois, faça logout/login)
# Ou simplesmente: sudo python mouse_keyboard_to_gamepad_gui.py
# =====================================================================
