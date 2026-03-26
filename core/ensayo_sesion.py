
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

La GUI no habla directamente con el hardware ni con el procesador:
todo pasa por esta clase.
"""

import time
import csv
import struct
from pathlib import Path
from typing import Optional, List, Dict
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
    """
    Comandos binarios que se envían al TAR.
    Formato siempre: 0x25 | BYTE_DE_COMANDO [| datos opcionales]
    """
    START    = b'\x25\x01'    # Inicia la adquisición de pulsos
    STOP     = b'\x25\x02'    # Detiene la adquisición
    GET_CONF = b'\x25\xF0'    # Solicita que el TAR responda con su configuración actual


# =============================================================
# CLASE PRINCIPAL
# =============================================================
class EnsayoSession:

    def __init__(self, fuente, mode: TARMode, base_dir: Optional[Path] = None):
        # ── Fuente y modo ────────────────────────────────────────────
        self._fuente    = fuente                    # SerialSource o ReplayBinSource
        self._mode      = mode
        self._live_mode = (mode is TARMode.LIVE)

        # ── Procesador según modo ────────────────────────────────────
        if self._live_mode:
            self._procesador = ProcesaDatosLive()
            self._procesador.start_async()
        else:
            self._procesador = ProcesaDatosReplay()

        # ── Estado del ensayo ────────────────────────────────────────
        self._running         = False
        self._closing_pending = False   # True entre STOP y guardado final
        self._stop_sent       = False   # Evita enviar STOP doble

        # Estado GET_CONF
        self._expecting_conf  = False
        self._conf_requested  = False
        self._conf_received   = False
        self._conf_buffer     = ""
        self._conf_deadline:   Optional[float] = None
        self._last_control_msg: Optional[str]  = None   # Texto completo del log TAR
        self._last_conf_struct: Optional[dict] = None   # {CHA:{min,max}, CHB:{min,max}}
        self._applied_params:   Optional[dict] = None   # Backup si GET_CONF falla
 
        # Guardado incremental
        self._save_interval_s = 15
        self._next_save_t     = 0.0
        self._last_saved_idx  = 0
 
        # ── Directorios de salida ────────────────────────────────────
        # Se crean al llamar start(). Estructura:
        #   Ensayos_TAR/
        #     Ensayo_DDMMAAAA-HHMMSS/
        #       csv/    archivos CSV por canal
        #       bin/    archivo binario crudo + config (solo LIVE)
        self.base_dir  = base_dir or (Path.home() / "Documents" / "Ensayos_TAR")
        self.ensayo_dir: Optional[Path] = None
        self.csv_dir:   Optional[Path] = None
        self.bin_dir:   Optional[Path] = None


    # =============================================================
    # ESTADO
    # =============================================================
    def is_running(self) -> bool:
        return self._running

    def has_finished(self) -> bool:
        """True cuando el ensayo terminó Y el guardado final ya se completó."""
        return (not self._running) and (not self._closing_pending)


    # =============================================================
    # CONTROL DEL ENSAYO
    # =============================================================
    def start(self):
        """
        Inicia un ensayo nuevo.
 
        En modo LIVE el orden es crítico:
            1. get_conf_pre_start() — lee el log del TAR antes de arrancar el hilo
            2. _fuente._start()     — arranca el hilo de lectura
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

        # Inicializar guardado incremental (solo se utiliza en LIVE)
        if self._live_mode:
            self._last_saved_idx = 0
            self._next_save_t    = time.time() + self._save_interval_s

            # Paso 1: GET_CONF sincrónico ANTES de arrancar el hilo.
            # El TAR responde al GET_CONF antes del START; el texto {LOG}
            # llega por el puerto sin que el hilo esté activo.
            self.get_conf_pre_start()
 
        # Paso 2: arrancar hilo (en REPLAY también)
        self._fuente._start(
            self._procesador.feed,
            self._on_control_bytes
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

        # ── Cierre automático de REPLAY ──────────────────────────────
        # ReplayBinSource pone _running=False cuando termina de leer el archivo.
        if self._running and not self._live_mode:
            if not self._fuente.is_running():
                self._guardar_final()
                self._running = False
                return

        # ── Cierre LIVE ────────────
        if self._closing_pending:
            conf_ok      = self._conf_received
            conf_timeout = (self._conf_deadline is not None
                            and time.time() >= self._conf_deadline)

            if conf_ok or conf_timeout:
                self._guardar_conf_final()  # Guarda test-config.txt
                self._guardar_final()       # Guarda CSV + BIN completos
                self._fuente._stop()        # Detiene el hilo de lectura serie
                self._closing_pending = False
                self._running         = False


    def stop(self):
        """
        Inicia el cierre de un ensayo LIVE.
        Ya no solicita GET_CONF al finalizar porque se hizo antes del START.
        El cierre espera el timeout de _conf_deadline por si hubiera
        algún dato pendiente, pero _conf_received ya debería ser True.
        """
        if not self._running or self._stop_sent:
            return

        self._stop_sent = True

        if self._live_mode:
            if hasattr(self._procesador, 'stop_async'):
                self._procesador.stop_async()

            self._fuente.send_command(TARCommands.STOP)
            self._closing_pending = True
            # Si get_conf_pre_start() recibió el log, conf_received=True
            # y tick() cierra de inmediato. Si no, espera 2s de timeout.
            self._conf_deadline = time.time() + 2.0   # 2 seg de timeout


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
 
        # Limpiar estado previo
        self._last_control_msg = None
        self._last_conf_struct = None
        self._conf_buffer      = ""
        self._conf_received    = False
        self._conf_requested   = True
 
        # Enviar GET_CONF
        self._fuente.send_command(TARCommands.GET_CONF)
        log.info("GET_CONF pre-start enviado — leyendo respuesta...")
 
        # Leer respuesta sincrónicamente (el hilo aún no está corriendo)
        raw = self._fuente.read_raw(timeout_s=2.0)
 
        if not raw:
            log.warning("GET_CONF pre-start: no se recibió respuesta")
            return False
 
        # Extraer bloque entre '{' y '}'
        texto = raw.decode("ascii", errors="ignore")
        m = re.search(r"\{(.*?)\}", texto, re.DOTALL)
        if not m:
            log.warning("GET_CONF pre-start: no se encontró bloque { } en: %r", texto)
            return False
 
        contenido = m.group(1)
        self._last_control_msg = contenido   # Texto completo del log TAR
        self._conf_buffer      = contenido
 
        if self._parse_conf_text(contenido):
            self._conf_received = True
            log.info("GET_CONF pre-start OK:\n%s", contenido.strip())
            return True
 
        log.warning("GET_CONF pre-start: respuesta recibida pero no parseada: %r", contenido)
        return False
 
 
    def get_conf(self) -> None:
        """
        Solicita GET_CONF de forma manual desde la GUI.
        Solo válido en LIVE y cuando NO hay ensayo en curso.
        """
        if not self._live_mode:
            raise RuntimeError("GET_CONF solo es válido en modo LIVE")
        if self._running:
            raise RuntimeError("No se puede consultar durante un ensayo en curso")
        if not hasattr(self._fuente, "send_command"):
            raise RuntimeError("La fuente no permite envío de comandos")

        self._last_control_msg  = None
        self._last_conf_struct  = None
        self._conf_buffer       = ""
        self._expecting_conf    = True
        self._fuente.send_command(TARCommands.GET_CONF)


    def _on_control_bytes(self, data: bytes):
        """
        Callback del hilo de lectura cuando recibe un bloque { } en modo COMANDO.
        Solo activo si _expecting_conf=True (consulta manual desde GUI).
        """
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
        Extrae los umbrales de histéresis del texto ASCII del TAR.
        Formato esperado (dentro del bloque { }):
            CHA: histéresis (min ; max)
            CHB: histéresis (min ; max)
        Retorna True solo si ambos canales fueron parseados correctamente.
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
        """Carga configuración desde texto ( Contenido de test-config.txt)."""
        conf = self._parse_conf_from_file(txt)
        self._last_conf_struct = conf
        return conf is not None

    def clear_conf(self):
        """Limpia la configuración en memoria (usar antes de una nueva consulta)."""
        self._last_control_msg = None
        self._last_conf_struct = None


    # =============================================================
    # CONSULTAS PARA LA GUI
    # =============================================================
    def get_last_conf_struct(self) -> Optional[dict]:
        """Retorna la configuración parseada {CHA:{min,max}, CHB:{min,max}}."""
        return self._last_conf_struct
 
    def get_last_control_msg(self) -> Optional[str]:
        """
        Retorna el texto completo del log recibido del TAR.
        La GUI lo muestra en el popup al finalizar un ensayo LIVE.
        """
        return self._last_control_msg
 
 
    # =============================================================
    # HISTÉRESIS
    # =============================================================
    def apply_hysteresis(self, params: dict):
        """
        Empaqueta y envía CHA_H + CHB_H al TAR antes de start().
 
        Formato (alineado con serial_port.c del hardware):
            param = (max << 16) | min   → uint32 Big-Endian
            [0x25][cmd][B3][B2][B1][B0] = 6 bytes
            700ms entre comandos        → usleep(700000) del C
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

        # Validación de rango
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
        """Crea la estructura de directorios para este ensayo."""
        timestamp = time.strftime("%d%m%Y-%H%M%S")
        self.ensayo_dir = self.base_dir / f"Ensayo_{timestamp}"
        self.csv_dir    = self.ensayo_dir / "csv"

        if self._live_mode:
            self.bin_dir = self.ensayo_dir / "bin"
            self.bin_dir.mkdir(parents=True, exist_ok=True)

        self.csv_dir.mkdir(parents=True, exist_ok=True)


    def _guardar_incremental(self):
        """
        Guarda los registros nuevos desde la última vez que se guardó.
        Se ejecuta cada _save_interval_s segundos durante LIVE.
        Guarda:
            - CSV parcial por canal (con timestamp en el nombre para no pisar).
            - BIN parcial con los bytes crudos correspondientes.
        """
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
        """
        Guarda los archivos definitivos del ensayo (todos los registros).
        Se ejecuta una sola vez al finalizar.
        """
        if not self._procesador.registros:
            return

        ts = time.strftime("%d%m%Y-%H%M%S")
        # CSV completos por canal
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

        # BIN completo (solo en LIVE; en REPLAY ya existe el original)
        if self._live_mode:
            bin_path = self.bin_dir / f"{ts}_test-raw.bin"
            with open(bin_path, "wb") as f:
                for r in self._procesador.registros:
                    f.write(r["_raw"])


    def _guardar_conf_final(self):
        """
        Guarda test-config.txt.
        Prioridad:
            1. _last_conf_struct (respuesta real del TAR via GET_CONF pre-start)
            2. _applied_params  (backup: lo que se envió)
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

        # Guardar también el texto completo del log si está disponible
        if self._last_control_msg:
            log_path = self.bin_dir / "test-log.txt"
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(self._last_control_msg)
            log.info("Log TAR guardado en %s", log_path)


