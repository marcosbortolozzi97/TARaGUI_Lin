
# gui/Panel_Histograma.py
"""
Panel de visualización de histogramas.

Responsabilidades:
    - Dibujar dos histogramas independientes (Canal A y Canal B) con matplotlib.
    - Actualizar los histogramas en tiempo real durante un ensayo (LIVE o REPLAY).
    - Permitir al usuario configurar el rango visual (MIN, MAX) y la calibración keV
      de cada histograma independientemente.
    - Mostrar un contador de pulsos por canal.
    - Mostrar eje secundario superior con conversión automática a keV.

IMPORTANTE: Los histogramas trabajan en CUENTAS ADC (0-16383), no en mV.
    - Eje X inferior: Cuentas ADC (valor directo del ADC de 14 bits)
    - Eje X superior: Energía en keV (conversión con factor y offset editables)

Conversión aplicada:
    Cuentas ADC → mV → keV
    1 cuenta = 3.21 mV (según hardware ZMOD Scope 1410-105)
    E[keV] = (ampl × factor) + offset (editable por el usuario)
"""

import tkinter as tk
from tkinter import ttk, messagebox
import numpy as np
import time
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.axes import Axes
from core.ensayo_sesion import EnsayoSession
from typing import Optional
from matplotlib.ticker import MaxNLocator


class PanelHistograma(ttk.LabelFrame):

    def __init__(self, parent, ensayo_session=None):
        super().__init__(parent, text="Histogramas", padding=0)

        self.ensayo: Optional[EnsayoSession] = ensayo_session

        # _last_idx: posición hasta donde ya se leyeron eventos del ensayo.
        # Cada tick se piden eventos desde esta posición en lugar de releer todos.
        self._last_idx = 0

        # Control de frecuencia de dibujo: no se redibuja en cada tick,
        # solo cada _draw_interval_s segundos.
        self._last_draw_t      = 0
        self._draw_interval_s  = 0.15   # 150 ms entre redraws

        # Almacén de todas las amplitudes por canal (para reconstrucción al cambiar cfg visual)
        self._data = {"A": [], "B": []}

        # Contadores de eventos por canal (se muestran en la GUI)
        self._contadores = {"A": 0, "B": 0}

        # Configuración visual por canal (rango de ejes y tamaño de bin)
        # IMPORTANTE: Ahora trabajamos en CUENTAS ADC (0-16383), no en mV
        # INTERVALO FIJO = 1 (máxima resolución)
        self._cfg = {
            "A": {"min": 0, "max": 16383, "intervalo": 1},
            "B": {"min": 0, "max": 16383, "intervalo": 1},
        }

        # Estructuras del histograma por canal:
        #   _edges: arreglo numpy de bordes de bins (largo = N+1)
        #   _bins:  arreglo numpy de frecuencias   (largo = N)
        #   _hist_dirty: True si hay datos nuevos que no se dibujaron aún
        self._edges      = {"A": None, "B": None}
        self._bins       = {"A": None, "B": None}
        self._hist_dirty = {"A": False, "B": False}

        # ══════════════════════════════════════════════════════════════
        # CONVERSIÓN CUENTAS ADC → mV → keV (CALIBRACIÓN EDITABLE POR CANAL)
        # ══════════════════════════════════════════════════════════════
        
        # Factor 1: ADC → mV (FIJO según hardware)
        # Rango largo: 1 cuenta ADC = 3.21 mV (Tabla 6.1 del proyecto)
        self._MV_a_ADC = 3.21  # mV/cuenta
        
        # Factor 2 y Offset: mV → keV (EDITABLE por canal)
        # Valores por defecto para detector de centelleo típico
        self._cal_factor = {"A": 0.05, "B": 0.05}  # keV/mV por canal
        self._cal_offset = {"A": 0, "B": 0}         # keV por canal
        
        # Fórmula de conversión:
        #   E[keV] = (amp_mV × factor[canal]) + offset[canal]

        # Construye la GUI y dibuja histogramas vacíos
        self._build_ui()
        for c in ("A", "B"):
            self._redibujar(c)


    # ==================================================
    # API pública
    # ==================================================
    def set_ensayo(self, ensayo: EnsayoSession):
        """
        Conecta este panel a un ensayo nuevo.
        Se resetea todo el estado interno porque los datos del ensayo anterior
        ya no son relevantes.
        """
        self.ensayo = ensayo
        self.reset_total()


    # ==================================================
    # Construcción de la GUI
    # ==================================================
    def _build_ui(self):
        # Filas: histogramas crecen verticalmente
        self.rowconfigure(0, weight=1)   # Histograma A: crece
        self.rowconfigure(1, weight=1)   # Histograma B: crece
        self.columnconfigure(0, weight=1)

        # Cada canal es un bloque independiente
        self._build_canal("A", 0)   # Fila 0
        self._build_canal("B", 1)   # Fila 1

        # Botón global que resetea ambos histogramas (sin reiniciar el ensayo).
        # Empieza deshabilitado; se habilita cuando el ensayo termina.
        self.btn_borrar = ttk.Button(
            self, text="Reset histogramas", command=self.resetear
        )
        self.btn_borrar.grid(row=2, column=0, pady=4)
        self.btn_borrar.config(state="disabled")


    def _build_canal(self, canal, row):
        """
        Construye el bloque visual completo de un canal:
            - Fila de configuración (MIN, MAX, Factor keV, Offset keV + Aplicar + feedback)
            - Figura matplotlib (el histograma propiamente)
            - Panel lateral con el contador de pulsos
        """
        # Marco exterior del canal
        frame = ttk.LabelFrame(self)
        frame.grid(row=row, column=0, sticky="nsew", pady=1)
        frame.rowconfigure(1, weight=1)      # La fila del histograma crece
        frame.columnconfigure(1, weight=1)   # La columna del histograma crece

        # ── Fila de configuración ────────────────────────────────────
        cfg = ttk.Frame(frame)
        cfg.grid(row=0, column=0, sticky="w")

        # MIN y MAX
        vars_ = {}
        ttk.Label(cfg, text="MIN:").grid(row=0, column=0, padx=5)
        v_min = tk.StringVar(value=str(self._cfg[canal]["min"]))
        ttk.Entry(cfg, textvariable=v_min, width=7).grid(row=0, column=1, padx=2)
        vars_["min"] = v_min

        ttk.Label(cfg, text="MAX:").grid(row=0, column=2, padx=5)
        v_max = tk.StringVar(value=str(self._cfg[canal]["max"]))
        ttk.Entry(cfg, textvariable=v_max, width=7).grid(row=0, column=3, padx=2)
        vars_["max"] = v_max

        # Botón Full (resetear a rango completo 0-16383)
        ttk.Button(
            cfg,
            text="Full",
            width=5,
            command=lambda c=canal, v=vars_: self._resetear_a_extremos(c, v)
        ).grid(row=0, column=4, padx=(2,10))

        # Factor keV
        ttk.Label(cfg, text="Factor:").grid(row=0, column=5, padx=(10,2))
        v_factor = tk.StringVar(value=str(self._cal_factor[canal]))
        ttk.Entry(cfg, textvariable=v_factor, width=7).grid(row=0, column=6, padx=2)
        ttk.Label(cfg, text="keV/cuenta").grid(row=0, column=7, padx=(0,5))
        vars_["factor"] = v_factor

        # Offset keV
        ttk.Label(cfg, text="Offset:").grid(row=0, column=8, padx=(10,2))
        v_offset = tk.StringVar(value=str(self._cal_offset[canal]))
        ttk.Entry(cfg, textvariable=v_offset, width=7).grid(row=0, column=9, padx=2)
        ttk.Label(cfg, text="keV").grid(row=0, column=10, padx=(0,10))
        vars_["offset"] = v_offset

        # Botón Aplicar
        ttk.Button(
            cfg,
            text="Aplicar",
            command=lambda c=canal, v=vars_: self._aplicar_cfg(c, v)
        ).grid(row=0, column=11, padx=5)

        # Feedback temporal
        status = ttk.Label(cfg, text="", foreground="green", font=("TkDefaultFont", 8))
        status.grid(row=0, column=12, padx=(5, 0), sticky="w")
        setattr(self, f"status_{canal}", status)

        # ── Figura matplotlib ────────────────────────────────────────
        fig = Figure(figsize=(8.25, 2))
        fig.subplots_adjust(top=0.75, bottom=0.2)
        ax = fig.add_subplot(111)
        canvas = FigureCanvasTkAgg(fig, master=frame)
        canvas.get_tk_widget().grid(row=1, column=0, sticky="nsew", pady=(0, 0))

        # ── Panel lateral: contador de pulsos ────────────────────────
        panel_derecho = ttk.Frame(frame)
        panel_derecho.grid(row=0, column=1, rowspan=2, padx=5, sticky="n")
        panel_derecho.columnconfigure(0, weight=1)

        ttk.Label(
            panel_derecho,
            text="CONTADOR DE PULSOS",
            font=("TkDefaultFont", 8)
        ).grid(row=0, column=0, pady=(5, 5))

        lbl_valor = ttk.Label(
            panel_derecho,
            text="0",
            font=("TkDefaultFont", 14, "bold")
        )
        lbl_valor.grid(row=1, column=0, pady=(10, 0))

        # Guardar referencias
        setattr(self, f"lbl_{canal}",    lbl_valor)
        setattr(self, f"ax_{canal}",     ax)
        setattr(self, f"ax_kev_{canal}", None)
        setattr(self, f"canvas_{canal}", canvas)


    # ==================================================
    # Bloqueo y controles durante ensayo
    # ==================================================
    def bloquear(self, flag: bool):
        """
        Bloquea los campos de configuración y los botones Aplicar durante el ensayo.
        Usa recorrido recursivo.
        """
        state = "disabled" if flag else "normal"
        
        def _bloquear_recursivo(widget):
            if isinstance(widget, (ttk.Entry, ttk.Button)):
                if widget is not self.btn_borrar:
                    widget.config(state=state)
            for child in widget.winfo_children():
                _bloquear_recursivo(child)
        
        _bloquear_recursivo(self)


    def habilitar_borrar(self, flag: bool):
        """Controla el botón Reset independientemente del bloqueo general."""
        self.btn_borrar.config(state="normal" if flag else "disabled")


    def _mostrar_status(self, canal: str, texto: str, timeout_ms=2000):
        """Feedback temporal por canal."""
        lbl = getattr(self, f"status_{canal}")
        lbl.config(text=texto)
        lbl.after(timeout_ms, lambda: lbl.config(text=""))


    # ==================================================
    # Manejo de datos
    # ==================================================
    def resetear(self):
        """
        Reset visual: limpia los histogramas y contadores pero NO toca el ensayo.
        """
        self._data       = {"A": [], "B": []}
        self._contadores = {"A": 0, "B": 0}

        for canal in ("A", "B"):
            self._recalcular_histograma(canal)
            self._actualizar_label_contador(canal)
            self._redibujar(canal)


    def reset_total(self):
        """
        Reset Completo: se llama al conectar un ensayo nuevo (set_ensayo).
        """
        self._last_idx    = 0
        self._last_draw_t = 0

        self._data       = {"A": [], "B": []}
        self._contadores = {"A": 0, "B": 0}

        for canal in ("A", "B"):
            self._recalcular_histograma(canal)
            self._hist_dirty[canal] = False
            self._actualizar_label_contador(canal)
            self._redibujar(canal)


    def _actualizar_label_contador(self, canal):
        """Actualiza el número que se muestra en el panel lateral."""
        label = getattr(self, f"lbl_{canal}")
        label.config(text=str(self._contadores[canal]))


    def _recalcular_histograma(self, canal):
        """
        Recalcula los bordes y reinicia los bins a cero según la configuración actual.
        """
        cfg   = self._cfg[canal]
        edges = np.arange(cfg["min"], cfg["max"] + cfg["intervalo"], cfg["intervalo"])
        self._edges[canal] = edges
        self._bins[canal]  = np.zeros(len(edges) - 1, dtype=int)
        self._hist_dirty[canal] = True


    # ==================================================
    # Tick periódico (llamado desde MainWindow cada 200 ms)
    # ==================================================
    def tick(self):
        """
        Se llama periódicamente desde _tick_ensayo de MainWindow.
        """
        if not self.ensayo:
            return

        # Paso 1: obtener eventos nuevos
        nuevos = self.ensayo.get_eventos_desde(self._last_idx)
        self._last_idx += len(nuevos)

        # Paso 2: clasificar y acumular
        for ev in nuevos:
            ch = ev.get("chan")
            canal = "A" if ch == 0 else "B" if ch == 1 else None
            if canal is None:
                continue

            # Los datos YA vienen en cuentas ADC (no dividir por 3.21)
            amp_adc = ev["ampl"]
            
            self._data[canal].append(amp_adc)
            self._contadores[canal] += 1
            self._actualizar_label_contador(canal)

            edges = self._edges[canal]
            bins  = self._bins[canal]
            if edges is None or bins is None:
                continue

            idx = np.searchsorted(edges, amp_adc, side="right") - 1
            if 0 <= idx < len(bins):
                bins[idx] += 1
                self._hist_dirty[canal] = True

        # Paso 3: dibujar solo si pasó suficiente tiempo
        self._intentar_redibujar()


    def _intentar_redibujar(self):
        """
        Gatekeeper del dibujo: solo redibuja si pasaron al menos 150 ms.
        """
        now = time.time()
        if now - self._last_draw_t < self._draw_interval_s:
            return

        for canal in ("A", "B"):
            if self._hist_dirty[canal]:
                self._redibujar(canal)
                self._hist_dirty[canal] = False

        self._last_draw_t = now


    # ==================================================
    # Dibujo
    # ==================================================
    def _redibujar(self, canal: str):
        """
        Redibuja el histograma con eje keV usando calibración del canal.
        
        OPTIMIZACIÓN: Con intervalo=1 tenemos hasta 16,384 bins. Dibujar
        16k barras individuales es muy lento. Usamos plot con step en su lugar.
        """
        ax:     Axes               = getattr(self, f"ax_{canal}")
        canvas: FigureCanvasTkAgg  = getattr(self, f"canvas_{canal}")

        # Remover solo los elementos dibujados (lines, patches)
        for artist in ax.lines[:]:
            artist.remove()
        for artist in ax.patches[:]:
            artist.remove()
        # Remover también collections (fill_between crea PolyCollection)
        for artist in ax.collections[:]:
            artist.remove()

        cfg   = self._cfg[canal]
        bins  = self._bins[canal]
        edges = self._edges[canal]

        if bins is not None and edges is not None:
            # ═══════════════════════════════════════════════════════════
            # OPTIMIZACIÓN CRÍTICA: Con intervalo=1 (máxima resolución)
            # tenemos miles de bins.
            # ═══════════════════════════════════════════════════════════
            num_bins = len(bins)
            
            if num_bins > 1000:
                # Muchos bins: usar step (línea escalonada) - MUY RÁPIDO
                ax.step(edges[:-1], bins, where='post', linewidth=0.5, color='#1f77b4')
                # Rellenar el área bajo la curva para mejor visualización
                ax.fill_between(edges[:-1], bins, step='post', alpha=0.3, color='#1f77b4')
            else:
                # Pocos bins: usar bar (barras individuales) - normal
                ax.bar(edges[:-1], bins, width=cfg["intervalo"], align="edge", color='#1f77b4')

        # Actualizar límites y etiquetas del eje principal
        ax.set_title(f"Canal {canal}", fontsize=11, fontweight='bold')
        ax.set_xlabel("Amplitud (cuentas ADC)", fontsize=10)
        ax.set_ylabel("Frecuencia", fontsize=10)
        ax.set_xlim(cfg["min"], cfg["max"])
        if bins is not None and len(bins) > 0:
            max_freq = np.max(bins)
            ax.set_ylim(0, max_freq * 1.1 if max_freq > 0 else 1.0)
        else:
            ax.set_ylim(0, 1.0)
        ax.grid(True, which='major', axis='both', linestyle='-', linewidth=0.4, color='#B0B0B0',alpha=0.6)  
        ax.set_axisbelow(True)  # Si queremos asegurar que el grid quede detrás de las curvas
        ax.xaxis.set_major_locator(MaxNLocator(integer=True))

        # ══════════════════════════════════════════════════════════════
        # Eje secundario: Energía (keV)
        # ══════════════════════════════════════════════════════════════
        ax_kev = getattr(self, f"ax_kev_{canal}", None)
        
        if ax_kev is None:
            # Primera vez: crear eje keV
            factor = self._cal_factor[canal]
            offset = self._cal_offset[canal]
            
            def adc_to_kev(adc_counts):
                mv = adc_counts * self._MV_a_ADC
                kev = (mv * factor) + offset
                return kev
            
            def kev_to_adc(kev):
                mv = (kev - offset) / factor
                adc = mv / self._MV_a_ADC
                return adc
            
            ax_kev = ax.secondary_xaxis('top', functions=(adc_to_kev, kev_to_adc))
            ax_kev.set_xlabel("Energía (keV)", fontsize=9, color='darkred', style='italic')
            ax_kev.tick_params(axis='x', labelcolor='darkred', labelsize=8)
            setattr(self, f"ax_kev_{canal}", ax_kev)
        
        # El eje keV se actualiza automáticamente cuando cambia xlim del eje principal

        canvas.draw_idle()


    def _reconstruir_histograma_desde_datos(self, canal):
        """
        Reconstruye _bins desde _data cuando el usuario cambia MIN/MAX.
        """
        self._recalcular_histograma(canal)

        edges = self._edges[canal]
        bins  = self._bins[canal]

        for amp in self._data[canal]:
            idx = np.searchsorted(edges, amp, side="right") - 1
            if 0 <= idx < len(bins):
                bins[idx] += 1

        self._hist_dirty[canal] = True


    def _recrear_eje_kev(self, canal):
        """
        Recrea el eje keV cuando cambia la calibración (Factor/Offset).
        
        Este método es COSTOSO computacionalmente, por eso solo se llama
        cuando el usuario presiona Aplicar y cambia la calibración, NO en
        cada redibujado normal.
        """
        ax = getattr(self, f"ax_{canal}")
        ax_kev = getattr(self, f"ax_kev_{canal}", None)
        
        # Remover eje viejo si existe
        if ax_kev is not None:
            ax_kev.remove()
        
        # Crear nuevo eje con la calibración actualizada
        factor = self._cal_factor[canal]
        offset = self._cal_offset[canal]
        
        def adc_to_kev(adc_counts):
            mv = adc_counts * self._MV_a_ADC
            kev = (mv * factor) + offset
            return kev
        
        def kev_to_adc(kev):
            mv = (kev - offset) / factor
            adc = mv / self._MV_a_ADC
            return adc
        
        ax_kev = ax.secondary_xaxis('top', functions=(adc_to_kev, kev_to_adc))
        ax_kev.set_xlabel("Energía (keV)", fontsize=9, color='darkred', style='italic')
        ax_kev.tick_params(axis='x', labelcolor='darkred', labelsize=8)
        setattr(self, f"ax_kev_{canal}", ax_kev)


    # ==================================================
    # Aplicar configuración
    # ==================================================
    def _resetear_a_extremos(self, canal, vars_):
        """
        Resetea MIN y MAX al rango completo del ADC (0-16383).
        Actualiza solo los campos, no aplica los cambios automáticamente.
        """
        vars_["min"].set("0")
        vars_["max"].set("16383")
        self._mostrar_status(canal, "Rango completo (presione Aplicar)")


    def _aplicar_cfg(self, canal, vars_):
        """
        Se ejecuta al presionar Aplicar en un histograma.
        Aplica tanto zoom (MIN/MAX) como calibración (Factor/Offset).
        """
        try:
            vmin = int(vars_["min"].get())
            vmax = int(vars_["max"].get())
            factor = float(vars_["factor"].get())
            offset = int(vars_["offset"].get())
        except ValueError:
            messagebox.showerror("Error", "Valores inválidos")
            return

        if vmin < 0 or vmax < 0:
            messagebox.showerror("Error", "MIN y MAX no pueden ser negativos")
            return

        if vmax <= vmin:
            messagebox.showerror("Error", "MAX debe ser mayor que MIN")
            return

        if factor <= 0:
            messagebox.showerror("Error", "Factor debe ser mayor a cero")
            return

        # Detectar si cambió la calibración
        calibracion_cambio = (
            factor != self._cal_factor[canal] or 
            offset != self._cal_offset[canal]
        )

        # Guarda configuración del canal
        self._cfg[canal] = {
            "min":       vmin,
            "max":       vmax,
            "intervalo": 1,
        }
        
        # Guarda calibración del canal
        self._cal_factor[canal] = factor
        self._cal_offset[canal] = offset

        # Reconstruye histograma
        self._reconstruir_histograma_desde_datos(canal)
        
        # SOLO recrear eje keV si cambió la calibración
        # (esto es muy costoso computacionalmente, por eso lo evitamos cuando solo cambia MIN/MAX)
        if calibracion_cambio:
            self._recrear_eje_kev(canal)
        
        # Redibuja
        self._redibujar(canal)
        self._mostrar_status(canal, "Valores configurados correctamente")