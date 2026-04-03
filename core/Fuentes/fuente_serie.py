
# core/Fuentes/fuente_serie.py
"""
Fuente de datos TAR vía puerto serie (modo LIVE).

Responsabilidades:
    - Abrir y cerrar el puerto serie físico.
    - Leer bytes en un hilo de background sin bloquear la GUI.
    - Separar los datos recibidos en dos tipos según el modo:
        * STREAMING (después de START): todo es datos binarios TAR → data_callback
        * COMANDO   (antes de START):   busca bloques ASCII { } → control_callback
    - Exponer read_raw() para lectura sincrónica antes de arrancar el hilo
      (usado por get_conf_pre_start en ensayo_sesion).
    - Enviar comandos al TAR.

DISEÑO DE MODOS:
    Se mantienen dos modos de lectura porque los frames TAR de 8 bytes pueden
    contener cualquier valor de byte en sus campos. Si se buscaran delimitadores
    { } durante el streaming se correría el riesgo de fragmentar frames válidos.
    El GET_CONF se manda ANTES del START y su respuesta llega en modo COMANDO
    (sin hilo activo), por eso se lee sincrónicamente con read_raw().
    Una vez recibido el log, se arranca el hilo y se manda START.

COORDINACIÓN DE CIERRE (tres hilos):
    El cierre limpio requiere coordinar tres hilos:
        Hilo 1 (GUI):    llama stop() → send_command(STOP) → stop_async()
        Hilo 2 (serie):  sigue leyendo hasta que puerto inactivo Y worker terminó
        Hilo 3 (worker): vacía la cola y termina

    El hilo serie recibe worker_running_fn como callback en _start().
    En _read_loop, antes de cerrar, consulta si el worker terminó.
    Esto garantiza que todos los bytes encolados se procesen antes del cierre.
"""

import threading
import serial
from typing import Callable, Optional
import logging
import time

log = logging.getLogger(__name__)


class SerialSource:

    def __init__(
        self,
        port:               str,
        baudrate:           int   = 115200,
        chunk_size:         int   = 256,
        inactivity_timeout: float = 2.0
    ):
        self.port               = port
        self.baudrate           = baudrate
        self.chunk_size         = chunk_size
        self.inactivity_timeout = inactivity_timeout

        self._ser:    Optional[serial.Serial]    = None
        self._thread: Optional[threading.Thread] = None
        self._running        = False
        self._streaming      = False
        self._stopping       = False
        self._last_data_time = 0.0

        self._data_callback:       Optional[Callable[[bytes], None]] = None
        self._control_callback:    Optional[Callable[[bytes], None]] = None
        self._worker_running_fn:   Optional[Callable[[], bool]]      = None

        self._in_control_msg = False
        self._control_buffer = bytearray()


    # =============================================================
    # APERTURA / CIERRE DEL PUERTO
    # =============================================================
    def open(self) -> None:
        """Abre el puerto serie. Si ya está abierto, no hace nada."""
        if self._ser and self._ser.is_open:
            return
        try:
            self._ser = serial.Serial(self.port, self.baudrate, timeout=0.05)
            log.info("Puerto serie %s abierto", self.port)
        except Exception as e:
            raise RuntimeError(f"No se pudo abrir el puerto serie {self.port}: {e}")

    def close(self) -> None:
        """Detiene la lectura y cierra el puerto."""
        self._stop()

        # Esperar a que el hilo de lectura termine realmente
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

        if self._ser:
            try:
                self._ser.close()
                log.info("Puerto serie %s cerrado", self.port)
            except Exception as e:
                log.warning("Error al cerrar puerto serie: %s", e)
            finally:
                self._ser = None


    # =============================================================
    # LECTURA SINCRÓNICA (sin hilo — para GET_CONF pre-start)
    # =============================================================
    def read_raw(self, timeout_s: float = 2.0) -> bytes:
        """
        Lee bytes directamente del puerto de forma sincrónica y bloqueante.
        Se usa ANTES de arrancar el hilo (_start), para leer la respuesta
        del GET_CONF que llega antes del START.

        Lee hasta que no lleguen más datos durante timeout_s segundos.
        Retorna todos los bytes recibidos.
        """
        if not self._ser or not self._ser.is_open:
            raise RuntimeError("Puerto serie no abierto")

        buffer   = bytearray()
        t_ultimo = time.time()

        while True:
            if self._ser.in_waiting > 0:
                chunk    = self._ser.read(self._ser.in_waiting)
                buffer  += chunk
                t_ultimo = time.time()
            else:
                if time.time() - t_ultimo > timeout_s:
                    break
                time.sleep(0.01)

        return bytes(buffer)


    # =============================================================
    # INICIO / PARADA DE LA LECTURA
    # =============================================================
    def _start(
        self,
        data_callback:     Callable[[bytes], None],
        control_callback:  Optional[Callable[[bytes], None]] = None,
        worker_running_fn: Optional[Callable[[], bool]]      = None
    ) -> None:
        """
        Inicia el hilo de lectura en segundo plano.

        Args:
            data_callback:     Recibe chunks de bytes binarios TAR.
            control_callback:  Recibe bloques ASCII { } completos (GET_CONF).
            worker_running_fn: Callback que retorna True si el worker async
                               todavía está procesando datos. El hilo serie
                               lo consulta antes de cerrar para asegurarse
                               de que todos los bytes encolados se procesaron.
        """
        if self._running:
            return
        if not self._ser or not self._ser.is_open:
            raise RuntimeError("Puerto serie no abierto")

        self._data_callback      = data_callback
        self._control_callback   = control_callback
        self._worker_running_fn  = worker_running_fn
        self._streaming          = False
        self._stopping           = False
        self._last_data_time     = time.time()
        self._in_control_msg     = False
        self._control_buffer.clear()

        # Limpiar buffer: descarta cualquier basura antes de arrancar
        self._ser.reset_input_buffer()
        log.debug("Buffer de entrada limpiado")

        self._running = True
        self._thread  = threading.Thread(
            target=self._read_loop,
            name="SerialSourceReader",
            daemon=True
        )
        self._thread.start()
        log.debug("Loop de lectura serie iniciado")

    def _stop(self) -> None:
        """
        Cierre suave NO BLOQUEANTE.
        Activa _stopping=True y retorna inmediatamente.
        El hilo termina cuando el puerto está inactivo Y el worker terminó.
        """
        if not self._running:
            return

        self._stopping  = True
        self._streaming = False
        log.info("Cierre suave activado — hilo termina cuando worker vacíe la cola")

    def is_running(self) -> bool:
        return self._running


    # =============================================================
    # ENVÍO DE COMANDOS
    # =============================================================
    def send_command(self, cmd: bytes) -> None:
        """
        Escribe un comando en el puerto serie.

        El flag _streaming se actualiza ANTES de escribir para evitar
        race conditions: el TAR empieza a emitir casi de inmediato tras
        recibir START y los primeros bytes deben encontrar el flag ya listo.
        """
        if not self._ser or not self._ser.is_open:
            raise RuntimeError("Puerto serie no abierto")

        # Actualizar modo ANTES de enviar
        if len(cmd) >= 2:
            if cmd[1] == 0x01:       # START
                self._streaming = True
                self._stopping  = False
                log.debug("Modo STREAMING activado")
            elif cmd[1] == 0x02:     # STOP
                self._streaming = False
                self._stopping  = True
                log.debug("Modo STREAMING desactivado — esperando vaciado de cola")

        try:
            self._ser.write(cmd)
            log.debug("Comando TAR enviado: %s", cmd.hex())
        except Exception as e:
            raise RuntimeError(f"Error enviando comando al TAR: {e}")


    # =============================================================
    # LOOP DE LECTURA
    # =============================================================
    def _read_loop(self) -> None:
        """
        Loop de lectura no bloqueante con cierre coordinado.

        Condición de cierre:
            _stopping=True (se recibió STOP)
            AND puerto inactivo durante inactivity_timeout segundos
            AND worker terminó de procesar la cola (worker_running_fn()=False)

        Esto garantiza que ningún byte encolado se pierda antes del cierre.
        """
        log.debug("Iniciando loop de lectura")

        while self._ser:
            try:
                disponibles = self._ser.in_waiting

                if disponibles > 0:
                    chunk = self._ser.read(min(disponibles, self.chunk_size))
                    if chunk:
                        self._last_data_time = time.time()
                        if self._streaming:
                            self._process_binary(chunk)
                        else:
                            self._process_mixed(chunk)
                else:
                    if self._stopping:
                        elapsed = time.time() - self._last_data_time

                        # Verificar si el worker terminó de procesar la cola
                        worker_termino = (
                            self._worker_running_fn is None
                            or not self._worker_running_fn()
                        )

                        if elapsed > self.inactivity_timeout and worker_termino:
                            log.info(
                                "Sin datos por %.1fs y worker terminado — cerrando hilo",
                                elapsed
                            )
                            break
                        elif elapsed > self.inactivity_timeout and not worker_termino:
                            log.debug("Puerto inactivo pero worker aún procesando cola...")

                    time.sleep(0.005)

            except Exception as e:
                log.error(f"Error crítico en lectura serie (posible desconexión): {e}")
                
                # Forzamos el estado de stopping para que el resto del sistema 
                # sepa que la fuente de datos se cortó.
                self._stop() # Marca _stopping = True y _streaming = False
    
                # Le damos un margen al worker para vaciar lo que quedó en la cola 
                # antes de que el hilo de lectura (y posiblemente el orquestador)
                # declaren que la sesión terminó.
                timeout_error = 1.5 
                start_error = time.time()
    
                while time.time() - start_error < timeout_error:
                    # Si el worker ya terminó, salimos antes
                    if self._worker_running_fn and not self._worker_running_fn():
                        break
                    time.sleep(0.1)
        
                log.warning("Hilo de lectura finalizado tras error y espera de cortesía.")
                break

        self._running = False
        log.debug("Loop de lectura finalizado")


    # =============================================================
    # PROCESAMIENTO DE DATOS
    # =============================================================
    def _process_binary(self, data: bytes) -> None:
        """Modo STREAMING: todo el chunk son datos binarios TAR."""
        if self._data_callback:
            self._data_callback(data)

    def _process_mixed(self, data: bytes) -> None:
        """
        Modo COMANDO: separa bytes de control de datos binarios.

        El TAR delimita la respuesta GET_CONF con '{' (0x7B) y '}' (0x7D).
        Todo lo que no está dentro de un bloque de control se trata como
        datos binarios y se manda al data_callback.
        """
        bin_buffer = bytearray()

        for b in data:
            if not self._in_control_msg:
                if b == ord('{'):
                    self._in_control_msg = True
                    self._control_buffer.clear()
                    if bin_buffer and self._data_callback:
                        self._data_callback(bytes(bin_buffer))
                        bin_buffer.clear()
                else:
                    bin_buffer.append(b)
            else:
                if b == ord('}'):
                    self._in_control_msg = False
                    self._emit_control_block()
                else:
                    self._control_buffer.append(b)

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
