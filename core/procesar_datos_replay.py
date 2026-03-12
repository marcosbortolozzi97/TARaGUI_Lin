
# core/procesar_datos_replay.py
"""
Procesador TAR para modo REPLAY (archivos binarios previamente grabados).

Es un wrapper muy ligero sobre ProcesaDatosBase: no añade estado ni lógica
propia. La diferencia con ProcesaDatosLive es que no necesita cola ni
worker thread, porque ReplayBinSource controla el ritmo de emisión
internamente (time.sleep entre chunks).

Toda la lógica de parseo, overflow y almacenamiento está en la clase base.
"""

from .procesar_datos_base import ProcesaDatosBase


class ProcesaDatosReplay(ProcesaDatosBase):

    def __init__(self):
        super().__init__()
        # No se necesita estado adicional.

    def feed(self, data: bytes):
        """
        Recibe un chunk de bytes desde ReplayBinSource.

        Delega completamente a ProcesaDatosBase.feed(), que:
            1. Parsea los bytes en TARFrame.
            2. Aplica el filtro de primer frame basura.
            3. Reconstruye el timeline con overflows.
            4. Almacena los registros.

        No se reimplemena la lógica: usar super() garantiza que cualquier
        corrección futura en la clase base aplica automáticamente al replay.
        """
        super().feed(data)

