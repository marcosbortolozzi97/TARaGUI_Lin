
# core/procesar_datos_live.py
"""
Procesador TAR para modo LIVE (tiempo real desde puerto serie).

Extiende ProcesaDatosBase añadiendo un modo asíncrono:
    - SÍNCRONO (default): feed() hace el analisis y almacena inmediatamente.
    - ASÍNCRONO:          feed() solo encola los bytes; un worker thread
                          separado hace el procesamiento. Esto evita que el hilo
                          de lectura serie se bloquee y la GUI no se congela.

EnsayoSession siempre lo instancia con async_mode=True.
"""

import queue
import threading
from typing import Optional
from .procesar_datos_base import ProcesaDatosBase


class ProcesaDatosLive(ProcesaDatosBase):

    def __init__(self, async_mode: bool = False):
        super().__init__()
        self.async_mode = async_mode

        # Solo se crean estos atributos si se usa modo async.
        if self.async_mode:
            self.data_queue: queue.Queue = queue.Queue()   # Cola de bytes pendientes
            self._running  = False                         # Flag de vida del worker
            self._worker_thread: Optional[threading.Thread] = None

    # -------------------------------------------------
    # Reset
    # -------------------------------------------------
    def reset(self):
        """Limpia estado base + vacía la cola si está en modo async."""
        super().reset()
        if self.async_mode:
            # Vaciar la cola de forma segura (puede tener elementos en vuelo)
            while not self.data_queue.empty():
                try:
                    self.data_queue.get_nowait()
                    self.data_queue.task_done()
                except queue.Empty:
                    break

    # =============================================================
    # CICLO DE VIDA DEL WORKER (solo modo async)
    # =============================================================
    def start_async(self):
        """
        Lanza el worker thread.
        Debe llamarse ANTES de que lleguen datos (es decir, antes de
        EnsayoSession.start()).
        """
        if not self.async_mode:
            raise RuntimeError("start_async requiere async_mode=True")
        if self._running:
            return      # Ya está activo, no se relanza

        self._running = True
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="TAR_Live_Worker",
            daemon=True         # Se mata automáticamente al cerrar la app
        )
        self._worker_thread.start()

    def stop_async(self, timeout: float = 2.0):
        """
        Señala al worker que pare y espera a que termine.
        Debe llamarse despues de que la fuente dejó de enviar datos
        (es decir, después de EnsayoSession.stop()).

        Args:
            timeout: Segundos máximos de espera antes de abandonar.
        """
        if not self.async_mode or not self._running:
            return

        self._running = False
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)

    # =============================================================
    # ENTRADA DE DATOS
    # =============================================================
    def feed(self, data: bytes):
        """
        Recibe un chunk de bytes desde SerialSource (hilo de lectura serie).

        - Modo SYNC:  llama a super().feed() directamente para parseo inmediato.
        - Modo ASYNC: mete los bytes en la cola y retorna al momento.
                      El worker thread los procesa en segundo plano.
        """
        if self.async_mode:
            self.data_queue.put(data)       # No bloqueante
        else:
            super().feed(data)              # Bloqueante (parseo aquí mismo)

    # =============================================================
    # WORKER THREAD (solo modo async)
    # =============================================================
    def _worker_loop(self):
        """
        Loop que vive mientras self._running sea True.
        Saca chunks de la cola y los pasa a super().feed() (parseo + almacenamiento).

        El timeout=0.1 en get() permite que el loop recompruebe self._running
        cada 100 ms, así se detiene limpiamente cuando se pide stop_async().
        Cuando _running pasa a False, sigue vaciando la cola hasta que esté
        vacía (condición del while) para no perder datos en vuelo.
        """
        while self._running or not self.data_queue.empty():
            try:
                chunk = self.data_queue.get(timeout=0.1)
                super().feed(chunk)             # Parseo real ocurre aquí
                self.data_queue.task_done()
            except queue.Empty:
                continue                        # Timeout normal, recomprueba condición
            except Exception as e:
                print(f"[ERROR] Worker TAR Live: {e}")
                # No se rompe el loop: un chunk malo no debe detener el resto

    # =============================================================
    # CONSULTAS DE ESTADO
    # =============================================================
    def is_async_running(self) -> bool:
        """True si el worker thread está activo."""
        return self.async_mode and self._running

    def get_queue_size(self) -> int:
        """Cantidad de chunks pendientes en la cola (0 si no está en modo async)."""
        if not self.async_mode:
            return 0
        return self.data_queue.qsize()
    
    