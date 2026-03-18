
# core/Fuentes/replay_bin.py
"""
Fuente de datos TAR desde archivo binario (modo REPLAY).

Simula la llegada de datos por serie leyendo un archivo .bin grabado
previamente: lee chunks, los emite al callback, y espera un intervalo
entre cada uno para que la GUI pueda actualizarse.

La interfaz pública (_start, _stop, is_running) es la misma que
SerialSource, así EnsayoSession puede usarlas intercambiando.
"""

import threading
import time
from pathlib import Path
from typing import Callable, Optional


class ReplayBinSource:

    def __init__(
        self,
        path: str | Path,
        chunk_size:  int   = 256,       # Bytes por emisión (mismo que SerialSource)
        interval_s:  float = 0.023      # Pausa entre chunks (baudrate 115200 ≈ 0.023s por chunk de 256 bytes)
    ):
        self.path       = Path(path)
        self.chunk_size = chunk_size
        self.interval_s = interval_s

        # Tamaño del archivo: se calcula una vez al construir
        self._total_bytes = self.path.stat().st_size
        self._read_bytes  = 0           # Bytes emitidos hasta ahora (para get_seconds)

        self._thread:   Optional[threading.Thread]      = None
        self._running   = False
        self._callback: Optional[Callable[[bytes], None]] = None


    # =============================================================
    # INTERFAZ PÚBLICA (compatible con SerialSource)
    # =============================================================
    def close(self) -> None:
        self._stop()

    def _start(
        self,
        callback: Callable[[bytes], None],
        control_callback: Optional[Callable[[str], None]] = None
    ) -> None:
        """
        Inicia el replay en un hilo separado.

        Args:
            callback:         Recibe cada chunk de bytes (va al procesador).
            control_callback: No se usa en replay (existe solo por compatibilidad
                              de firma con SerialSource), recordando que EnsayoSession 
                              lo llama con ambos argumentos.
        """
        if self._running:
            return
        if not self.path.exists() or not self.path.is_file():
            raise RuntimeError(f"Archivo binario no válido: {self.path}")

        self._callback = callback
        self._read_bytes = 0
        self._running  = True

        self._thread = threading.Thread(
            target=self._replay_loop,
            name="ReplayBinSource",
            daemon=True
        )
        self._thread.start()

    def _stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    def is_running(self) -> bool:
        return self._running


    # =============================================================
    # LOOP DE REPLAY (hilo background)
    # =============================================================
    def _replay_loop(self) -> None:
        """
        Lee el archivo en bloques de chunk_size bytes y los emite al callback.
        Entre bloques hace un sleep para simular la llegada real por serie.

        El bloque finally garantiza que _running quede False cuando termine
        por lectura completa. Esto permite que EnsayoSession.tick() detecte 
        el fin del replay.
        """
        try:
            with open(self.path, "rb") as f:
                while self._running:
                    chunk = f.read(self.chunk_size)
                    if not chunk:
                        break       # EOF: el archivo se leyó completo

                    self._read_bytes += len(chunk)

                    if self._callback:
                        self._callback(chunk)

                    time.sleep(self.interval_s)  # Simula latencia de serie
        finally:
            # Se ejecuta siempre al salir del try (fin normal o stop externo)
            self._running = False


    # =============================================================
    # CONSULTA DE TIEMPO RESTANTE
    # =============================================================
    def get_progress_percentage(self) -> int:
        """
        Calcula el porcentaje de avance del replay.

        Retorna:
            float: Valor entre 0.0 y 100.0.
        """
        if self._total_bytes <= 0:
            return 0.0

        # Calculamos el porcentaje: (parte / total) * 100
        porcentaje = (self._read_bytes / self._total_bytes) * 100

        # Aseguramos que no exceda el 100% por redondeos y que no sea negativo
        avance = max(0.0, min(100.0, porcentaje))

        return int(avance)

   
