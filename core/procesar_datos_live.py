
# core/procesar_datos_live.py
"""
Procesador TAR para modo LIVE (tiempo real desde puerto serie).

Extiende ProcesaDatosBase añadiendo procesamiento asíncrono mediante
un worker thread y una cola de bytes.

¿Por qué asíncrono?
    El hilo de lectura serie (SerialSource) no puede bloquearse: si feed()
    tardara en parsear, el buffer del puerto del SO (típicamente 4096 bytes)
    se llenaría y se perderían bytes. Con la cola, el hilo serie encola
    los bytes y retorna inmediatamente; el worker los procesa en paralelo.

Flujo:
    SerialSource (hilo lector)
        → feed() → encola chunk → retorna inmediato
                        ↓
                  worker thread
                        → parsea chunk → almacena en registros
"""

import queue
import threading
from typing import Optional
from .procesar_datos_base import ProcesaDatosBase


class ProcesaDatosLive(ProcesaDatosBase):

    def __init__(self):
        super().__init__()
        self.data_queue:    queue.Queue               = queue.Queue()
        self._running:      bool                      = False
        self._worker_thread: Optional[threading.Thread] = None


    # =============================================================
    # RESET
    # =============================================================
    def reset(self):
        """
        Limpia estado base y vacía la cola.
        Se llama al inicio de cada ensayo nuevo desde EnsayoSession.
        """
        super().reset()
        while not self.data_queue.empty():
            try:
                self.data_queue.get_nowait()
                self.data_queue.task_done()
            except queue.Empty:
                break


    # =============================================================
    # CICLO DE VIDA DEL WORKER
    # =============================================================
    def start_async(self):
        """
        Lanza el worker thread.
        Debe llamarse ANTES de que lleguen datos, es decir antes de
        EnsayoSession.start(), para que el worker esté listo cuando
        llegue el primer chunk.
        """
        if self._running:
            return   # Ya activo, no relanzar

        self._running = True
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="TAR_Live_Worker",
            daemon=True   # Se mata automáticamente al cerrar la app
        )
        self._worker_thread.start()

    def stop_async(self, timeout: float = 2.0):
        """
        Señala al worker que pare y espera a que vacíe la cola.

        Se llama después de que la fuente dejó de enviar datos
        (después de EnsayoSession.stop()), para asegurar que los
        últimos chunks en vuelo se procesen antes de cerrar.

        Args:
            timeout: Segundos máximos de espera antes de abandonar.
        """
        if not self._running:
            return

        self._running = False
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)


    # =============================================================
    # ENTRADA DE DATOS
    # =============================================================
    def feed(self, data: bytes):
        """
        Recibe un chunk de bytes desde SerialSource.
        Encola inmediatamente y retorna — nunca bloquea el hilo lector.
        """
        self.data_queue.put(data)


    # =============================================================
    # WORKER THREAD
    # =============================================================
    def _worker_loop(self):
        """
        Loop del worker: saca chunks de la cola y los parsea.

        El timeout=0.1 en get() permite recomprobar _running cada 100ms,
        así el worker se detiene limpiamente cuando stop_async() lo pide.

        Cuando _running pasa a False, el while sigue hasta vaciar la cola
        completamente para no perder datos en vuelo.
        """
        while self._running or not self.data_queue.empty():
            try:
                chunk = self.data_queue.get(timeout=0.1)
                super().feed(chunk)        # Parseo + almacenamiento en registros
                self.data_queue.task_done()
            except queue.Empty:
                continue                   # Timeout normal, recomprueba condición
            except Exception as e:
                print(f"[ERROR] Worker TAR Live: {e}")
                # No se rompe el loop: un chunk malo no detiene el resto


    # =============================================================
    # CONSULTAS DE ESTADO
    # =============================================================
    def is_async_running(self) -> bool:
        """True si el worker thread está activo."""
        return self._running

    def get_queue_size(self) -> int:
        """Cantidad de chunks pendientes en la cola."""
        return self.data_queue.qsize()
        
