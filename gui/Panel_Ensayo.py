
# gui/Panel_Ensayo.py
"""
Panel de control del ensayo.

Responsabilidades:
    - Mostrar el campo de duración y los botones Iniciar / Finalizar /
      Procesar binario / Consultar configuración.
    - Validar la duración antes de notificar a MainWindow.
    - Mostrar el estado del ensayo en tiempo real (texto dinámico que
      MainWindow actualiza desde _tick_ensayo).

Como todos los paneles, NO ejecuta lógica de ensayo por sí mismo:
los botones solo llaman a los callbacks que MainWindow proporcionó.
"""

import tkinter as tk
from tkinter import ttk, messagebox


class PanelEnsayo(ttk.LabelFrame):

    def __init__(
        self,
        parent,
        on_iniciar_callback=None,
        on_finalizar_callback=None,
        on_cargar_crudo_callback=None,
        on_get_conf_callback=None,
        validar_inicio_callback=None
    ):
        super().__init__(parent, text="Ensayo", padding=2)

        # Cada callback viene de MainWindow. El panel no necesita saber
        # qué hace MainWindow internamente, solo notifica.
        self.on_iniciar      = on_iniciar_callback
        self.on_finalizar    = on_finalizar_callback
        self.on_cargar_crudo = on_cargar_crudo_callback
        self.on_get_conf     = on_get_conf_callback

        # Este es especial: MainWindow le da una función que retorna (bool, str).
        # El panel la llama ANTES de notificar on_iniciar, para ver si las
        # precondiciones están cumplidas (puerto conectado, parámetros aplicados).
        self.validar_inicio  = validar_inicio_callback

        # ---------------------------
        # Título
        # ---------------------------
        ttk.Label(
            self,
            text="Control del Ensayo",
            font=("TkDefaultFont", 11, "bold")
        ).grid(row=0, column=0, columnspan=2, pady=(0, 5))

        # ---------------------------
        # Campo de duración
        # ---------------------------
        ttk.Label(self, text="Duración del ensayo (seg):").grid(row=1, column=0, sticky="w")

        # var_duracion es un StringVar porque el campo puede estar vacío
        # (ensayo sin fin automático). Un IntVar tiraría error con campo vacío.
        self.var_duracion = tk.StringVar(value="")
        self.entry_duracion = ttk.Entry(self, textvariable=self.var_duracion, width=8)
        self.entry_duracion.grid(row=1, column=1, sticky="w", padx=5)

        # ---------------------------
        # Botón: INICIAR
        # ---------------------------
        self.boton_iniciar = ttk.Button(
            self,
            text="Iniciar ensayo",
            command=self._iniciar
        )
        self.boton_iniciar.grid(row=2, column=0, columnspan=2, pady=5, sticky="ew")

        # ---------------------------
        # Botón: FINALIZAR
        # Empieza deshabilitado; MainWindow lo habilita solo cuando hay ensayo LIVE activo.
        # ---------------------------
        self.boton_finalizar = ttk.Button(
            self,
            text="Finalizar ensayo",
            command=self._finalizar
        )
        self.boton_finalizar.grid(row=3, column=0, columnspan=2, pady=5, sticky="ew")

        # ---------------------------
        # Botón: PROCESAR BINARIO PREVIO
        # Abre un explorador de archivos para cargar un .bin grabado antes.
        # ---------------------------
        self.boton_crudo = ttk.Button(
            self,
            text="Procesar binario previo",
            command=self._cargar_crudo
        )
        self.boton_crudo.grid(row=4, column=0, columnspan=2, pady=5, sticky="ew")

        # ---------------------------
        # Botón: CONSULTAR CONFIGURACIÓN TAR
        # Empieza deshabilitado; se habilita cuando hay configuración disponible
        # (tras replay con test-config.txt asociado, o tras GET_CONF exitoso en LIVE).
        # ---------------------------
        self.boton_get_conf = ttk.Button(
            self,
            text="Consultar configuración TAR",
            command=self._get_conf
        )
        self.boton_get_conf.grid(row=5, column=0, columnspan=2, pady=5, sticky="ew")

        # ---------------------------
        # Etiqueta de estado
        # ---------------------------
        # var_estado es un StringVar que MainWindow actualiza desde _tick_ensayo
        # para mostrar "Ensayo en curso (X s)" o "Restan aproximadamente (Y s)".
        self.var_estado = tk.StringVar(value=" - Listo para Ensayar - ")
        self.lbl_estado = ttk.Label(
            self,
            textvariable=self.var_estado,
            font=("JetBrains Mono", 11),
            width=31,
            anchor="w"
        )
        self.lbl_estado.grid(row=6, column=0, sticky="w", padx=(20, 0), pady=(8, 0))

        # Distribución de columnas uniforme
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)


    # ======================================================
    # Handlers de botones (lógica de validación local)
    # ======================================================
    def _iniciar(self):
        """
        Se ejecuta al presionar 'Iniciar ensayo'.

        Flujo de validación en tres capas:
            1. Lee el campo de duración y lo convierte a entero (o None si está vacío).
            2. Llama a validar_inicio (MainWindow verifica puerto + parámetros aplicados).
            3. Si todo pasa, llama a on_iniciar con la duración.
        """
        txt = self.var_duracion.get().strip()

        # Capa 1: campo vacío implica ensayo sin fin automático
        # (solo se detiene con el botón Finalizar o por error)
        if txt == "":
            dur = None
        else:
            # Debe ser un entero positivo; cualquier otra cosa es error
            try:
                dur = int(txt)
                if dur <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Duración inválida",
                    "Ingrese un entero positivo o deje el campo vacío para ensayo sin fin."
                )
                return

        # Capa 2: validación externa (precondiciones que este panel no puede
        # verificar solo, por ejemplo si el puerto está conectado)
        if self.validar_inicio:
            ok, msg = self.validar_inicio()
            if not ok:
                messagebox.showwarning("No se puede iniciar ensayo", msg)
                return

        # Capa 3: todo pasó y se notifica a MainWindow con la duración
        if self.on_iniciar:
            self.on_iniciar(dur)


    def _finalizar(self):
        """Notifica a MainWindow que el usuario pidió detener el ensayo."""
        if self.on_finalizar:
            self.on_finalizar()


    def _cargar_crudo(self):
        """Notifica a MainWindow que el usuario quiere procesar un binario previo."""
        if self.on_cargar_crudo:
            self.on_cargar_crudo()


    def _get_conf(self):
        """Notifica a MainWindow que el usuario quiere ver la configuración TAR."""
        if self.on_get_conf:
            self.on_get_conf() 


    # ======================================================
    # API pública: MainWindow a Panel (actualizar estado visual)
    # ======================================================
    def set_estado(self, texto: str):
        """MainWindow actualiza el texto de estado desde _tick_ensayo (feedback REPLAY)."""
        self.var_estado.set(texto)


    def bloquear_duracion(self, flag: bool):
        """
        Se bloquea el campo de duración durante un ensayo activo.
        No tiene sentido cambiar la duración mientras está corriendo.
        """
        state = "disabled" if flag else "normal"
        self.entry_duracion.config(state=state)
