
# gui/Panel_Parametros.py
"""
Panel de configuración de histéresis.

El Panel Parámtros tiene variadas responsabilidades:
    - Mostrar los 4 campos de umbral (CHA min/max, CHB min/max).
    - Validar que los valores sean numéricos, no negativos, y que min < max.
    - Notificar a MainWindow cuando los parámetros son aplicados correctamente.
    - Invalidar automáticamente los parámetros si el usuario toca algún campo
      después de haber aplicado (se fuerza a reaplicar antes del próximo ensayo).
    - Bloquearse durante ensayo activo.

Los parámetros que salen de este panel son un diccionario con las claves
que ensayo_sesion.apply_hysteresis() espera. MainWindow los guarda y los
envía al TAR cuando el ensayo inicia.
"""

import tkinter as tk
from tkinter import ttk, messagebox


class PanelParametros(ttk.LabelFrame):

    def __init__(self, parent, on_apply_params_callback=None):
        super().__init__(parent, text="Parámetros", padding=2)

        # Rango válido de los umbrales en mV (validación física del sistema)
        self.MIN_VALUE = 0
        self.MAX_VALUE = 16383

        # Callback que MainWindow proporcionó. Se llama con el diccionario
        # de parámetros cuando el usuario presiona Aplicar y todo es válido.
        self.on_apply_params_callback = on_apply_params_callback

        # Flag lógico: True solo entre "el usuario presionó Aplicar con valores OK"
        # y "el usuario modificó algún campo". MainWindow lo consulta antes de
        # permitir iniciar un ensayo (no se puede ensayar sin aplicar primero).
        self.parametros_aplicados = False
        # Estado de bloqueo de los campos (se bloquean durante ensayo)
        self.bloqueado = False

        # -------------------------------------------------
        # Validación en tiempo real: solo dígitos mientras se escribe.
        # register() crea un comando Tcl que Tkinter puede llamar.
        # "%P" es el valor del campo DESPUÉS del keystroke propuesto.
        # Si retorna True el keystroke se permite; si False se bloquea.
        # -------------------------------------------------
        vcmd = (self.register(self._validar_numerico), "%P")

        # -------------------------------------------------
        # Feedback temporal (mensaje verde que desaparece solo)
        # -------------------------------------------------
        self.status = ttk.Label(self, text="", font=("TkDefaultFont", 8), foreground="green")
        self.status.grid(row=8, column=0, columnspan=3, pady=(1, 1), sticky="w")

        # ----------------------------------
        # Título
        # ----------------------------------
        ttk.Label(
            self,
            text="Configuración de Ventanas de Histéresis",
            font=("TkDefaultFont", 11, "bold")
        ).grid(row=0, column=0, columnspan=3, pady=(0, 5))

        # Factor de conversión ADC → mV
        self.MV_a_ADC = 3.21  # Según hardware ZMOD

        # ----------------------------------
        # Umbrales Canal A
        # ----------------------------------
        ttk.Label(self, text="Umbral CHA:", font=("TkDefaultFont", 9, "bold")).grid(
            row=1, column=0, sticky="w", pady=(5, 0)
        )

        ttk.Label(self, text="Min:").grid(row=2, column=0, sticky="w")
        ttk.Label(self, text="Max:").grid(row=3, column=0, sticky="w")

        self.var_cha_min = tk.StringVar(value="0")
        self.var_cha_max = tk.StringVar(value="16383")

        # validate="key": se valida en cada keystroke, no solo al salir del campo.
        # validatecommand=vcmd: la función registrada arriba.
        self.entry_cha_min = ttk.Entry(
            self, textvariable=self.var_cha_min, width=8,
            validate="key", validatecommand=vcmd
        )
        self.entry_cha_max = ttk.Entry(
            self, textvariable=self.var_cha_max, width=8,
            validate="key", validatecommand=vcmd
        )

        self.entry_cha_min.grid(row=2, column=1, padx=5)
        self.entry_cha_max.grid(row=3, column=1, padx=5)

        # Labels dinámicos con conversión a mV
        self.lbl_cha_min_mv = ttk.Label(self, text="cuentas ADC (0.0 mV)", 
                                         foreground="#3D3D3D")
        self.lbl_cha_max_mv = ttk.Label(self, text="cuentas ADC (52571.4 mV)", 
                                         foreground="#3D3D3D")
        
        self.lbl_cha_min_mv.grid(row=2, column=2, sticky="w", padx=(5,0))
        self.lbl_cha_max_mv.grid(row=3, column=2, sticky="w", padx=(5,0))

        # ----------------------------------
        # Umbrales Canal B (mismo esquema que Canal A)
        # ----------------------------------
        ttk.Label(self, text="Umbral CHB:", font=("TkDefaultFont", 9, "bold")).grid(
            row=4, column=0, sticky="w", pady=(5, 0)
        )

        ttk.Label(self, text="Min:").grid(row=5, column=0, sticky="w")
        ttk.Label(self, text="Max:").grid(row=6, column=0, sticky="w")

        self.var_chb_min = tk.StringVar(value="0")
        self.var_chb_max = tk.StringVar(value="16383")

        self.entry_chb_min = ttk.Entry(
            self, textvariable=self.var_chb_min, width=8,
            validate="key", validatecommand=vcmd
        )
        self.entry_chb_max = ttk.Entry(
            self, textvariable=self.var_chb_max, width=8,
            validate="key", validatecommand=vcmd
        )

        self.entry_chb_min.grid(row=5, column=1, padx=5)
        self.entry_chb_max.grid(row=6, column=1, padx=5)

        # Labels dinámicos con conversión a mV
        self.lbl_chb_min_mv = ttk.Label(self, text="cuentas ADC (0.0 mV)", 
                                         foreground="#3D3D3D")
        self.lbl_chb_max_mv = ttk.Label(self, text="cuentas ADC (52571.4 mV)", 
                                         foreground="#3D3D3D")
        
        self.lbl_chb_min_mv.grid(row=5, column=2, sticky="w", padx=(5,0))
        self.lbl_chb_max_mv.grid(row=6, column=2, sticky="w", padx=(5,0))

        # ----------------------------------
        # Botón Aplicar
        # ----------------------------------
        self.btn_apply = ttk.Button(
            self, text="Aplicar parámetros", command=self._aplicar
        )
        self.btn_apply.grid(row=7, column=0, columnspan=3, pady=5)

        # Distribución de columnas uniforme
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.columnconfigure(2, weight=1)

         # -------------------------------------------------
        # Traza de cambios CON ACTUALIZACIÓN DE mV
        # -------------------------------------------------
        for var, lbl in [
            (self.var_cha_min, self.lbl_cha_min_mv),
            (self.var_cha_max, self.lbl_cha_max_mv),
            (self.var_chb_min, self.lbl_chb_min_mv),
            (self.var_chb_max, self.lbl_chb_max_mv)
        ]:
            # El *args atrapa los 3 argumentos automáticos de Tkinter (name, index, mode)
            var.trace_add("write", lambda *args, v=var, l=lbl: self._on_value_change(v, l))


    # ============================================================
    # Validación y aplicación
    # ============================================================
    def _validar_numerico(self, valor: str) -> bool:
        """
        Se llama en cada keystroke. Retorna True si el keystroke es permitido.
        Solo permite dígitos o campo vacío (para poder borrar todo).
        """
        return valor.isdigit() or valor == ""


    def _aplicar(self):
        """
        Se ejecuta al presionar 'Aplicar parámetros'.

        Validaciones en capas:
            Capa 1: conversión a enteros (falla si algún campo está vacío).
            Capa 2: rango físico (cada valor entre 0 y 16383).
            Capa 3: lógica (min < max en cada canal).
            Capa 4: si todo pasa, construye el diccionario y notifica a MainWindow.
        """
        # Capa 1: todos los campos son obligatorios
        try:
            A_min = int(self.var_cha_min.get())
            A_max = int(self.var_cha_max.get())
            B_min = int(self.var_chb_min.get())
            B_max = int(self.var_chb_max.get())
        except ValueError:
            messagebox.showerror("Error", "Los campos son obligatorios y deben ser de tipo numérico.")
            return

        errores = []

        # Capa 2: rango físico permitido por el sistema
        if not (self.MIN_VALUE <= A_min <= self.MAX_VALUE):
            errores.append("CHA Min fuera de rango.")
        if not (self.MIN_VALUE <= A_max <= self.MAX_VALUE):
            errores.append("CHA Max fuera de rango.")
        if not (self.MIN_VALUE <= B_min <= self.MAX_VALUE):
            errores.append("CHB Min fuera de rango.")
        if not (self.MIN_VALUE <= B_max <= self.MAX_VALUE):
            errores.append("CHB Max fuera de rango.")

        # Capa 3: coherencia lógica entre min y max de cada canal
        if A_min >= A_max:
            errores.append("CHA Min debe ser < CHA Max.")
        if B_min >= B_max:
            errores.append("CHB Min debe ser < CHB Max.")

        if errores:
            messagebox.showerror("Error en parámetros", "\n".join(errores))
            return

        # Capa 4: todo OK, se construye el diccionario con las claves que espera
        # ensayo_sesion.apply_hysteresis()
        params = {
            "umbral_cha_min": A_min,
            "umbral_cha_max": A_max,
            "umbral_chb_min": B_min,
            "umbral_chb_max": B_max
        }

        # Notifica a MainWindow (que los guarda para enviarlos al TAR al iniciar)
        if self.on_apply_params_callback:
            self.on_apply_params_callback(params)

        # Marca que los parámetros actuales están aplicados y listos para ensayo
        self.parametros_aplicados = True
        self._mostrar_status("Valores almacenados correctamente")


    # ============================================================
    # Consulta de estado
    # ============================================================
    def parametros_estan_aplicados(self) -> bool:
        """
        MainWindow la llama desde _validar_inicio para verificar que
        el usuario no olvide presionar Aplicar antes de ensayar.
        """
        return self.parametros_aplicados


    # ============================================================
    # Actualización en vivo de conversión
    # ============================================================
    def _on_value_change(self, var, label):
        """
        Se ejecuta cada vez que el usuario escribe.
        Actualiza el texto en mV e invalida el estado de 'Aplicado'.
        """
        # 1. Invalidar el flag (obligar a reaplicar)
        self.parametros_aplicados = False
        self.status.config(text="") # Limpiar mensaje de "Éxito" si existía

        # 2. Intentar actualizar el label de mV en tiempo real
        try:
            valor_str = var.get()
            if valor_str:
                adc_count = int(valor_str)
                # USAR EL NOMBRE EXACTO: self.MV_a_ADC
                mv_value = adc_count * self.MV_a_ADC
                label.config(text=f"cuentas ADC ({mv_value:.1f} mV)")
            else:
                label.config(text="cuentas ADC (0.0 mV)")
        except ValueError:
            label.config(text="Valor inválido")


    # ============================================================
    # Feedback visual temporal
    # ============================================================
    def _mostrar_status(self, texto: str, timeout_ms=1500):
        """
        Muestra un mensaje verde por 1.5 segundos y luego desaparece solo.
        after() es el mecanismo de Tkinter para ejecutar algo después de un tiempo
        sin bloquear la GUI (equivale a un timer no bloqueante).
        """
        self.status.config(text=texto)
        self.status.after(timeout_ms, lambda: self.status.config(text=""))


    # ============================================================
    # Bloqueo durante ensayo
    # ============================================================
    def bloquear(self, flag: bool):
        """
        Durante un ensayo activo los campos se deshabilitan.
        No tiene sentido cambiar los umbrales mientras el TAR está emitiendo
        con los valores actuales.
        """
        self.bloqueado = flag
        state = "disabled" if flag else "normal"

        for widget in [
            self.entry_cha_min, self.entry_cha_max,
            self.entry_chb_min, self.entry_chb_max,
            self.btn_apply
        ]:
            widget.config(state=state)
