
# core/protocolo_tar.py
"""
Definición formal del protocolo TAR.

Centraliza constantes, máscaras, la estructura TARFrame y el parser
que convierte un stream de bytes en objetos decodificados.

En memoria se guarda en little endian (byte 0 = LSB del word):
    b[0]=FTR  b[1]=VP_low  b[2]=VP_high|CH|TS_low  b[3..6]=TS  b[7]=HDR
"""

from dataclasses import dataclass
from typing import Optional, List


# =============================================================
# CONSTANTES DE FRAMING
# =============================================================
FRAME_SIZE = 8      # Cada trama ocupa exactamente 8 bytes
HDR        = 0x26   # Header: posición 7 en memoria (byte más significativo)
FTR        = 0x27   # Footer: posición 0 en memoria (byte menos significativo)


# =============================================================
# MÁSCARAS DE EXTRACCIÓN
# Se aplican después del shift correspondiente sobre el word de 64 bits.
# =============================================================
MSK_TS = 0xFFFFFFFF  # 32 bits de Timestamp
MSK_CH = 0x3         #  2 bits de Canal
MSK_VP = 0x3FFF      # 14 bits de Valor Pico


# =============================================================
# CÓDIGOS DE CANAL (campo CH, 2 bits)
# =============================================================
CH_A        = 0b01   # Canal A
CH_B        = 0b10   # Canal B 
CH_OVERFLOW = 0b11   # Marca especial: el contador TS de 32 bits desbordó


# =============================================================
# CONSTANTES TEMPORALES
# =============================================================
TS_BITS = 32            # Ancho del campo timestamp en bits
TS_MAX  = 1 << TS_BITS  # 2^32 = 4,294,967,296 ticks ~42.95 seg a 100 MHz
                        # Es el valor que se suma al acumulador cuando hay overflow


# =============================================================
# UTILIDADES
# =============================================================
def channel_to_index(ch: int) -> Optional[int]:
    """
    Convierte código de canal (2 bits) a índice de array.
    Returns:
        0 --> Canal A, 1 --> Canal B, None --> overflow u otro (no es pulso válido).
    """
    ch &= 0b11          # Máscara defensiva
    if ch == CH_A: return 0
    if ch == CH_B: return 1
    return None


# =============================================================
# ESTRUCTURA DE DATOS
# =============================================================
@dataclass
class TARFrame:
    """
    Trama TAR ya decodificada.

    Atributos:
        ts:        Timestamp LOCAL de 32 bits en ticks.
                   Para obtener el tiempo absoluto se debe sumar el
                   acumulador de overflows (lo hace ProcesaDatosBase).
        ch:        Código de canal crudo (2 bits): CH_A, CH_B o CH_OVERFLOW.
        vp:        Valor Pico en cuentas ADC (14 bits, rango 0–16383).
        raw_frame: Los 8 bytes originales sin tocar (se re-guarda en el .bin).
    """
    ts:        int
    ch:        int
    vp:        int
    raw_frame: bytes

    @property
    def is_overflow(self) -> bool:
        """True si la trama es una marca de overflow del timestamp."""
        return self.ch == CH_OVERFLOW

    @property
    def channel_index(self) -> Optional[int]:
        """Índice de array del canal (0=A, 1=B) o None si es overflow."""
        return channel_to_index(self.ch)


# =============================================================
# PARSER
# =============================================================
class TARFrameParser:
    """
    Convierte un stream arbitrario de bytes en objetos TARFrame.

    Modelo de operación:
        - Recibe chunks de cualquier tamaño (puede llegar cortado).
        - Acumula en un buffer interno.
        - Lee de 8 en 8 bytes, verifica HDR/FTR, decodifica campos.
        - Si el inicio está desalineado, intenta resincronizar una sola
          vez buscando el próximo HDR/FTR válido dentro de los siguientes 7 bytes.

    Uso:
        parser = TARFrameParser()
        frames = parser.feed(chunk)   # retorna lista (puede estar vacía)
    """

    def __init__(self):
        self._buffer            = bytearray()   # Bytes recibidos y no procesados aún
        self._frames_descartados = 0            # Frames que no pasaron validación HDR/FTR

    # -------------------------------------------------
    def feed(self, data: bytes) -> List[TARFrame]:
        """
        Ingresa bytes y retorna los TARFrame decodificados.

        Args:
            data: Chunk recibido (cualquier tamaño, no necesariamente alineado a 8).
        Returns:
            Lista de TARFrame. Puede estar vacía si no hay 8 bytes completos aún.
        """
        self._buffer.extend(data)
        frames = []
        i = 0   # Puntero de lectura dentro del buffer

        while i + FRAME_SIZE <= len(self._buffer):
            raw = bytes(self._buffer[i:i + FRAME_SIZE])

            # ── Validación HDR / FTR ─────────────────────────────────
            # En memoria: raw[0] debe ser FTR, raw[7] debe ser HDR.
            if raw[7] != HDR or raw[0] != FTR:
                self._frames_descartados += 1

                # Resincronización: solo al arranque del buffer.
                # Si los primeros bytes son basura (ej: media trama de
                # una conexión previa), busca el offset correcto.
                if i == 0 and len(self._buffer) >= 16:
                    found = False
                    for offset in range(1, min(8, len(self._buffer) - FRAME_SIZE)):
                        test = bytes(self._buffer[offset:offset + FRAME_SIZE])
                        if test[7] == HDR and test[0] == FTR:
                            i = offset
                            found = True
                            break
                    if found:
                        continue   # Reintenta desde el nuevo offset

                # Sin resincronización posible: salta 8 bytes
                i += FRAME_SIZE
                continue

            # ── Reconstrucción del word de 64 bits ───────────────────
            # Little endian: raw[0] es el byte menos significativo.
            word = 0
            for j in range(8):
                word |= raw[j] << (8 * j)

            # ── Extracción de campos ─────────────────────────────────
            # HDR[63:56] TS[55:24] CH[23:22] VP[21:8] FTR[7:0]
            ts = (word >> 24) & MSK_TS   # Timestamp  (32 bits)
            ch = (word >> 22) & MSK_CH   # Canal      ( 2 bits)
            vp = (word >>  8) & MSK_VP   # Valor Pico (14 bits)

            frames.append(TARFrame(ts, ch, vp, raw))
            i += FRAME_SIZE

        # ── Limpieza del buffer ──────────────────────────────────────
        # Borra los bytes ya consumidos. Los restantes (< 8) esperan
        # el siguiente chunk para completar un frame.
        del self._buffer[:i]
        return frames

    # -------------------------------------------------
    def reset(self):
        """Limpia buffer y contadores (se usa al cambiar de fuente/archivo)."""
        self._buffer.clear()
        self._frames_descartados = 0
