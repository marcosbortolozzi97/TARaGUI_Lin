
# core/ensayo_sesion.py
"""
Orquestador del ciclo de vida de un ensayo TAR.
 
Responsabilidades:
    - Controlar start / stop del ensayo.
    - Conectar la fuente de datos (serie o replay) con el procesador.
    - Enviar / recibir comandos al hardware (START, STOP, GET_CONF, CHA_H, CHB_H).
    - Guardar datos incrementalmente durante LIVE y el archivo final al terminar.
    - Exponer los registros procesados y el log de configuración a la GUI.
 
FLUJO DE INICIO (modo LIVE):
    1. apply_hysteresis()      → envía CHA_H + CHB_H (sin hilo)
    2. get_conf_pre_start()    → envía GET_CONF, lee respuesta {LOG}
                                 sincrónicamente antes de arrancar el hilo.
                                 Guarda el texto completo en _last_control_msg.
    3. _fuente._start()        → arranca hilo de lectura
    4. send_command(START)     → TAR empieza a emitir frames binarios
 
FLUJO DE CIERRE (modo LIVE):
    1. send_command(STOP)      → TAR deja de emitir
                                 → _stopping=True en hilo serie
    2. stop_async()            → señala al worker que pare cuando vacíe la cola
    3. Hilo serie sigue leyendo últimos bytes y encolando
    4. Worker vacía cola       → _running=False (worker)
    5. Hilo serie detecta      → puerto inactivo AND worker terminó → cierra
    6. tick() detecta          → _fuente.is_running()==False → guarda y cierra
 
La GUI no habla directamente con el hardware ni con el procesador:
todo pasa por esta clase.
"""
 
import time
import csv
import struct
from pathlib import Path
from typing import Callable, Optional, List, Dict
from enum import Enum, auto
import re
import logging
log = logging.getLogger(__name__)
 
from .procesar_datos_live   import ProcesaDatosLive
from .procesar_datos_replay import ProcesaDatosReplay
 
 
# =============================================================
# ENUMS Y COMANDOS
# =============================================================
class TARMode(Enum):
    LIVE   = auto()
    REPLAY = auto()
 
 
class TARCommands:
    START    = b'\x25\x01'
    STOP     = b'\x25\x02'
    GET_CONF = b'\x25\xF0'
 
 
# =============================================================
# CLASE PRINCIPAL
# =============================================================
class EnsayoSession:
 
    def __init__(self, fuente, mode: TARMode, base_dir: Optional[Path] = None):
        self._fuente    = fuente
        self._mode      = mode
        self._live_mode = (mode is TARMode.LIVE)
 
        if self._live_mode:
            self._procesador = ProcesaDatosLive()
            self._procesador.start_async()
        else:
            self._procesador = ProcesaDatosReplay()
 
        self._running         = False
        self._closing_pending = False
        self._stop_sent       = False
 
        # Estado GET_CONF
        self._expecting_conf   = False
        self._conf_requested   = False
        self._conf_received    = False
        self._conf_buffer      = ""
        self._conf_deadline:    Optional[float] = None
        self._last_control_msg: Optional[str]   = None
        self._last_conf_struct: Optional[dict]  = None
        self._applied_params:   Optional[dict]  = None
        self._on_error_callback: Optional[Callable[[str], None]] = None
 
        # Guardado incremental
        self._save_interval_s = 15
        self._next_save_t     = 0.0
        self._last_saved_idx  = 0
 
        # Directorios
        self.base_dir   = base_dir or (Path.home() / "Documents" / "Ensayos_TAR")
        self.ensayo_dir: Optional[Path] = None
        self.csv_dir:    Optional[Path] = None
        self.bin_dir:    Optional[Path] = None
 
 
    # =============================================================
    # ESTADO
    # =============================================================
    def is_running(self) -> bool:
        return self._running
 
    def has_finished(self) -> bool:
        """True cuando el ensayo terminó Y el guardado final ya se completó."""
        return (not self._running) and (not self._closing_pending)
    
    # Método para registrar el callback desde MainWindow:
    def set_error_callback(self, fn: Callable[[str], None]):
        self._on_error_callback = fn
    
    def _finalizar_ensayo_por_fuerza(self):
        """Finaliza el ensayo ante un error crítico de hardware."""
        # Intentamos guardar lo que se tenga hasta el momento
        try:
            self._guardar_final() 
        except Exception as e:
            log.critical("No se pudo realizar el guardado final tras el error: %s", e)
        
        # Limpiamos estados
        self._running = False
        self._closing_pending = False
        
        # Aseguramos que la fuente se limpie (liberar puerto)
        self._fuente.close()
        
        # Aquí emitimos una señal a la GUI para avisar al usuario:
        if self._on_error_callback:
            self._on_error_callback("Conexión perdida con el hardware")
 
 
    # =============================================================
    # CONTROL DEL ENSAYO
    # =============================================================
    def start(self):
        """
        Inicia un ensayo nuevo.
 
        En modo LIVE el orden es crítico:
            1. get_conf_pre_start() — lee el log del TAR antes de arrancar el hilo
            2. _fuente._start()     — arranca el hilo, pasando worker_running_fn
                                      para coordinar el cierre limpio
            3. send START           — TAR empieza a emitir frames binarios
        """
        if self._running:
            return
 
        self._procesador.reset()
        self._conf_received   = False
        self._closing_pending = False
        self._expecting_conf  = False
        self._conf_buffer     = ""
        self._stop_sent       = False
        self._conf_requested  = False
 
        self._crear_carpetas_ensayo()
 
        if self._live_mode:
            self._last_saved_idx = 0
            self._next_save_t    = time.time() + self._save_interval_s
 
            # Paso 1: GET_CONF sincrónico ANTES de arrancar el hilo
            self.get_conf_pre_start()
 
        # Paso 2: arrancar hilo — en LIVE pasa worker_running_fn para
        # que el hilo serie sepa cuándo el worker terminó de procesar
        self._fuente._start(
            self._procesador.feed,
            self._on_control_bytes,
            self._procesador.is_async_running if self._live_mode else None
        )
 
        # Paso 3: START en LIVE
        if self._live_mode:
            self._fuente.send_command(TARCommands.START)
 
        self._running = True
 
 
    def tick(self):
        # ── Guardado incremental ─────────────────────────────────────
        if self._running and self._live_mode:
            now = time.time()
            if now >= self._next_save_t:
                self._guardar_incremental()
                self._next_save_t = now + self._save_interval_s
        
        # ── Detección de Falla Inestperada (El cable se soltó o hubo error) ────────────────────────────
        if self._running and self._live_mode and not self._closing_pending:
            if not self._fuente.is_running():
                log.error("¡Detección de fallo en fuente de datos! Finalizando ensayo por seguridad.")
                self._finalizar_ensayo_por_fuerza()
                return
 
        # ── Cierre automático REPLAY ─────────────────────────────────
        if self._running and not self._live_mode:
            if not self._fuente.is_running():
                self._guardar_final()
                self._running = False
                return
 
        # ── Cierre LIVE ──────────────────────────────────────────────
        if self._closing_pending:
            # El hilo serie cierra solo cuando:
            #   puerto inactivo AND worker terminó (coordinado via worker_running_fn)
            # Cuando el hilo serie cierra, _fuente.is_running() pasa a False.
            # tick() lo detecta y ejecuta el guardado final.
            if not self._fuente.is_running():
                self._guardar_conf_final()
                self._guardar_final()
                self._closing_pending = False
                self._running         = False
                return
 
            # Timeout de seguridad: si el hilo serie no cerró en 2s extra, forzar
            conf_timeout = (
                self._conf_deadline is not None
                and time.time() >= self._conf_deadline
            )
            if conf_timeout:
                log.warning("Timeout de cierre — forzando guardado final")
                self._guardar_conf_final()
                self._guardar_final()
                self._fuente._stop()
                self._closing_pending = False
                self._running         = False
 
 
    def stop(self):
        """
        Inicia el cierre de un ensayo LIVE.
 
        ORDEN CRÍTICO:
            1. send_command(STOP) — TAR deja de emitir, _stopping=True en hilo serie
            2. stop_async()       — señala al worker que pare cuando vacíe la cola
                                    El hilo serie sigue leyendo hasta que:
                                    puerto inactivo AND worker terminó
            3. _closing_pending   — tick() espera que el hilo serie cierre
 
        El worker se señala DESPUÉS del STOP para que pueda procesar
        todos los bytes que lleguen entre el STOP y el silencio del puerto.
        """
        if not self._running or self._stop_sent:
            return
 
        self._stop_sent = True
 
        if self._live_mode:
            # 1. STOP al TAR → _stopping=True en hilo serie
            self._fuente.send_command(TARCommands.STOP)
 
            # 2. Señalar al worker que pare cuando vacíe la cola
            if hasattr(self._procesador, 'stop_async'):
                self._procesador.stop_async()

            self._closing_pending = True
            # Timeout de seguridad: inactivity_timeout del hilo + margen
            self._conf_deadline = time.time() + self._fuente.inactivity_timeout + 3.0


    # =============================================================
    # GET_CONF
    # =============================================================
    def get_conf_pre_start(self) -> bool:
        """
        Solicita GET_CONF y lee la respuesta de forma sincrónica y bloqueante
        ANTES de arrancar el hilo de lectura.
 
        El TAR responde al GET_CONF antes del START con un bloque ASCII
        delimitado por '{' y '}'. Se lee directamente del puerto con
        SerialSource.read_raw(), sin depender del hilo ni de callbacks.
 
        Guarda el texto completo en _last_control_msg y el struct parseado
        en _last_conf_struct. Retorna True si recibió respuesta válida.
        """
        if not self._live_mode:
            return False
        if not hasattr(self._fuente, "send_command"):
            return False

        self._last_control_msg = None
        self._last_conf_struct = None
        self._conf_buffer      = ""
        self._conf_received    = False
        self._conf_requested   = True
 
        self._fuente.send_command(TARCommands.GET_CONF)
        log.info("GET_CONF pre-start enviado — leyendo respuesta...")
 
        raw = self._fuente.read_raw(timeout_s=2.0)
 
        if not raw:
            log.warning("GET_CONF pre-start: no se recibió respuesta")
            return False
 
        texto = raw.decode("ascii", errors="ignore")
        m = re.search(r"\{(.*?)\}", texto, re.DOTALL)
        if not m:
            log.warning("GET_CONF pre-start: no se encontró bloque { } en: %r", texto)
            return False
 
        contenido = m.group(1)
        self._last_control_msg = contenido
        self._conf_buffer      = contenido
 
        if self._parse_conf_text(contenido):
            self._conf_received = True
            log.info("GET_CONF pre-start OK:\n%s", contenido.strip())
            return True
 
        log.warning("GET_CONF pre-start: respuesta recibida pero no parseada: %r", contenido)
        return False
 
 
    def get_conf(self) -> None:
        """Solicita GET_CONF de forma manual desde la GUI (solo fuera de ensayo)."""
        if not self._live_mode:
            raise RuntimeError("GET_CONF solo es válido en modo LIVE")
        if self._running:
            raise RuntimeError("No se puede consultar durante un ensayo en curso")
        if not hasattr(self._fuente, "send_command"):
            raise RuntimeError("La fuente no permite envío de comandos")
 
        self._last_control_msg = None
        self._last_conf_struct = None
        self._conf_buffer      = ""
        self._expecting_conf   = True
        self._fuente.send_command(TARCommands.GET_CONF)
 
 
    def _on_control_bytes(self, data: bytes):
        """Callback del hilo cuando recibe un bloque { } en modo COMANDO."""
        if not self._expecting_conf:
            return
 
        text = data.decode("ascii", errors="ignore")
        self._conf_buffer += text
 
        if self._parse_conf_text(self._conf_buffer):
            self._last_control_msg = self._conf_buffer
            self._expecting_conf   = False
            self._conf_received    = True
 
 
    def _parse_conf_text(self, text: str) -> bool:
        """
        Extrae umbrales CHA/CHB del texto ASCII del TAR.
        Retorna True solo si ambos canales fueron parseados.
        """
        conf = {}
        try:
            m = re.search(r"CHA.*?\(\s*(\d+)\s*;\s*(\d+)\s*\)",
                          text, re.IGNORECASE | re.DOTALL)
            if m:
                conf["CHA"] = {"min": int(m.group(1)), "max": int(m.group(2))}
 
            m = re.search(r"CHB.*?\(\s*(\d+)\s*;\s*(\d+)\s*\)",
                          text, re.IGNORECASE | re.DOTALL)
            if m:
                conf["CHB"] = {"min": int(m.group(1)), "max": int(m.group(2))}
 
            self._last_conf_struct = conf if conf else None
            return len(conf) == 2
 
        except Exception as e:
            log.warning("Error interpretando configuración TAR", exc_info=e)
            return False
 
 
    def _parse_conf_from_file(self, text: str) -> Optional[dict]:
        """Parsea test-config.txt (formato: CHA min max / CHB min max)."""
        try:
            conf = {}
            for line in text.splitlines():
                parts = line.strip().split()
                if len(parts) != 3:
                    continue
                key, vmin, vmax = parts
                if key in ("CHA", "CHB"):
                    conf[key] = {"min": int(vmin), "max": int(vmax)}
            return conf if len(conf) == 2 else None
        except Exception:
            return None
 
 
    def load_conf_from_text(self, txt: str) -> bool:
        conf = self._parse_conf_from_file(txt)
        self._last_conf_struct = conf
        return conf is not None
 
    def clear_conf(self):
        self._last_control_msg = None
        self._last_conf_struct = None
 
 
    # =============================================================
    # CONSULTAS PARA LA GUI
    # =============================================================
    def get_last_conf_struct(self) -> Optional[dict]:
        return self._last_conf_struct
 
    def get_last_control_msg(self) -> Optional[str]:
        """Texto completo del log TAR — mostrado en popup al finalizar ensayo LIVE."""
        return self._last_control_msg
 
 
    # =============================================================
    # HISTÉRESIS
    # =============================================================
    def apply_hysteresis(self, params: dict):
        """
        Empaqueta y envía CHA_H + CHB_H al TAR antes de start().
 
        Formato (alineado con serial_port.c):
            param = (max << 16) | min → uint32 Big-Endian
            [0x25][cmd][B3][B2][B1][B0] = 6 bytes
            700ms entre comandos
        """
        if not self._live_mode:
            raise RuntimeError("No se pueden aplicar parámetros en modo REPLAY")
        if not hasattr(self._fuente, "send_command"):
            raise RuntimeError("La fuente no permite envío de comandos")
 
        try:
            A_min = int(params["umbral_cha_min"])
            A_max = int(params["umbral_cha_max"])
            B_min = int(params["umbral_chb_min"])
            B_max = int(params["umbral_chb_max"])
        except KeyError as e:
            raise ValueError(f"Parámetro faltante: {e}")
 
        if not (0 <= A_min < A_max <= 8191):
            raise ValueError("Valores inválidos para CHA")
        if not (0 <= B_min < B_max <= 8191):
            raise ValueError("Valores inválidos para CHB")
 
        param_cha = (A_max << 16) | A_min
        param_chb = (B_max << 16) | B_min
 
        cmd_cha = struct.pack(">BBI", 0x25, 0xA0, param_cha)
        cmd_chb = struct.pack(">BBI", 0x25, 0xB0, param_chb)
 
        log.info("Enviando CHA_H: min=%d max=%d (param=0x%08X)", A_min, A_max, param_cha)
        self._fuente.send_command(cmd_cha)
        time.sleep(0.7)
 
        log.info("Enviando CHB_H: min=%d max=%d (param=0x%08X)", B_min, B_max, param_chb)
        self._fuente.send_command(cmd_chb)
        time.sleep(0.7)
 
        self._applied_params = {
            "CHA": {"min": A_min, "max": A_max},
            "CHB": {"min": B_min, "max": B_max},
        }
 
 
    # =============================================================
    # DATOS PARA LA GUI
    # =============================================================
    def get_registros(self) -> List[Dict]:
        return self._procesador.registros
 
    def get_eventos_desde(self, idx: int) -> List[Dict]:
        regs = self._procesador.registros
        if idx < 0 or idx >= len(regs):
            return []
        return regs[idx:]
 
 
    # =============================================================
    # CARPETAS Y GUARDADO
    # =============================================================
    def _crear_carpetas_ensayo(self):
        timestamp = time.strftime("%d%m%Y-%H%M%S")
        self.ensayo_dir = self.base_dir / f"Ensayo_{timestamp}"
        self.csv_dir    = self.ensayo_dir / "csv"
 
        if self._live_mode:
            self.bin_dir = self.ensayo_dir / "bin"
            self.bin_dir.mkdir(parents=True, exist_ok=True)
 
        self.csv_dir.mkdir(parents=True, exist_ok=True)
 
 
    def _guardar_incremental(self):
        nuevos = self._procesador.registros[self._last_saved_idx:]
        if not nuevos:
            return
 
        ts = time.strftime("%d%m%Y-%H%M%S")
        por_canal = {0: [], 1: []}
        for r in nuevos:
            por_canal[r["chan"]].append(r)
 
        for ch, regs in por_canal.items():
            if not regs:
                continue
            fname = self.csv_dir / f"{ts}_test-ch{'A' if ch == 0 else 'B'}.csv"
            with open(fname, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["Index", "Timestamp", "Value"])
                for i, r in enumerate(regs):
                    w.writerow([i, int(r["tstamp"]), int(r["ampl"])])
 
        with open(self.bin_dir / f"{ts}_test-raw.bin", "wb") as f:
            for r in nuevos:
                f.write(r["_raw"])
 
        self._last_saved_idx += len(nuevos)
 
 
    def _guardar_final(self):
        if not self._procesador.registros:
            return
 
        ts = time.strftime("%d%m%Y-%H%M%S")
        por_canal = {0: [], 1: []}
        for r in self._procesador.registros:
            ch = r.get("chan")
            if ch in por_canal:
                por_canal[ch].append(r)
 
        for ch, regs in por_canal.items():
            if not regs:
                continue
            canal_nombre = "A" if ch == 0 else "B"
            fname = self.csv_dir / f"{ts}_test-ch{canal_nombre}.csv"
            with open(fname, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["Index", "Timestamp", "Value"])
                for i, r in enumerate(regs):
                    w.writerow([i, int(r["tstamp"]), int(r["ampl"])])
 
        if self._live_mode:
            bin_path = self.bin_dir / f"{ts}_test-raw.bin"
            with open(bin_path, "wb") as f:
                for r in self._procesador.registros:
                    f.write(r["_raw"])
 
 
    def _guardar_conf_final(self):
        """
        Guarda test-config.txt y test-log.txt.
        Prioridad:
            1. _last_conf_struct (respuesta real GET_CONF pre-start)
            2. _applied_params  (backup)
        """
        if not self._live_mode:
            return

        conf = self._last_conf_struct or self._applied_params
        if conf is None:
            return

        conf_path = self.bin_dir / "test-config.txt"
        with open(conf_path, "w", encoding="utf-8") as f:
            f.write(f"CHA {conf['CHA']['min']} {conf['CHA']['max']}\n")
            f.write(f"CHB {conf['CHB']['min']} {conf['CHB']['max']}\n")

        if self._last_control_msg:
            log_path = self.bin_dir / "test-log.txt"
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(self._last_control_msg)
            log.info("Log TAR guardado en %s", log_path)
