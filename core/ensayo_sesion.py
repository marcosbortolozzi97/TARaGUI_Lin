
# core/ensayo_sesion.py
"""
Orquestador del ciclo de vida de un ensayo TAR.

Tiene las siguientes responsabilidades:
    - Controlar start / stop del ensayo.
    - Conectar la fuente de datos (serie o replay) con el procesador.
    - Enviar / recibir comandos al hardware (START, STOP, GET_CONF, CHA_H, CHB_H).
    - Guardar datos incrementalmente durante LIVE y el archivo final al terminar.
    - Exponer los registros procesados a la GUI.

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
    """Modos de operación del ensayo."""
    LIVE   = auto()     # Datos en tiempo real desde puerto serie
    REPLAY = auto()     # Datos desde un archivo .bin grabado previamente


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
        self._live_mode = (mode is TARMode.LIVE)    # Shortcut booleano

        # ── Procesador según modo ────────────────────────────────────
        if self._live_mode:
            self._procesador = ProcesaDatosLive(async_mode=True)
            self._procesador.start_async()   # Worker debe estar listo antes de start()
        else:
            self._procesador = ProcesaDatosReplay()

        # ── Estado del ensayo ────────────────────────────────────────
        self._running         = False
        self._closing_pending = False   # True entre STOP y guardado final
        self._stop_sent       = False   # Evita enviar STOP doble

        # ── Estado de GET_CONF ───────────────────────────────────────
        # El TAR responde a GET_CONF con un bloque ASCII delimitado por 0x25.
        # Estos atributos controlan la recepción y la construcción de la estructura
        # de esa respuesta.
        self._expecting_conf    = False
        self._conf_requested    = False
        self._conf_received     = False
        self._conf_buffer       = ""                        # Texto ASCII acumulado
        self._conf_deadline     = None                      # Timeout: si no llega en 2 s, se cierra sin ella
        self._last_control_msg: Optional[str]  = None       # Texto completo de la última respuesta
        self._last_conf_struct: Optional[dict] = None       # Diccionario parseado {CHA:{min,max}, CHB:{min,max}}
        self._applied_params:  Optional[dict]  = None       # Parámetros que SE ENVIARON (backup si GET_CONF falla)

        # ── Guardado incremental (solo LIVE) ─────────────────────────
        self._save_interval_s = 15          # Cada 15 s se guarda un snapshot
        self._next_save_t     = 0.0         # Timestamp (time.time) del próximo guardado
        self._last_saved_idx  = 0           # Índice hasta donde ya se guardó en disco

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
        - Resetea el procesador y los flags.
        - Crea las carpetas de salida.
        - Conecta la fuente con los callbacks del procesador.
        - En modo LIVE: envía START al hardware.
        """
        if self._running:
            return

        # Reseteo completo
        self._procesador.reset()
        self._conf_received     = False
        self._closing_pending   = False
        self._expecting_conf    = False
        self._conf_buffer       = ""
        self._stop_sent         = False
        self._conf_requested    = False

        self._crear_carpetas_ensayo()

        # Inicializar guardado incremental (solo se utiliza en LIVE)
        if self._live_mode:
            self._last_saved_idx = 0
            self._next_save_t    = time.time() + self._save_interval_s

        # Conectar fuente al procesador.
        # data_callback:    cada chunk de bytes binarios va a feed() del procesador.
        # control_callback: bloques ASCII (GET_CONF) van a _on_control_bytes().
        self._fuente._start(
            self._procesador.feed,
            self._on_control_bytes
        )

        # En LIVE se envía START al TAR para que empiece a emitir pulsos
        if self._live_mode:
            self._fuente.send_command(TARCommands.START)

        self._running = True


    def tick(self):
        """
        Se llama periódicamente desde la GUI.
        Maneja dos tareas asíncronas:
            1. Guardado incremental durante LIVE.
            2. Cierre final cuando ya se recibió GET_CONF (o timeout).
        """
        # ── Guardado incremental (LIVE en curso) ─────────────────────
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

        # ── Cierre de LIVE (se espera GET_CONF o timeout) ────────────
        if self._closing_pending:
            conf_ok      = self._conf_received
            conf_timeout = (self._conf_deadline is not None
                            and time.time() >= self._conf_deadline)

            if conf_ok or conf_timeout:
                self._guardar_conf_final()      # Guarda test-config.txt
                self._guardar_final()           # Guarda CSV + BIN completos
                if self._fuente.is_running():
                    self._fuente._stop()        # Detiene el hilo de lectura serie
                self._closing_pending = False
                self._running         = False


    def stop(self):
        """
        Inicia el cierre de un ensayo LIVE.

        Secuencia:
            1. Envía STOP al hardware (deja de emitir pulsos).
            2. Detiene el worker async del procesador.
            3. Solicita GET_CONF para guardar la configuración.
            4. Pone _closing_pending=True; el cierre real lo hace tick().
        """
        if not self._running or self._stop_sent:
            return

        self._stop_sent = True

        if self._live_mode:
            # Detener worker async: ya no van a llegar datos nuevos
            if hasattr(self._procesador, 'stop_async'):
                self._procesador.stop_async()

            self._fuente.send_command(TARCommands.STOP)
            self._request_final_conf()                  # GET_CONF automático
            self._closing_pending = True
            self._conf_deadline   = time.time() + 2.0   # 2 seg de timeout


    # =============================================================
    # GET_CONF: solicitud y recepción
    # =============================================================
    def get_conf(self) -> None:
        """
        Solicita al TAR la configuración actual (uso manual desde GUI).
        Solo válido en LIVE y cuando NO hay ensayo en curso.
        """
        if not self._live_mode:
            raise RuntimeError("GET_CONF solo es válido en modo LIVE")
        if self._running:
            raise RuntimeError("No se puede consultar durante un ensayo en curso")
        if not hasattr(self._fuente, "send_command"):
            raise RuntimeError("La fuente no permite envío de comandos")

        # Limpiar estado previo y enviar comando
        self._last_control_msg  = None
        self._last_conf_struct  = None
        self._conf_buffer       = ""
        self._expecting_conf    = True
        self._fuente.send_command(TARCommands.GET_CONF)


    def _request_final_conf(self):
        """
        GET_CONF automático al finalizar un ensayo LIVE.
        Se guarda la configuración juntos con los datos del ensayo.
        """
        if self._conf_requested:
            return
        if not hasattr(self._fuente, "send_command"):
            return

        self._conf_requested    = True
        self._last_control_msg  = None
        self._last_conf_struct  = None
        self._conf_buffer       = ""
        self._expecting_conf    = True
        self._fuente.send_command(TARCommands.GET_CONF)


    def _on_control_bytes(self, data: bytes):
        """
        Callback que SerialSource invoca cuando recibe un bloque ASCII
        completo delimitado por 0x25 ... 0x25.

        Si no estamos esperando una respuesta de GET_CONF, el bloque
        se ignora silenciosamente.
        """
        if not self._expecting_conf:
            return

        # Decodifica a texto y acumula (puede llegar en varios bloques de tramas)
        text = data.decode("ascii", errors="ignore")
        self._conf_buffer += text

        # Intenta construir la estructura de configuración; si tiene CHA y CHB se da por completado
        if self._parse_conf_text(self._conf_buffer):
            self._last_control_msg = self._conf_buffer
            self._expecting_conf   = False
            self._conf_received    = True


    def _parse_conf_text(self, text: str) -> bool:
        """
        Extrae los valores de histéresis del texto ASCII del TAR.
        El formato esperado es el siguiente (puede tener texto extra antes/después):
            AXI_TAR
            CHA: histeresis (1300 ; 1500) mV
            CHB: histeresis (1200 ; 1600) mV

        Usa regex flexible para tolerar espacios o texto extra.
        Retorna True cuando ambos canales fueron analizados y construidos exitosamente.
        """
        conf = {}
        try:
            # Busca patrón CHA ... (min ; max)
            m = re.search(r"CHA.*?\(\s*(\d+)\s*;\s*(\d+)\s*\)",
                          text, re.IGNORECASE | re.DOTALL)
            if m:
                conf["CHA"] = {"min": int(m.group(1)), "max": int(m.group(2))}

            # Busca patrón CHB ... (min ; max)
            m = re.search(r"CHB.*?\(\s*(\d+)\s*;\s*(\d+)\s*\)",
                          text, re.IGNORECASE | re.DOTALL)
            if m:
                conf["CHB"] = {"min": int(m.group(1)), "max": int(m.group(2))}

            self._last_conf_struct = conf if conf else None
            return len(conf) == 2       # Solo True si ambos canales están

        except Exception as e:
            log.warning("Error interpretando configuración TAR", exc_info=e)
            return False


    def _parse_conf_from_file(self, text: str) -> Optional[dict]:
        """
        Parsea el archivo test-config.txt que se guarda junto al ensayo 
        (este se guarda junto con los archivos binarios en live).
        Formato:
            CHA min max
            CHB min max
        (Usado al cargar un replay para mostrar la configuración original.)
        """
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
    # HISTÉRESIS: envío de parámetros al TAR
    # =============================================================
    def apply_hysteresis(self, params: dict):
        """
        Empaqueta y envía las ventanas de histéresis al TAR.
        Debe llamarse ANTES de start().

        Args:
            params: diccionario con claves:
                "umbral_cha_min", "umbral_cha_max",
                "umbral_chb_min", "umbral_chb_max"

        Formato binario enviado:
            CHA_H: 0x25 | 0xA0 | MIN (16 bits LE) | MAX (16 bits LE)
            CHB_H: 0x25 | 0xB0 | MIN (16 bits LE) | MAX (16 bits LE)
        """
        if not self._live_mode:
            raise RuntimeError("No se pueden aplicar parámetros en modo REPLAY")
        if not hasattr(self._fuente, "send_command"):
            raise RuntimeError("La fuente no permite envío de comandos")

        # Extraer valores
        try:
            A_min = int(params["umbral_cha_min"])
            A_max = int(params["umbral_cha_max"])
            B_min = int(params["umbral_chb_min"])
            B_max = int(params["umbral_chb_max"])
        except KeyError as e:
            raise ValueError(f"Parámetro faltante: {e}")

        # Validación de rango
        if not (0 <= A_min < A_max <= 16383):
            raise ValueError("Valores inválidos para CHA")
        if not (0 <= B_min < B_max <= 16383):
            raise ValueError("Valores inválidos para CHB")

        # Empaquetado: "<BBHH" = 1 byte header + 1 byte cmd + 2 uint16 little endian
        cmd_cha = struct.pack("<BBHH", 0x25, 0xA0, A_min, A_max)
        cmd_chb = struct.pack("<BBHH", 0x25, 0xB0, B_min, B_max)

        self._fuente.send_command(cmd_cha)
        self._fuente.send_command(cmd_chb)

        # Guarda backup en caso de que GET_CONF falle al finalizar
        self._applied_params = {
            "CHA": {"min": A_min, "max": A_max},
            "CHB": {"min": B_min, "max": B_max},
        }


    # =============================================================
    # DATOS PARA LA GUI
    # =============================================================
    def get_registros(self) -> List[Dict]:
        """Retorna la lista completa de registros procesados."""
        return self._procesador.registros

    def get_eventos_desde(self, idx: int) -> List[Dict]:
        """
        Retorna registros desde el índice idx (inclusive).
        Usado por la GUI para obtener solo los datos nuevos desde la
        última actualización del histograma.
        """
        regs = self._procesador.registros
        if idx < 0 or idx >= len(regs):
            return []
        return regs[idx:]

    def get_last_conf_struct(self) -> Optional[dict]:
        """Retorna la configuración parseada del TAR (o None si no hay)."""
        return self._last_conf_struct


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

        # Separar por canal para generar un CSV por cada uno
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

        # BIN parcial: los 8 bytes originales de cada frame
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
        Guarda test-config.txt con la configuración de histéresis.
        Prioridad:
            1. _last_conf_struct (respuesta real del TAR via GET_CONF).
            2. _applied_params  (lo que se envió; backup si GET_CONF no llegó).
        Si no hay ninguno, no se guarda archivo.
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

