
# core/procesar_datos_base.py
"""
Clase base de procesamiento TAR.

Este archivo implementa la lógica común para convertir bytes crudos en registros de 
pulsos con timestamps absolutos. Se encarga de:
    - Recibir bytes crudos y generar la estructura en TARFrame (vía TARFrameParser).
    - Reconstruir el timeline absoluto sumando overflows al timestamp local.
    - Filtrar el primer frame si parece basura (VP fuera de rango físico).
    - Almacenar los registros procesados y exponer estadísticas.

Hereda: ProcesaDatosLive y ProcesaDatosReplay.
No debe instanciarse directamente.
"""

from typing import List, Dict
from .protocolo_tar import (
    TARFrameParser,
    channel_to_index,
    TS_MAX
)

# --- Si cambiamos a True para ver mensajes en consola, eran usados para debuggear ---
DEBUG_OVERFLOWS   = False
DEBUG_FIRST_FRAME = False


class ProcesaDatosBase:

    def __init__(self):
        self._crear_parser()
        self.registros: List[Dict] = []     # Lista central de pulsos procesados

        # --- Estado del timeline ---
        self._time_base = 0         # Acumulador de overflows en ticks.
                                    # Cada overflow suma 2^32 ticks (~42.95 s).
        self._started   = False     # Become True al procesar el primer pulso válido.

        # --- Estadísticas ---
        self._overflow_count      = 0   # Cantidad de overflows detectados
        self._frames_descartados  = 0   # Frames rechazados por este procesador

    # -------------------------------------------------
    def _crear_parser(self):
        """Instancia un TARFrameParser nuevo."""
        self.parser = TARFrameParser()

    def reset(self):
        """Reinicia todo el estado interno (usar al empezar un ensayo nuevo)."""
        self.registros.clear()
        self._crear_parser()
        self._time_base          = 0
        self._started            = False
        self._overflow_count     = 0
        self._frames_descartados = 0

    # =============================================================
    # ENTRADA DE DATOS
    # =============================================================
    def feed(self, data: bytes):
        """
        Punto de entrada principal: recibe bytes crudos de la fuente.

        El flujo es:
            1. El parser convierte bytes en TARFrame.
            2. Si es el primer frame y tiene VP sospechoso, se descarta.
               (Protección contra basura al inicio de una conexión serie.)
            3. Los frames válidos pasan a _process_frames().
        """
        frames = self.parser.feed(data)

        # ── Filtro de primer frame ───────────────────────────────────
        # Un archivo cortado o una conexión serie nueva puede empezar
        # con bytes incoherentes que forman un frame con VP absurdo.
        # Se descarta SOLO ese primer frame si está fuera de rango.
        if not self._started and frames:
            primer = frames[0]
            if not primer.is_overflow:
                # Rango esperado: 100–10000 cuentas ADC
                if primer.vp < 100 or primer.vp > 10000:
                    if DEBUG_FIRST_FRAME:
                        print(f"[WARN] Descartando primer frame con VP={primer.vp} (fuera de rango)")
                    frames = frames[1:]
                    self._frames_descartados += 1

        self._process_frames(frames)

    # =============================================================
    # PROCESAMIENTO FRAME A FRAME
    # =============================================================
    def _process_frames(self, frames: List):
        """
        Itero sobre los frames decodificados y los convierte en registros.

        Por cada frame:
            1. Si es overflow se actualiza _time_base y sigue al siguiente.
            2. Filtra canales no válidos (None = overflow ya tratado u otro).
            3. Calcula timestamp absoluto = _time_base + ts_local.
            4. Almacena el registro con canal, timestamp, amplitud y bytes crudos.
        """
        for f in frames:

            # ── 1. Overflow del timestamp ────────────────────────────
            # El hardware emite una trama especial (CH=0b11) cada vez que
            # el contador de 32 bits pasa de 0xFFFFFFFF a 0.
            # Se suma TS_MAX (2^32) al acumulador para mantener continuidad.
            if f.is_overflow:
                self._time_base += TS_MAX
                self._overflow_count += 1

                if DEBUG_OVERFLOWS:
                    print(f"[OVERFLOW #{self._overflow_count}] "
                          f"time_base={self._time_base} "
                          f"({self._time_base * 10 / 1e9:.2f} s acumulados)")
                continue   # No es un pulso, no se almacena

            # ── 2. Filtrado de canal ─────────────────────────────────
            # channel_to_index retorna None si el código no es A ni B.
            ch = channel_to_index(f.ch)
            if ch is None:
                continue

            # ── 3. Timestamp absoluto ────────────────────────────────
            # ts_local es el valor de 32 bits dentro de la trama.
            # ts_ext suma los overflows previa acumulados.
            ts_ext = self._time_base + f.ts

            # ── 4. Primer evento válido ──────────────────────────────
            if not self._started:
                self._started = True
                if DEBUG_FIRST_FRAME:
                    print(f"[PRIMER EVENTO] Canal={'A' if ch==0 else 'B'}, "
                          f"TS={f.ts}, VP={f.vp}")

            # ── 5. Almacenamiento ────────────────────────────────────
            # Los CSV originales del docente usan estas unidades cruda,
            # por eso no se convierten aquí.
            # _raw: los 8 bytes originales, necesarios para re-guardar el .bin.
            self.registros.append({
                "chan":    ch,
                "tstamp":  int(ts_ext),
                "ampl":    f.vp,
                "_raw":    f.raw_frame
            })

    # =============================================================
    # CONSULTAS
    # =============================================================
    def get_registros_por_canal(self) -> Dict[int, List[Dict]]:
        """Separa self.registros en dos listas: {0: [Canal A], 1: [Canal B]}."""
        canales = {0: [], 1: []}
        for reg in self.registros:
            ch = reg["chan"]
            if ch in canales:
                canales[ch].append(reg)
        return canales

    def get_estadisticas(self) -> Dict:
        """
        Retorna un diccionario con las estadísticas del ensayo actual.
        Si no hay registros, retorna valores cero.
        """
        if not self.registros:
            return {
                "total_pulsos":         0,
                "pulsos_canal_a":       0,
                "pulsos_canal_b":       0,
                "duracion_ticks":       0,
                "duracion_s":           0.0,
                "overflow_count":       self._overflow_count,
                "frames_descartados":   self._frames_descartados
            }

        canales      = self.get_registros_por_canal()
        ultimo_ts    = self.registros[-1]["tstamp"]   # Último timestamp absoluto
        parser_stats = self.parser.get_stats()           # Frames descartados por el parser

        return {
            "total_pulsos":       len(self.registros),
            "pulsos_canal_a":     len(canales[0]),
            "pulsos_canal_b":     len(canales[1]),
            "duracion_ticks":     ultimo_ts,
            "duracion_s":         (ultimo_ts * 10) / 1e9,   # ticks -> ns -> s
            "overflow_count":     self._overflow_count,
            # Suma los descartados de este nivel + los del parser
            "frames_descartados": self._frames_descartados + parser_stats["frames_descartados"]
        }














