
# core/Fuentes/fuente_serie.py
"""
Fuente de datos TAR vía puerto serie (modo LIVE).

En este archivo se implementa la clase SerialSource, que maneja la comunicación con el TAR a través de un 
puerto serie. Específicamente, se encarga de:
    - Abrir y cerrar el puerto serie físico. Se espera a que dejen de llegar datos antes de cerrar el hilo, 
        evitando pérdida de información.
    - Leer bytes en un hilo de background sin bloquear la GUI.
    - Separar el los datos recibidos en dos tipos de mensaje según el estado: STREAMING (después de START) donde 
        todo es datos binarios TAR, es para data_callback, y COMANDO (antes de START o después de STOP) 
        que busca bloques ASCII delimitados por 0x25 para GET_CONF, es para control_callback.
    - Enviar comandos al TAR y cambiar de modo automáticamente.

    Utilizamos dos modos de lectura porque los frames TAR de 8 bytes pueden contener 
el byte 0x25 en cualquier posición de la trama. Si siempre buscábamos 0x25 como delimitador de 
mensajes de control se puede fragmentar los frames y el parser recibía basura.
    Como GET_CONF solo se usa cuando no hay stream activo, es más seguro buscar delimitadores 
en modo COMANDO.
"""

import threading
import serial
from typing import Callable, Optional
import logging
import time

log = logging.getLogger(__name__)


class SerialSource:

    DELIMITADOR = 0x25

    def __init__(
        self, 
        port: str, 
        baudrate: int = 115200, 
        chunk_size: int = 256,
        inactivity_timeout: float = 2.0 
    ):
        """
        Args:
            inactivity_timeout: Segundos sin recibir datos antes de cerrar
                                el hilo después de un STOP. Debe ser mayor
                                que el tiempo de transmisión esperado de 
                                los datos restantes.
        """
        self.port       = port
        self.baudrate   = baudrate
        self.chunk_size = chunk_size
        self.inactivity_timeout = inactivity_timeout  # NUEVO

        self._ser:    Optional[serial.Serial]      = None
        self._thread: Optional[threading.Thread]   = None
        self._running   = False
        self._streaming = False
        self._stopping  = False  # NUEVO: Flag de "cierre suave"
        self._last_data_time = 0.0  # NUEVO: Timestamp del último dato

        self._data_callback:    Optional[Callable[[bytes], None]] = None
        self._control_callback: Optional[Callable[[bytes], None]] = None

        self._in_control_msg  = False
        self._control_buffer  = bytearray()


    # =============================================================
    # APERTURA / CIERRE DEL PUERTO
    # =============================================================
    def open(self) -> None:
        """Abre el puerto serie. Si ya está abierto, no hace nada."""
        if self._ser and self._ser.is_open:
            return
        try:
            self._ser = serial.Serial(self.port, self.baudrate, timeout=1)
            log.info("Puerto serie %s abierto", self.port)
        except Exception as e:
            raise RuntimeError(f"No se pudo abrir el puerto serie {self.port}: {e}")

    def close(self) -> None:
        """Detiene la lectura y cierra el puerto."""
        self._stop()
        if self._ser:
            try:
                self._ser.close()
                log.info("Puerto serie %s cerrado", self.port)
            except Exception:
                pass
            self._ser = None


    # =============================================================
    # INICIO / PARADA DE LA LECTURA
    # =============================================================
    def _start(
        self,
        data_callback:    Callable[[bytes], None],
        control_callback: Optional[Callable[[bytes], None]] = None
    ) -> None:
        """Inicia el hilo de lectura en segundo plano."""
        if self._running:
            return
        if not self._ser or not self._ser.is_open:
            raise RuntimeError("Puerto serie no abierto")

        self._data_callback    = data_callback
        self._control_callback = control_callback
        self._streaming        = False
        self._stopping         = False  # NUEVO
        self._last_data_time   = time.time()  # NUEVO
        self._in_control_msg   = False
        self._control_buffer.clear()
        self._running          = True

        self._thread = threading.Thread(
            target=self._read_loop,
            name="SerialSourceReader",
            daemon=True
        )
        self._thread.start()
        log.debug("Loop de lectura serie iniciado")

    def _stop(self) -> None:
        """
        NUEVO: Cierre suave con espera de datos restantes.
        
        En lugar de matar el hilo inmediatamente, activa el flag _stopping.
        El hilo seguirá leyendo hasta que pasen N segundos sin datos.
        """
        if not self._running:
            return
        
        log.info("Iniciando cierre suave del puerto serie...")
        self._stopping = True      # Señal de "empezar a cerrar"
        self._streaming = False    # Ya no estamos en modo streaming
        
        # Esperar a que el hilo termine naturalmente (máx 30 seg)
        if self._thread:
            self._thread.join(timeout=30.0)
            if self._thread.is_alive():
                log.warning("Hilo de lectura no terminó a tiempo (forzando cierre)")
                self._running = False
                self._thread.join(timeout=1.0)
            self._thread = None
        
        log.info("Puerto serie cerrado")

    def is_running(self) -> bool:
        return self._running


    # =============================================================
    # ENVÍO DE COMANDOS
    # =============================================================
    def send_command(self, cmd: bytes) -> None:
        """Escribe un comando en el puerto serie."""
        if not self._ser or not self._ser.is_open:
            raise RuntimeError("Puerto serie no abierto")
        try:
            self._ser.write(cmd)
            self._ser.flush()

            if len(cmd) >= 2:
                if cmd[1] == 0x01:          # START
                    self._streaming = True
                    self._stopping = False   # NUEVO: Cancelar cierre si había uno
                    log.debug("Modo STREAMING activado")
                elif cmd[1] == 0x02:        # STOP
                    self._streaming = False
                    log.debug("Modo STREAMING desactivado")

            log.debug("Comando TAR enviado: %s", cmd.hex())
        except Exception as e:
            raise RuntimeError(f"Error enviando comando al TAR: {e}")


    # =============================================================
    # LOOP DE LECTURA 
    # =============================================================
    def _read_loop(self) -> None:
        """
        NUEVO: Loop con cierre inteligente.
        
        Funcionamiento:
        1. Lee datos normalmente mientras _running=True
        2. Cuando _stopping=True (usuario pidió stop):
           - Sigue leyendo datos
           - Si pasan N segundos sin datos → termina
        3. Si hay error fatal → termina inmediatamente
        """
        log.debug("Iniciando loop de lectura")
        
        while self._running and self._ser:
            try:
                chunk = self._ser.read(self.chunk_size)
                
                if chunk:
                    # ══════════════════════════════════════════════════
                    # HAY DATOS: Procesar normalmente
                    # ══════════════════════════════════════════════════
                    self._last_data_time = time.time()  # Actualizar timestamp
                    
                    if self._streaming:
                        self._process_binary(chunk)
                    else:
                        self._process_mixed(chunk)
                
                else:
                    # ══════════════════════════════════════════════════
                    # NO HAY DATOS: Verificar si es tiempo de cerrar
                    # ══════════════════════════════════════════════════
                    if self._stopping:
                        elapsed = time.time() - self._last_data_time
                        
                        if elapsed > self.inactivity_timeout:
                            # No hay datos hace N segundos → cerrar
                            log.info(f"Sin datos por {elapsed:.1f}s - cerrando hilo")
                            break
                        else:
                            # Todavía esperando datos restantes
                            log.debug(f"Esperando datos restantes... ({elapsed:.1f}s)")
                    
            except Exception as e:
                log.error("Error en lectura serie", exc_info=e)
                break
        
        self._running = False
        log.debug("Loop de lectura finalizado")


    def _process_binary(self, data: bytes) -> None:
        """Modo STREAMING: todo el chunk es datos binarios TAR."""
        if self._data_callback:
            self._data_callback(data)


    def _process_mixed(self, data: bytes) -> None:
        """Modo COMANDO: separa bytes de control de datos binarios."""
        bin_buffer = bytearray()

        for b in data:
            if b == self.DELIMITADOR:
                if not self._in_control_msg:
                    self._in_control_msg = True
                    self._control_buffer.clear()
                    if bin_buffer and self._data_callback:
                        self._data_callback(bytes(bin_buffer))
                        bin_buffer.clear()
                else:
                    self._in_control_msg = False
                    self._emit_control_block()
                continue

            if self._in_control_msg:
                self._control_buffer.append(b)
            else:
                bin_buffer.append(b)

        if bin_buffer and self._data_callback:
            self._data_callback(bytes(bin_buffer))


    def _emit_control_block(self) -> None:
        """Entrega el bloque de control completo al callback."""
        if not self._control_callback:
            return
        try:
            self._control_callback(bytes(self._control_buffer))
        except Exception as e:
            log.warning("Error entregando bloque de control TAR", exc_info=e)